# Copyright 2012 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import json
import six
from six.moves.urllib import parse

import warlock

from glanceclient.common import utils
from glanceclient import exc
from glanceclient.openstack.common import strutils

DEFAULT_PAGE_SIZE = 20


class Controller(object):
    def __init__(self, http_client, model):
        self.http_client = http_client
        self.model = model

    def list(self, **kwargs):
        """Retrieve a listing of Image objects

        :param page_size: Number of images to request in each paginated request
        :returns generator over list of Images
        """

        ori_validate_fun = self.model.validate
        empty_fun = lambda *args, **kwargs: None

        def paginate(url):
            resp, body = self.http_client.json_request('GET', url)
            for image in body['images']:
                # NOTE(bcwaldon): remove 'self' for now until we have
                # an elegant way to pass it into the model constructor
                # without conflict.
                image.pop('self', None)
                yield self.model(**image)
                # NOTE(zhiyan): In order to resolve the performance issue
                # of JSON schema validation for image listing case, we
                # don't validate each image entry but do it only on first
                # image entry for each page.
                self.model.validate = empty_fun

            # NOTE(zhiyan); Reset validation function.
            self.model.validate = ori_validate_fun

            try:
                next_url = body['next']
            except KeyError:
                return
            else:
                for image in paginate(next_url):
                    yield image

        filters = kwargs.get('filters', {})

        if not kwargs.get('page_size'):
            filters['limit'] = DEFAULT_PAGE_SIZE
        else:
            filters['limit'] = kwargs['page_size']

        tags = filters.pop('tag', [])
        tags_url_params = []

        for tag in tags:
            if isinstance(tag, six.string_types):
                tags_url_params.append({'tag': strutils.safe_encode(tag)})

        for param, value in six.iteritems(filters):
            if isinstance(value, six.string_types):
                filters[param] = strutils.safe_encode(value)

        url = '/v2/images?%s' % parse.urlencode(filters)

        for param in tags_url_params:
            url = '%s&%s' % (url, parse.urlencode(param))

        for image in paginate(url):
            yield image

    def get(self, image_id):
        url = '/v2/images/%s' % image_id
        resp, body = self.http_client.json_request('GET', url)
        #NOTE(bcwaldon): remove 'self' for now until we have an elegant
        # way to pass it into the model constructor without conflict
        body.pop('self', None)
        return self.model(**body)

    def data(self, image_id, do_checksum=True):
        """
        Retrieve data of an image.

        :param image_id:    ID of the image to download.
        :param do_checksum: Enable/disable checksum validation.
        """
        url = '/v2/images/%s/file' % image_id
        resp, body = self.http_client.raw_request('GET', url)
        checksum = resp.getheader('content-md5', None)
        if do_checksum and checksum is not None:
            body.set_checksum(checksum)
        return body

    def upload(self, image_id, image_data, image_size=None):
        """
        Upload the data for an image.

        :param image_id: ID of the image to upload data for.
        :param image_data: File-like object supplying the data to upload.
        :param image_size: Total size in bytes of image to be uploaded.
        """
        url = '/v2/images/%s/file' % image_id
        hdrs = {'Content-Type': 'application/octet-stream'}
        self.http_client.raw_request('PUT', url,
                                     headers=hdrs,
                                     body=image_data,
                                     content_length=image_size)

    def delete(self, image_id):
        """Delete an image."""
        self.http_client.json_request('DELETE', '/v2/images/%s' % image_id)

    def create(self, **kwargs):
        """Create an image."""
        url = '/v2/images'

        image = self.model()
        for (key, value) in kwargs.items():
            try:
                setattr(image, key, value)
            except warlock.InvalidOperation as e:
                raise TypeError(utils.exception_to_str(e))

        resp, body = self.http_client.json_request('POST', url, body=image)
        #NOTE(esheffield): remove 'self' for now until we have an elegant
        # way to pass it into the model constructor without conflict
        body.pop('self', None)
        return self.model(**body)

    def update(self, image_id, remove_props=None, **kwargs):
        """
        Update attributes of an image.

        :param image_id: ID of the image to modify.
        :param remove_props: List of property names to remove
        :param **kwargs: Image attribute names and their new values.
        """
        image = self.get(image_id)
        for (key, value) in kwargs.items():
            try:
                setattr(image, key, value)
            except warlock.InvalidOperation as e:
                raise TypeError(utils.exception_to_str(e))

        if remove_props is not None:
            cur_props = image.keys()
            new_props = kwargs.keys()
            #NOTE(esheffield): Only remove props that currently exist on the
            # image and are NOT in the properties being updated / added
            props_to_remove = set(cur_props).intersection(
                set(remove_props).difference(new_props))

            for key in props_to_remove:
                delattr(image, key)

        url = '/v2/images/%s' % image_id
        hdrs = {'Content-Type': 'application/openstack-images-v2.1-json-patch'}
        self.http_client.raw_request('PATCH', url,
                                     headers=hdrs,
                                     body=image.patch)

        #NOTE(bcwaldon): calling image.patch doesn't clear the changes, so
        # we need to fetch the image again to get a clean history. This is
        # an obvious optimization for warlock
        return self.get(image_id)

    def _get_image_with_locations_or_fail(self, image_id):
        image = self.get(image_id)
        if getattr(image, 'locations', None) is None:
            raise exc.HTTPBadRequest('The administrator has disabled '
                                     'API access to image locations')
        return image

    def _send_image_update_request(self, image_id, patch_body):
        url = '/v2/images/%s' % image_id
        hdrs = {'Content-Type': 'application/openstack-images-v2.1-json-patch'}
        self.http_client.raw_request('PATCH', url,
                                     headers=hdrs,
                                     body=json.dumps(patch_body))

    def add_location(self, image_id, url, metadata):
        """Add a new location entry to an image's list of locations.

        It is an error to add a URL that is already present in the list of
        locations.

        :param image_id: ID of image to which the location is to be added.
        :param url: URL of the location to add.
        :param metadata: Metadata associated with the location.
        :returns: The updated image
        """
        image = self._get_image_with_locations_or_fail(image_id)
        url_list = [l['url'] for l in image.locations]
        if url in url_list:
            err_str = 'A location entry at %s already exists' % url
            raise exc.HTTPConflict(err_str)

        add_patch = [{'op': 'add', 'path': '/locations/-',
                      'value': {'url': url, 'metadata': metadata}}]
        self._send_image_update_request(image_id, add_patch)
        return self.get(image_id)

    def delete_locations(self, image_id, url_set):
        """Remove one or more location entries of an image.

        :param image_id: ID of image from which locations are to be removed.
        :param url_set: set of URLs of location entries to remove.
        :returns: None
        """
        image = self._get_image_with_locations_or_fail(image_id)
        current_urls = [l['url'] for l in image.locations]

        missing_locs = url_set.difference(set(current_urls))
        if missing_locs:
            raise exc.HTTPNotFound('Unknown URL(s): %s' % list(missing_locs))

        # NOTE: warlock doesn't generate the most efficient patch for remove
        # operations (it shifts everything up and deletes the tail elements) so
        # we do it ourselves.
        url_indices = [current_urls.index(url) for url in url_set]
        url_indices.sort(reverse=True)
        patches = [{'op': 'remove', 'path': '/locations/%s' % url_idx}
                   for url_idx in url_indices]
        self._send_image_update_request(image_id, patches)

    def update_location(self, image_id, url, metadata):
        """Update an existing location entry in an image's list of locations.

        The URL specified must be already present in the image's list of
        locations.

        :param image_id: ID of image whose location is to be updated.
        :param url: URL of the location to update.
        :param metadata: Metadata associated with the location.
        :returns: The updated image
        """
        image = self._get_image_with_locations_or_fail(image_id)
        url_map = dict([(l['url'], l) for l in image.locations])
        if url not in url_map:
            raise exc.HTTPNotFound('Unknown URL: %s' % url)

        if url_map[url]['metadata'] == metadata:
            return image

        # NOTE: The server (as of now) doesn't support modifying individual
        # location entries. So we must:
        #   1. Empty existing list of locations.
        #   2. Send another request to set 'locations' to the new list
        #      of locations.
        url_map[url]['metadata'] = metadata
        patches = [{'op': 'replace',
                    'path': '/locations',
                    'value': p} for p in ([], list(url_map.values()))]
        self._send_image_update_request(image_id, patches)

        return self.get(image_id)
