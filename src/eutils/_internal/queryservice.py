# -*- coding: utf-8 -*-

"""provide cached and throttled querying of `NCBI E-utilities
<http://www.ncbi.nlm.nih.gov/books/NBK25499/>`_.

QueryService defaults to returning XML documents only. This behavior
may be controlled upon instantiation by setting default_args.

::

    # create an instance of QueryService
    >> qs = QueryService()

    # get xml for database info (in this case, a list of available database)
    >> result = qs.einfo()

    # execute a search using an NCBI query against the gene database
    >> result = qs.esearch({"db": "gene", "term": "VEGF AND human[organism]"})

    # get xml doc for gene id=7157
    >> result = qs.efetch({"db": "gene", "id": 7157})

"""

from __future__ import absolute_import, division, print_function, unicode_literals

import hashlib
import logging
import os
import pickle
import time

import lxml.etree
import requests

from .sqlitecache import SQLiteCache
from .exceptions import EutilsRequestError, EutilsNCBIError


_logger = logging.getLogger(__name__)

url_base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
default_default_args = {"retmode": "xml", "usehistory": "y", "restart": 0, "retmax": 10000}
default_tool = __package__
default_email = "biocommons-dev@googlegroups.com"
default_cache_path = os.path.join(os.path.expanduser("~"), ".cache", "eutils-cache.db")


class QueryService(object):

    """*provides throttled and cached querying of NCBI E-utilities services*

    QueryService has three functions:

    * construct URLs appropriate for eutils endpoints

    * throttle queries per NCBI guidelines

    * cache results in persistent cache (sqlite)

    QueryService works with any valid query arguments, passed as
    dictionaries.

    Implemented interfaces:

        * esearch
        * efetch
        * elink
        * einfo
        * esummary

    Implementing other query modes should be straightforward.

    See also  
        * http://www.ncbi.nlm.nih.gov/books/NBK25500/ :the NCBI's Entrez Programming Utilities Help
        * http://www.ncbi.nlm.nih.gov/books/NBK25499/ :NCBI E-utilities

    """

    def __init__(self,
                 email=default_email,
                 cache=False,
                 default_args=default_default_args,
                 request_interval=None,
                 tool=default_tool,
                 api_key=None
                 ):
        """
        :param str email: email of user (for abuse reports)
        :param str cache: if True, cache at ~/.cache/eutils-db.sqlite; if string, cache there; if False, don't cache
        :param dict default_args: dictionary of query args that should accompany all requests
        :param request_interval: seconds between requests; default: auto-select based on API key
        :type request_interval: int or a callable returning an int
        :param str api_key: api key assigned by NCBI
        :param str tool: name of client
        :rtype: None
        :raises OSError: if sqlite file can't be opened

        """

        self.default_args = default_args
        self.email = email
        self.tool = tool
        self.api_key = api_key

        if request_interval is not None:
            _logger.warning("eutils QueryService: request_interval no longer supported; ignoring passed parameter")

        if self.api_key is None:
            requests_per_second = 3
            _logger.warning("No NCBI API key provided; throttling to {} requests/second; see "
                         "https://ncbiinsights.ncbi.nlm.nih.gov/2017/11/02/new-api-keys-for-the-e-utilities/".format(
                             requests_per_second))
        else:
            requests_per_second = 10
            _logger.info("Using NCBI API key; throttling to {} requests/second".format(requests_per_second))

        self.request_interval = 1.0 / requests_per_second

        self._last_request_clock = 0
        self._ident_args = {"tool": tool, "email": email}
        self._request_count = 0

        if cache is True:
            cache_path = default_cache_path
        elif cache:
            cache_path = cache  # better act like a path string
        else:
            cache_path = False
        self._cache = SQLiteCache(cache_path) if cache_path else None


    def efetch(self, args):
        """
        execute a cached, throttled efetch query

        :param dict args: dict of query items
        :returns: content of reply
        :rtype: str
        :raises EutilsRequestError: when NCBI replies, but the request failed (e.g., bogus database name)

        """
        return self._query("/efetch.fcgi", args)

    def einfo(self, args=None):
        """
        execute a NON-cached, throttled einfo query

        einfo.fcgi?db=<database>

        Input: Entrez database (&db) or None (returns info on all Entrez databases)

        Output: XML containing database statistics

        Example: Find database statistics for Entrez Protein.

            QueryService.einfo({"db": "protein"})

        Equivalent HTTP request:

            https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi?db=protein

        :param dict args: dict of query items (optional)
        :returns: content of reply
        :rtype: str
        :raises EutilsRequestError: when NCBI replies, but the request failed (e.g., bogus database name)

        """
        if args is None:
            args = {}
        return self._query("/einfo.fcgi", args, skip_cache=True)

    def esearch(self, args):
        """
        execute a cached, throttled esearch query

        :param dict args: dict of query items, containing at least "db" and "term" keys
        :returns: content of reply
        :rtype: str
        :raises EutilsRequestError: when NCBI replies, but the request failed (e.g., bogus database name)

        """
        return self._query("/esearch.fcgi", args)

    def elink(self, args):
        """
        execute a cached, throttled elink query

        Input: List of UIDs (&id); Source Entrez database (&dbfrom); Destination Entrez database (&db)

        Output: XML containing linked UIDs from source and destination databases

        Example: Find one set of Gene IDs linked to nuccore GIs 34577062 and 24475906

            QueryService.elink({"dbfrom": "nuccore", "db": "gene", "id": "34577062,24475906"})

        Equivalent HTTP request:

            https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=nuccore&db=gene&id=34577062,24475906

        :param dict args: dict of query items containing at least the "db", "dbfrom", and "id" keys.
        :returns: content of reply
        :rtype: str
        :raises EutilsRequestError: when NCBI replies, but the request failed (e.g., bogus database name)

        """
        return self._query("/elink.fcgi", args)

    def esummary(self, args):
        """
        execute a cached, throttled esummary query

        Input: List of UIDs (&id); Entrez database (&db)

        Output: XML document summary for requested ID(s) [comma-separated]

        Example: 
        
            QueryService.esummary({ "db": "medgen", "id": 134 })

        Equivalent HTTP request:

            https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=medgen&id=134

        :param dict args: dict of query items containing at least "db" and "id" keys.
        :returns: content of reply
        :rtype: str
        :raises EutilsRequestError: when NCBI replies, but the request failed (e.g., bogus database name)

        """
        return self._query("/esummary.fcgi", args)


    ############################################################################
    ## Internals
    def _query(self, path, args=None, skip_cache=False, skip_sleep=False):
        """return results for a NCBI query, possibly from the cache

        :param: path: relative query path (e.g., "einfo.fcgi")
        :param: args: dictionary of query args
        :param: skip_cache: whether to bypass the cache on reading
        :param: skip_sleep: whether to bypass query throttling
        :rtype: xml string

        The args are joined with args required by NCBI (tool and email
        address) and with the default args declared when instantiating
        the client.
        """
        if args is None:
            args = {}        
        def _cacheable(r):
            """return False if r shouldn't be cached (contains a no-cache meta
            line); True otherwise"""
            return not ("no-cache" in r  # obviate parsing, maybe
                        and lxml.etree.XML(r).xpath("//meta/@content='no-cache'"))
        
        # cache key: the key associated with this endpoint and args The
        # key intentionally excludes the identifying args (tool and email)
        # and is independent of the request method (GET/POST) args are
        # sorted for canonicalization

        url = url_base + path

        # next 3 lines converted by 2to3 -nm
        defining_args = dict(list(self.default_args.items()) + list(args.items()))
        full_args = dict(list(self._ident_args.items()) + list(defining_args.items()))
        cache_key = hashlib.md5(pickle.dumps((url, sorted(defining_args.items())))).hexdigest()

        sqas = ";".join([k + "=" + str(v) for k, v in sorted(args.items())])
        full_args_str = ";".join([k + "=" + str(v) for k, v in sorted(full_args.items())])

        logging.debug("CACHE:" + str(skip_cache) + "//" + str(self._cache))
        if not skip_cache and self._cache:
            try:
                v = self._cache[cache_key]
                _logger.debug("cache hit for key {cache_key} ({url}, {sqas}) ".format(
                    cache_key=cache_key,
                    url=url,
                    sqas=sqas))
                return v
            except KeyError:
                _logger.debug("cache miss for key {cache_key} ({url}, {sqas}) ".format(
                    cache_key=cache_key,
                    url=url,
                    sqas=sqas))
                pass

        if self.api_key:
            url += "?api_key={self.api_key}".format(self=self)

        # --

        if not skip_sleep:
            req_int = self.request_interval
            sleep_time = req_int - (time.monotonic() - self._last_request_clock)
            if sleep_time > 0:
                _logger.debug("sleeping {sleep_time:.3f}".format(sleep_time=sleep_time))
                time.sleep(sleep_time)

        r = requests.post(url, full_args)
        self._last_request_clock = time.monotonic()
        _logger.debug("post({url}, {fas}): {r.status_code} {r.reason}, {len})".format(
            url=url,
            fas=full_args_str,
            r=r,
            len=len(r.text)))

        if not r.ok:
            # TODO: discriminate between types of errors
            if r.headers["Content-Type"] == "application/json":
                json = r.json()
                raise EutilsRequestError('{r.reason} ({r.status_code}): {error}'.format(r=r, error=json["error"]))
            try:
                xml = lxml.etree.fromstring(r.text.encode("utf-8"))
                errornode = xml.find("ERROR")
                errormsg = errornode.text if errornode else "Unknown Error"
                raise EutilsRequestError("{r.reason} ({r.status_code}): {error}".format(r=r, error=errormsg))
            except Exception as ex:
                raise EutilsNCBIError("Error parsing response object from NCBI: {}".format(ex))

        if any(bad_word in r.text for bad_word in ["<error>", "<ERROR>"]):
            if r.text is not None:
                try:
                    xml = lxml.etree.fromstring(r.text.encode("utf-8"))
                    raise EutilsRequestError("{r.reason} ({r.status_code}): {error}".format(r=r, error=xml.find("ERROR").text))
                except Exception as ex:
                    raise EutilsNCBIError("Error parsing response object from NCBI: {}".format(ex))

        if '<h1 class="error">Access Denied</h1>' in r.text:
            raise EutilsRequestError("Access Denied: {url}".format(url=url))

        if self._cache and _cacheable(r.text):
            # N.B. we cache results even when skip_cache (read) is true
            self._cache[cache_key] = r.content
            _logger.info("cached results for key {cache_key} ({url}, {sqas}) ".format(
                cache_key=cache_key,
                url=url,
                sqas=sqas))

        return r.content


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    qs = QueryService()
    r = qs.einfo({"db": "protein"})
    r = qs.efetch({"db": "protein", "id": "319655736"})


# <LICENSE>
# Copyright 2015 eutils Committers
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
# http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing
# permissions and limitations under the License.
# </LICENSE>
