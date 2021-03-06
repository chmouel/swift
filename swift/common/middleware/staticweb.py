# Copyright (c) 2010-2012 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This StaticWeb WSGI middleware will serve container data as a static web site
with index file and error file resolution and optional file listings. This mode
is normally only active for anonymous requests. If you want to use it with
authenticated requests, set the ``X-Web-Mode: true`` header on the request.

The ``staticweb`` filter should be added to the pipeline in your
``/etc/swift/proxy-server.conf`` file just after any auth middleware. Also, the
configuration section for the ``staticweb`` middleware itself needs to be
added. For example::

    [DEFAULT]
    ...

    [pipeline:main]
    pipeline = catch_errors healthcheck proxy-logging cache ratelimit tempauth
               staticweb proxy-logging proxy-server

    ...

    [filter:staticweb]
    use = egg:swift#staticweb
    # Seconds to cache container x-container-meta-web-* header values.
    # cache_timeout = 300

Any publicly readable containers (for example, ``X-Container-Read: .r:*``, see
`acls`_ for more information on this) will be checked for
X-Container-Meta-Web-Index and X-Container-Meta-Web-Error header values::

    X-Container-Meta-Web-Index  <index.name>
    X-Container-Meta-Web-Error  <error.name.suffix>

If X-Container-Meta-Web-Index is set, any <index.name> files will be served
without having to specify the <index.name> part. For instance, setting
``X-Container-Meta-Web-Index: index.html`` will be able to serve the object
.../pseudo/path/index.html with just .../pseudo/path or .../pseudo/path/

If X-Container-Meta-Web-Error is set, any errors (currently just 401
Unauthorized and 404 Not Found) will instead serve the
.../<status.code><error.name.suffix> object. For instance, setting
``X-Container-Meta-Web-Error: error.html`` will serve .../404error.html for
requests for paths not found.

For pseudo paths that have no <index.name>, this middleware can serve HTML file
listings if you set the ``X-Container-Meta-Web-Listings: true`` metadata item
on the container.

If listings are enabled, the listings can have a custom style sheet by setting
the X-Container-Meta-Web-Listings-CSS header. For instance, setting
``X-Container-Meta-Web-Listings-CSS: listing.css`` will make listings link to
the .../listing.css style sheet. If you "view source" in your browser on a
listing page, you will see the well defined document structure that can be
styled.

The content-type of directory marker objects can be modified by setting
the ``X-Container-Meta-Web-Directory-Type`` header.  If the header is not set,
application/directory is used by default.  Directory marker objects are
0-byte objects that represent directories to create a simulated hierarchical
structure.

Example usage of this middleware via ``swift``:

    Make the container publicly readable::

        swift post -r '.r:*' container

    You should be able to get objects directly, but no index.html resolution or
    listings.

    Set an index file directive::

        swift post -m 'web-index:index.html' container

    You should be able to hit paths that have an index.html without needing to
    type the index.html part.

    Turn on listings::

        swift post -m 'web-listings: true' container

    Now you should see object listings for paths and pseudo paths that have no
    index.html.

    Enable a custom listings style sheet::

        swift post -m 'web-listings-css:listings.css' container

    Set an error file::

        swift post -m 'web-error:error.html' container

    Now 401's should load 401error.html, 404's should load 404error.html, etc.

    Set Content-Type of directory marker object::

        swift post -m 'web-directory-type:text/directory' container

    Now 0-byte objects with a content-type of text/directory will be treated
    as directories rather than objects.
"""


import cgi
import time
from urllib import quote as urllib_quote


from swift.common.utils import cache_from_env, human_readable, split_path, \
    config_true_value, json
from swift.common.wsgi import make_pre_authed_env, make_pre_authed_request, \
    WSGIContext
from swift.common.http import is_success, is_redirection, HTTP_NOT_FOUND
from swift.common.swob import Response, HTTPMovedPermanently, HTTPNotFound


def quote(value, safe='/'):
    """
    Patched version of urllib.quote that encodes utf-8 strings before quoting
    """
    if isinstance(value, unicode):
        value = value.encode('utf-8')
    return urllib_quote(value, safe)


def get_memcache_key(version, account, container):
    """
    This key's value is (index, error, listings, listings_css, dir_type)
    """
    return '/staticweb2/%s/%s/%s' % (version, account, container)


def get_compat_memcache_key(version, account, container):
    """
    This key's value is (index, error, listings, listings_css)

    TODO: This compat key and its use should be removed after the
          Havana OpenStack release.
    """
    return '/staticweb/%s/%s/%s' % (version, account, container)


class _StaticWebContext(WSGIContext):
    """
    The Static Web WSGI middleware filter; serves container data as a
    static web site. See `staticweb`_ for an overview.

    This _StaticWebContext is used by StaticWeb with each request
    that might need to be handled to make keeping contextual
    information about the request a bit simpler than storing it in
    the WSGI env.
    """

    def __init__(self, staticweb, version, account, container, obj):
        WSGIContext.__init__(self, staticweb.app)
        self.version = version
        self.account = account
        self.container = container
        self.obj = obj
        self.app = staticweb.app
        self.cache_timeout = staticweb.cache_timeout
        self.agent = '%(orig)s StaticWeb'
        # Results from the last call to self._get_container_info.
        self._index = self._error = self._listings = self._listings_css = \
            self._dir_type = None

    def _error_response(self, response, env, start_response):
        """
        Sends the error response to the remote client, possibly resolving a
        custom error response body based on x-container-meta-web-error.

        :param response: The error response we should default to sending.
        :param env: The original request WSGI environment.
        :param start_response: The WSGI start_response hook.
        """
        if not self._error:
            start_response(self._response_status, self._response_headers,
                           self._response_exc_info)
            return response
        save_response_status = self._response_status
        save_response_headers = self._response_headers
        save_response_exc_info = self._response_exc_info
        resp = self._app_call(make_pre_authed_env(
            env, 'GET', '/%s/%s/%s/%s%s' % (
                self.version, self.account, self.container,
                self._get_status_int(), self._error),
            self.agent, swift_source='SW'))
        if is_success(self._get_status_int()):
            start_response(save_response_status, self._response_headers,
                           self._response_exc_info)
            return resp
        start_response(save_response_status, save_response_headers,
                       save_response_exc_info)
        return response

    def _get_container_info(self, env):
        """
        Retrieves x-container-meta-web-index, x-container-meta-web-error,
        x-container-meta-web-listings, x-container-meta-web-listings-css,
        and x-container-meta-web-directory-type from memcache or from the
        cluster and stores the result in memcache and in self._index,
        self._error, self._listings, self._listings_css and self._dir_type.

        :param env: The WSGI environment dict.
        """
        self._index = self._error = self._listings = self._listings_css = \
            self._dir_type = None
        memcache_client = cache_from_env(env)
        if memcache_client:
            cached_data = memcache_client.get(
                get_memcache_key(self.version, self.account, self.container))
            if cached_data:
                (self._index, self._error, self._listings, self._listings_css,
                 self._dir_type) = cached_data
                return
            else:
                cached_data = memcache_client.get(
                    get_compat_memcache_key(
                        self.version, self.account, self.container))
                if cached_data:
                    (self._index, self._error, self._listings,
                     self._listings_css) = cached_data
                    self._dir_type = ''
                    return
        resp = make_pre_authed_request(
            env, 'HEAD', '/%s/%s/%s' % (
                self.version, self.account, self.container),
            agent=self.agent, swift_source='SW').get_response(self.app)
        if is_success(resp.status_int):
            self._index = \
                resp.headers.get('x-container-meta-web-index', '').strip()
            self._error = \
                resp.headers.get('x-container-meta-web-error', '').strip()
            self._listings = \
                resp.headers.get('x-container-meta-web-listings', '').strip()
            self._listings_css = \
                resp.headers.get('x-container-meta-web-listings-css',
                                 '').strip()
            self._dir_type = \
                resp.headers.get('x-container-meta-web-directory-type',
                                 '').strip()
            if memcache_client:
                memcache_client.set(
                    get_memcache_key(
                        self.version, self.account, self.container),
                    (self._index, self._error, self._listings,
                     self._listings_css, self._dir_type),
                    time=self.cache_timeout)

    def _listing(self, env, start_response, prefix=None):
        """
        Sends an HTML object listing to the remote client.

        :param env: The original WSGI environment dict.
        :param start_response: The original WSGI start_response hook.
        :param prefix: Any prefix desired for the container listing.
        """
        if not config_true_value(self._listings):
            resp = HTTPNotFound()(env, self._start_response)
            return self._error_response(resp, env, start_response)
        tmp_env = make_pre_authed_env(
            env, 'GET', '/%s/%s/%s' % (
                self.version, self.account, self.container),
            self.agent, swift_source='SW')
        tmp_env['QUERY_STRING'] = 'delimiter=/&format=json'
        if prefix:
            tmp_env['QUERY_STRING'] += '&prefix=%s' % quote(prefix)
        else:
            prefix = ''
        resp = self._app_call(tmp_env)
        if not is_success(self._get_status_int()):
            return self._error_response(resp, env, start_response)
        listing = None
        body = ''.join(resp)
        if body:
            listing = json.loads(body)
        if not listing:
            resp = HTTPNotFound()(env, self._start_response)
            return self._error_response(resp, env, start_response)
        headers = {'Content-Type': 'text/html; charset=UTF-8'}
        body = '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 ' \
               'Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">\n' \
               '<html>\n' \
               ' <head>\n' \
               '  <title>Listing of %s</title>\n' % \
               cgi.escape(env['PATH_INFO'])
        if self._listings_css:
            body += '  <link rel="stylesheet" type="text/css" ' \
                    'href="%s" />\n' % (self._build_css_path(prefix))
        else:
            body += '  <style type="text/css">\n' \
                    '   h1 {font-size: 1em; font-weight: bold;}\n' \
                    '   th {text-align: left; padding: 0px 1em 0px 1em;}\n' \
                    '   td {padding: 0px 1em 0px 1em;}\n' \
                    '   a {text-decoration: none;}\n' \
                    '  </style>\n'
        body += ' </head>\n' \
                ' <body>\n' \
                '  <h1 id="title">Listing of %s</h1>\n' \
                '  <table id="listing">\n' \
                '   <tr id="heading">\n' \
                '    <th class="colname">Name</th>\n' \
                '    <th class="colsize">Size</th>\n' \
                '    <th class="coldate">Date</th>\n' \
                '   </tr>\n' % \
                cgi.escape(env['PATH_INFO'])
        if prefix:
            body += '   <tr id="parent" class="item">\n' \
                    '    <td class="colname"><a href="../">../</a></td>\n' \
                    '    <td class="colsize">&nbsp;</td>\n' \
                    '    <td class="coldate">&nbsp;</td>\n' \
                    '   </tr>\n'
        for item in listing:
            if 'subdir' in item:
                if isinstance(item['subdir'], unicode):
                    subdir = item['subdir'].encode('utf-8')
                else:
                    subdir = item['subdir']
                if prefix:
                    subdir = subdir[len(prefix):]
                body += '   <tr class="item subdir">\n' \
                        '    <td class="colname"><a href="%s">%s</a></td>\n' \
                        '    <td class="colsize">&nbsp;</td>\n' \
                        '    <td class="coldate">&nbsp;</td>\n' \
                        '   </tr>\n' % \
                        (quote(subdir), cgi.escape(subdir))
        for item in listing:
            if 'name' in item:
                if isinstance(item['name'], unicode):
                    name = item['name'].encode('utf-8')
                else:
                    name = item['name']
                if prefix:
                    name = name[len(prefix):]
                body += '   <tr class="item %s">\n' \
                        '    <td class="colname"><a href="%s">%s</a></td>\n' \
                        '    <td class="colsize">%s</td>\n' \
                        '    <td class="coldate">%s</td>\n' \
                        '   </tr>\n' % \
                        (' '.join('type-' + cgi.escape(t.lower(), quote=True)
                                  for t in item['content_type'].split('/')),
                         quote(name), cgi.escape(name),
                         human_readable(item['bytes']),
                         cgi.escape(item['last_modified']).split('.')[0].
                            replace('T', ' '))
        body += '  </table>\n' \
                ' </body>\n' \
                '</html>\n'
        resp = Response(headers=headers, body=body)
        return resp(env, start_response)

    def _build_css_path(self, prefix=''):
        """
        Constructs a relative path from a given prefix within the container.
        URLs and paths starting with '/' are not modified.

        :param prefix: The prefix for the container listing.
        """
        if self._listings_css.startswith(('/', 'http://', 'https://')):
            css_path = quote(self._listings_css, ':/')
        else:
            css_path = '../' * prefix.count('/') + quote(self._listings_css)
        return css_path

    def handle_container(self, env, start_response):
        """
        Handles a possible static web request for a container.

        :param env: The original WSGI environment dict.
        :param start_response: The original WSGI start_response hook.
        """
        self._get_container_info(env)
        if not self._listings and not self._index:
            if config_true_value(env.get('HTTP_X_WEB_MODE', 'f')):
                return HTTPNotFound()(env, start_response)
            return self.app(env, start_response)
        if env['PATH_INFO'][-1] != '/':
            resp = HTTPMovedPermanently(
                location=(env['PATH_INFO'] + '/'))
            return resp(env, start_response)
        if not self._index:
            return self._listing(env, start_response)
        tmp_env = dict(env)
        tmp_env['HTTP_USER_AGENT'] = \
            '%s StaticWeb' % env.get('HTTP_USER_AGENT')
        tmp_env['swift.source'] = 'SW'
        tmp_env['PATH_INFO'] += self._index
        resp = self._app_call(tmp_env)
        status_int = self._get_status_int()
        if status_int == HTTP_NOT_FOUND:
            return self._listing(env, start_response)
        elif not is_success(self._get_status_int()) or \
                not is_redirection(self._get_status_int()):
            return self._error_response(resp, env, start_response)
        start_response(self._response_status, self._response_headers,
                       self._response_exc_info)
        return resp

    def handle_object(self, env, start_response):
        """
        Handles a possible static web request for an object. This object could
        resolve into an index or listing request.

        :param env: The original WSGI environment dict.
        :param start_response: The original WSGI start_response hook.
        """
        tmp_env = dict(env)
        tmp_env['HTTP_USER_AGENT'] = \
            '%s StaticWeb' % env.get('HTTP_USER_AGENT')
        tmp_env['swift.source'] = 'SW'
        resp = self._app_call(tmp_env)
        status_int = self._get_status_int()
        self._get_container_info(env)
        if is_success(status_int) or is_redirection(status_int):
            # Treat directory marker objects as not found
            if not self._dir_type:
                self._dir_type = 'application/directory'
            content_length = self._response_header_value('content-length')
            content_length = int(content_length) if content_length else 0
            if self._response_header_value('content-type') == self._dir_type \
                    and content_length <= 1:
                status_int = HTTP_NOT_FOUND
            else:
                start_response(self._response_status, self._response_headers,
                               self._response_exc_info)
                return resp
        if status_int != HTTP_NOT_FOUND:
            # Retaining the previous code's behavior of not using custom error
            # pages for non-404 errors.
            self._error = None
            return self._error_response(resp, env, start_response)
        if not self._listings and not self._index:
            return self.app(env, start_response)
        status_int = HTTP_NOT_FOUND
        if self._index:
            tmp_env = dict(env)
            tmp_env['HTTP_USER_AGENT'] = \
                '%s StaticWeb' % env.get('HTTP_USER_AGENT')
            tmp_env['swift.source'] = 'SW'
            if tmp_env['PATH_INFO'][-1] != '/':
                tmp_env['PATH_INFO'] += '/'
            tmp_env['PATH_INFO'] += self._index
            resp = self._app_call(tmp_env)
            status_int = self._get_status_int()
            if is_success(status_int) or is_redirection(status_int):
                if env['PATH_INFO'][-1] != '/':
                    resp = HTTPMovedPermanently(
                        location=env['PATH_INFO'] + '/')
                    return resp(env, start_response)
                start_response(self._response_status, self._response_headers,
                               self._response_exc_info)
                return resp
        if status_int == HTTP_NOT_FOUND:
            if env['PATH_INFO'][-1] != '/':
                tmp_env = make_pre_authed_env(
                    env, 'GET', '/%s/%s/%s' % (
                        self.version, self.account, self.container),
                    self.agent, swift_source='SW')
                tmp_env['QUERY_STRING'] = 'limit=1&format=json&delimiter' \
                    '=/&limit=1&prefix=%s' % quote(self.obj + '/')
                resp = self._app_call(tmp_env)
                body = ''.join(resp)
                if not is_success(self._get_status_int()) or not body or \
                        not json.loads(body):
                    resp = HTTPNotFound()(env, self._start_response)
                    return self._error_response(resp, env, start_response)
                resp = HTTPMovedPermanently(location=env['PATH_INFO'] + '/')
                return resp(env, start_response)
            return self._listing(env, start_response, self.obj)


class StaticWeb(object):
    """
    The Static Web WSGI middleware filter; serves container data as a static
    web site. See `staticweb`_ for an overview.

    The proxy logs created for any subrequests made will have swift.source set
    to "SW".

    :param app: The next WSGI application/filter in the paste.deploy pipeline.
    :param conf: The filter configuration dict.
    """

    def __init__(self, app, conf):
        #: The next WSGI application/filter in the paste.deploy pipeline.
        self.app = app
        #: The filter configuration dict.
        self.conf = conf
        #: The seconds to cache the x-container-meta-web-* headers.,
        self.cache_timeout = int(conf.get('cache_timeout', 300))

    def __call__(self, env, start_response):
        """
        Main hook into the WSGI paste.deploy filter/app pipeline.

        :param env: The WSGI environment dict.
        :param start_response: The WSGI start_response hook.
        """
        env['staticweb.start_time'] = time.time()
        try:
            (version, account, container, obj) = \
                split_path(env['PATH_INFO'], 2, 4, True)
        except ValueError:
            return self.app(env, start_response)
        if env['REQUEST_METHOD'] in ('PUT', 'POST') and container and not obj:
            memcache_client = cache_from_env(env)
            if memcache_client:
                memcache_client.delete(
                    get_memcache_key(version, account, container))
                memcache_client.delete(
                    get_compat_memcache_key(version, account, container))
            return self.app(env, start_response)
        if env['REQUEST_METHOD'] not in ('HEAD', 'GET'):
            return self.app(env, start_response)
        if env.get('REMOTE_USER') and \
                not config_true_value(env.get('HTTP_X_WEB_MODE', 'f')):
            return self.app(env, start_response)
        if not container:
            return self.app(env, start_response)
        context = _StaticWebContext(self, version, account, container, obj)
        if obj:
            return context.handle_object(env, start_response)
        return context.handle_container(env, start_response)


def filter_factory(global_conf, **local_conf):
    """ Returns a Static Web WSGI filter for use with paste.deploy. """
    conf = global_conf.copy()
    conf.update(local_conf)

    def staticweb_filter(app):
        return StaticWeb(app, conf)
    return staticweb_filter
