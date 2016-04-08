#!/usr/bin/python

# Usage: scripts/maint/updateFallbackDirs.py > src/or/fallback_dirs.inc
# Needs stem available in your PYTHONPATH, or just ln -s ../stem/stem .
#
# Then read the generated list to ensure no-one slipped anything funny into
# their name or contactinfo

# Script by weasel, April 2015
# Portions by gsathya & karsten, 2013
# https://trac.torproject.org/projects/tor/attachment/ticket/8374/dir_list.2.py
# Modifications by teor, 2015

import StringIO
import string
import re
import datetime
import gzip
import os.path
import json
import math
import sys
import urllib
import urllib2
import hashlib
import dateutil.parser
# bson_lazy provides bson
#from bson import json_util
import copy

from stem.descriptor.remote import DescriptorDownloader

import logging
# INFO tells you why each relay was included or excluded
# WARN tells you about potential misconfigurations
logging.basicConfig(level=logging.WARNING)

## Top-Level Configuration

# Output all candidate fallbacks, or only output selected fallbacks?
OUTPUT_CANDIDATES = False

# Perform DirPort checks over IPv4?
# Change this to False if IPv4 doesn't work for you, or if you don't want to
# download a consensus for each fallback
# Don't check ~1000 candidates when OUTPUT_CANDIDATES is True
PERFORM_IPV4_DIRPORT_CHECKS = False if OUTPUT_CANDIDATES else True

# Perform DirPort checks over IPv6?
# If you know IPv6 works for you, set this to True
# This will exclude IPv6 relays without an IPv6 DirPort configured
# So it's best left at False until #18394 is implemented
# Don't check ~1000 candidates when OUTPUT_CANDIDATES is True
PERFORM_IPV6_DIRPORT_CHECKS = False if OUTPUT_CANDIDATES else False

# Output matching ContactInfo in fallbacks list or the blacklist?
# Useful if you're trying to contact operators
CONTACT_COUNT = True if OUTPUT_CANDIDATES else False
CONTACT_BLACKLIST_COUNT = True if OUTPUT_CANDIDATES else False

## OnionOO Settings

ONIONOO = 'https://onionoo.torproject.org/'
#ONIONOO = 'https://onionoo.thecthulhu.com/'

# Don't bother going out to the Internet, just use the files available locally,
# even if they're very old
LOCAL_FILES_ONLY = False

## Whitelist / Blacklist Filter Settings

# The whitelist contains entries that are included if all attributes match
# (IPv4, dirport, orport, id, and optionally IPv6 and IPv6 orport)
# The blacklist contains (partial) entries that are excluded if any
# sufficiently specific group of attributes matches:
# IPv4 & DirPort
# IPv4 & ORPort
# ID
# IPv6 & DirPort
# IPv6 & IPv6 ORPort
# If neither port is included in the blacklist, the entire IP address is
# blacklisted.

# What happens to entries in neither list?
# When True, they are included, when False, they are excluded
INCLUDE_UNLISTED_ENTRIES = True if OUTPUT_CANDIDATES else False

# If an entry is in both lists, what happens?
# When True, it is excluded, when False, it is included
BLACKLIST_EXCLUDES_WHITELIST_ENTRIES = True

WHITELIST_FILE_NAME = 'scripts/maint/fallback.whitelist'
BLACKLIST_FILE_NAME = 'scripts/maint/fallback.blacklist'

# The number of bytes we'll read from a filter file before giving up
MAX_LIST_FILE_SIZE = 1024 * 1024

## Eligibility Settings

# Reduced due to a bug in tor where a relay submits a 0 DirPort when restarted
# This causes OnionOO to (correctly) reset its stability timer
# This issue will be fixed in 0.2.7.7 and 0.2.8.2
# Until then, the CUTOFFs below ensure a decent level of stability.
ADDRESS_AND_PORT_STABLE_DAYS = 7
# What time-weighted-fraction of these flags must FallbackDirs
# Equal or Exceed?
CUTOFF_RUNNING = .95
CUTOFF_V2DIR = .95
CUTOFF_GUARD = .95
# What time-weighted-fraction of these flags must FallbackDirs
# Equal or Fall Under?
# .00 means no bad exits
PERMITTED_BADEXIT = .00

# older entries' weights are adjusted with ALPHA^(age in days)
AGE_ALPHA = 0.99

# this factor is used to scale OnionOO entries to [0,1]
ONIONOO_SCALE_ONE = 999.

## Fallback Count Limits

# The target for these parameters is 20% of the guards in the network
# This is around 200 as of October 2015
_FB_POG = 0.2
FALLBACK_PROPORTION_OF_GUARDS = None if OUTPUT_CANDIDATES else _FB_POG

# We want exactly 100 fallbacks for the initial release
# This gives us scope to add extra fallbacks to the list as needed
# Limit the number of fallbacks (eliminating lowest by advertised bandwidth)
MAX_FALLBACK_COUNT = None if OUTPUT_CANDIDATES else 100
# Emit a C #error if the number of fallbacks is below
MIN_FALLBACK_COUNT = 100

## Fallback Bandwidth Requirements

# Any fallback with the Exit flag has its bandwidth multipled by this fraction
# to make sure we aren't further overloading exits
# (Set to 1.0, because we asked that only lightly loaded exits opt-in,
# and the extra load really isn't that much for large relays.)
EXIT_BANDWIDTH_FRACTION = 1.0

# If a single fallback's bandwidth is too low, it's pointless adding it
# We expect fallbacks to handle an extra 30 kilobytes per second of traffic
# Make sure they can support a hundred times the expected extra load
# (Use 102.4 to make it come out nicely in MB/s)
# We convert this to a consensus weight before applying the filter,
# because all the bandwidth amounts are specified by the relay
MIN_BANDWIDTH = 102.4 * 30.0 * 1024.0

# Clients will time out after 30 seconds trying to download a consensus
# So allow fallback directories half that to deliver a consensus
# The exact download times might change based on the network connection
# running this script, but only by a few seconds
# There is also about a second of python overhead
CONSENSUS_DOWNLOAD_SPEED_MAX = 15.0
# If the relay fails a consensus check, retry the download
# This avoids delisting a relay due to transient network conditions
CONSENSUS_DOWNLOAD_RETRY = True

## Fallback Weights for Client Selection

# All fallback weights are equal, and set to the value below
# Authorities are weighted 1.0 by default
# Clients use these weights to select fallbacks and authorities at random
# If there are 100 fallbacks and 9 authorities:
#  - each fallback is chosen with probability 10.0/(10.0*100 + 1.0*9) ~= 0.99%
#  - each authority is chosen with probability 1.0/(10.0*100 + 1.0*9) ~= 0.09%
# A client choosing a bootstrap directory server will choose a fallback for
# 10.0/(10.0*100 + 1.0*9) * 100 = 99.1% of attempts, and an authority for
# 1.0/(10.0*100 + 1.0*9) * 9 = 0.9% of attempts.
# (This disregards the bootstrap schedules, where clients start by choosing
# from fallbacks & authoritites, then later choose from only authorities.)
FALLBACK_OUTPUT_WEIGHT = 10.0

## Parsing Functions

def parse_ts(t):
  return datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")

def remove_bad_chars(raw_string, bad_char_list):
  # Remove each character in the bad_char_list
  cleansed_string = raw_string
  for c in bad_char_list:
    cleansed_string = cleansed_string.replace(c, '')
  return cleansed_string

def cleanse_unprintable(raw_string):
  # Remove all unprintable characters
  cleansed_string = ''
  for c in raw_string:
    if (c in string.ascii_letters or c in string.digits
        or c in string.punctuation or c in string.whitespace):
      cleansed_string += c
  return cleansed_string

def cleanse_whitespace(raw_string):
  # Replace all whitespace characters with a space
  cleansed_string = raw_string
  for c in string.whitespace:
    cleansed_string = cleansed_string.replace(c, ' ')
  return cleansed_string

def cleanse_c_multiline_comment(raw_string):
  cleansed_string = raw_string
  # Embedded newlines should be removed by tor/onionoo, but let's be paranoid
  cleansed_string = cleanse_whitespace(cleansed_string)
  # ContactInfo and Version can be arbitrary binary data
  cleansed_string = cleanse_unprintable(cleansed_string)
  # Prevent a malicious / unanticipated string from breaking out
  # of a C-style multiline comment
  # This removes '/*' and '*/' and '//'
  bad_char_list = '*/'
  # Prevent a malicious string from using C nulls
  bad_char_list += '\0'
  # Be safer by removing bad characters entirely
  cleansed_string = remove_bad_chars(cleansed_string, bad_char_list)
  # Some compilers may further process the content of comments
  # There isn't much we can do to cover every possible case
  # But comment-based directives are typically only advisory
  return cleansed_string

def cleanse_c_string(raw_string):
  cleansed_string = raw_string
  # Embedded newlines should be removed by tor/onionoo, but let's be paranoid
  cleansed_string = cleanse_whitespace(cleansed_string)
  # ContactInfo and Version can be arbitrary binary data
  cleansed_string = cleanse_unprintable(cleansed_string)
  # Prevent a malicious address/fingerprint string from breaking out
  # of a C-style string
  bad_char_list = '"'
  # Prevent a malicious string from using escapes
  bad_char_list += '\\'
  # Prevent a malicious string from using C nulls
  bad_char_list += '\0'
  # Be safer by removing bad characters entirely
  cleansed_string = remove_bad_chars(cleansed_string, bad_char_list)
  # Some compilers may further process the content of strings
  # There isn't much we can do to cover every possible case
  # But this typically only results in changes to the string data
  return cleansed_string

## OnionOO Source Functions

# a dictionary of source metadata for each onionoo query we've made
fetch_source = {}

# register source metadata for 'what'
# assumes we only retrieve one document for each 'what'
def register_fetch_source(what, url, relays_published, version):
  fetch_source[what] = {}
  fetch_source[what]['url'] = url
  fetch_source[what]['relays_published'] = relays_published
  fetch_source[what]['version'] = version

# list each registered source's 'what'
def fetch_source_list():
  return sorted(fetch_source.keys())

# given 'what', provide a multiline C comment describing the source
def describe_fetch_source(what):
  desc = '/*'
  desc += '\n'
  desc += 'Onionoo Source: '
  desc += cleanse_c_multiline_comment(what)
  desc += ' Date: '
  desc += cleanse_c_multiline_comment(fetch_source[what]['relays_published'])
  desc += ' Version: '
  desc += cleanse_c_multiline_comment(fetch_source[what]['version'])
  desc += '\n'
  desc += 'URL: '
  desc += cleanse_c_multiline_comment(fetch_source[what]['url'])
  desc += '\n'
  desc += '*/'
  return desc

## File Processing Functions

def write_to_file(str, file_name, max_len):
  try:
    with open(file_name, 'w') as f:
      f.write(str[0:max_len])
  except EnvironmentError, error:
    logging.warning('Writing file %s failed: %d: %s'%
                    (file_name,
                     error.errno,
                     error.strerror)
                    )

def read_from_file(file_name, max_len):
  try:
    if os.path.isfile(file_name):
      with open(file_name, 'r') as f:
        return f.read(max_len)
  except EnvironmentError, error:
    logging.info('Loading file %s failed: %d: %s'%
                 (file_name,
                  error.errno,
                  error.strerror)
                 )
  return None

def load_possibly_compressed_response_json(response):
    if response.info().get('Content-Encoding') == 'gzip':
      buf = StringIO.StringIO( response.read() )
      f = gzip.GzipFile(fileobj=buf)
      return json.load(f)
    else:
      return json.load(response)

def load_json_from_file(json_file_name):
    # An exception here may be resolved by deleting the .last_modified
    # and .json files, and re-running the script
    try:
      with open(json_file_name, 'r') as f:
        return json.load(f)
    except EnvironmentError, error:
      raise Exception('Reading not-modified json file %s failed: %d: %s'%
                    (json_file_name,
                     error.errno,
                     error.strerror)
                    )

## OnionOO Functions

def datestr_to_datetime(datestr):
  # Parse datetimes like: Fri, 02 Oct 2015 13:34:14 GMT
  if datestr is not None:
    dt = dateutil.parser.parse(datestr)
  else:
    # Never modified - use start of epoch
    dt = datetime.datetime.utcfromtimestamp(0)
  # strip any timezone out (in case they're supported in future)
  dt = dt.replace(tzinfo=None)
  return dt

def onionoo_fetch(what, **kwargs):
  params = kwargs
  params['type'] = 'relay'
  #params['limit'] = 10
  params['first_seen_days'] = '%d-'%(ADDRESS_AND_PORT_STABLE_DAYS,)
  params['last_seen_days'] = '-7'
  params['flag'] = 'V2Dir'
  url = ONIONOO + what + '?' + urllib.urlencode(params)

  # Unfortunately, the URL is too long for some OS filenames,
  # but we still don't want to get files from different URLs mixed up
  base_file_name = what + '-' + hashlib.sha1(url).hexdigest()

  full_url_file_name = base_file_name + '.full_url'
  MAX_FULL_URL_LENGTH = 1024

  last_modified_file_name = base_file_name + '.last_modified'
  MAX_LAST_MODIFIED_LENGTH = 64

  json_file_name = base_file_name + '.json'

  if LOCAL_FILES_ONLY:
    # Read from the local file, don't write to anything
    response_json = load_json_from_file(json_file_name)
  else:
    # store the full URL to a file for debugging
    # no need to compare as long as you trust SHA-1
    write_to_file(url, full_url_file_name, MAX_FULL_URL_LENGTH)

    request = urllib2.Request(url)
    request.add_header('Accept-encoding', 'gzip')

    # load the last modified date from the file, if it exists
    last_mod_date = read_from_file(last_modified_file_name,
                                   MAX_LAST_MODIFIED_LENGTH)
    if last_mod_date is not None:
      request.add_header('If-modified-since', last_mod_date)

    # Parse last modified date
    last_mod = datestr_to_datetime(last_mod_date)

    # Not Modified and still recent enough to be useful
    # Onionoo / Globe used to use 6 hours, but we can afford a day
    required_freshness = datetime.datetime.utcnow()
    # strip any timezone out (to match dateutil.parser)
    required_freshness = required_freshness.replace(tzinfo=None)
    required_freshness -= datetime.timedelta(hours=24)

    # Make the OnionOO request
    response_code = 0
    try:
      response = urllib2.urlopen(request)
      response_code = response.getcode()
    except urllib2.HTTPError, error:
      response_code = error.code
      if response_code == 304: # not modified
        pass
      else:
        raise Exception("Could not get " + url + ": "
                        + str(error.code) + ": " + error.reason)

    if response_code == 200: # OK
      last_mod = datestr_to_datetime(response.info().get('Last-Modified'))

    # Check for freshness
    if last_mod < required_freshness:
      if last_mod_date is not None:
        # This check sometimes fails transiently, retry the script if it does
        date_message = "Outdated data: last updated " + last_mod_date
      else:
        date_message = "No data: never downloaded "
      raise Exception(date_message + " from " + url)

    # Process the data
    if response_code == 200: # OK

      response_json = load_possibly_compressed_response_json(response)

      with open(json_file_name, 'w') as f:
        # use the most compact json representation to save space
        json.dump(response_json, f, separators=(',',':'))

      # store the last modified date in its own file
      if response.info().get('Last-modified') is not None:
        write_to_file(response.info().get('Last-Modified'),
                      last_modified_file_name,
                      MAX_LAST_MODIFIED_LENGTH)

    elif response_code == 304: # Not Modified

      response_json = load_json_from_file(json_file_name)

    else: # Unexpected HTTP response code not covered in the HTTPError above
      raise Exception("Unexpected HTTP response code to " + url + ": "
                      + str(response_code))

  register_fetch_source(what,
                        url,
                        response_json['relays_published'],
                        response_json['version'])

  return response_json

def fetch(what, **kwargs):
  #x = onionoo_fetch(what, **kwargs)
  # don't use sort_keys, as the order of or_addresses is significant
  #print json.dumps(x, indent=4, separators=(',', ': '))
  #sys.exit(0)

  return onionoo_fetch(what, **kwargs)

## Fallback Candidate Class

class Candidate(object):
  CUTOFF_ADDRESS_AND_PORT_STABLE = (datetime.datetime.utcnow()
                            - datetime.timedelta(ADDRESS_AND_PORT_STABLE_DAYS))

  def __init__(self, details):
    for f in ['fingerprint', 'nickname', 'last_changed_address_or_port',
              'consensus_weight', 'or_addresses', 'dir_address']:
      if not f in details: raise Exception("Document has no %s field."%(f,))

    if not 'contact' in details:
      details['contact'] = None
    if not 'flags' in details or details['flags'] is None:
      details['flags'] = []
    if (not 'advertised_bandwidth' in details
        or details['advertised_bandwidth'] is None):
      # relays without advertised bandwdith have it calculated from their
      # consensus weight
      details['advertised_bandwidth'] = 0
    details['last_changed_address_or_port'] = parse_ts(
                                      details['last_changed_address_or_port'])
    self._data = details
    self._stable_sort_or_addresses()

    self._fpr = self._data['fingerprint']
    self._running = self._guard = self._v2dir = 0.
    self._split_dirport()
    self._compute_orport()
    if self.orport is None:
      raise Exception("Failed to get an orport for %s."%(self._fpr,))
    self._compute_ipv6addr()
    if self.ipv6addr is None:
      logging.debug("Failed to get an ipv6 address for %s."%(self._fpr,))

  def _stable_sort_or_addresses(self):
    # replace self._data['or_addresses'] with a stable ordering,
    # sorting the secondary addresses in string order
    # leave the received order in self._data['or_addresses_raw']
    self._data['or_addresses_raw'] = self._data['or_addresses']
    or_address_primary = self._data['or_addresses'][:1]
    # subsequent entries in the or_addresses array are in an arbitrary order
    # so we stabilise the addresses by sorting them in string order
    or_addresses_secondaries_stable = sorted(self._data['or_addresses'][1:])
    or_addresses_stable = or_address_primary + or_addresses_secondaries_stable
    self._data['or_addresses'] = or_addresses_stable

  def get_fingerprint(self):
    return self._fpr

  # is_valid_ipv[46]_address by gsathya, karsten, 2013
  @staticmethod
  def is_valid_ipv4_address(address):
    if not isinstance(address, (str, unicode)):
      return False

    # check if there are four period separated values
    if address.count(".") != 3:
      return False

    # checks that each value in the octet are decimal values between 0-255
    for entry in address.split("."):
      if not entry.isdigit() or int(entry) < 0 or int(entry) > 255:
        return False
      elif entry[0] == "0" and len(entry) > 1:
        return False  # leading zeros, for instance in "1.2.3.001"

    return True

  @staticmethod
  def is_valid_ipv6_address(address):
    if not isinstance(address, (str, unicode)):
      return False

    # remove brackets
    address = address[1:-1]

    # addresses are made up of eight colon separated groups of four hex digits
    # with leading zeros being optional
    # https://en.wikipedia.org/wiki/IPv6#Address_format

    colon_count = address.count(":")

    if colon_count > 7:
      return False  # too many groups
    elif colon_count != 7 and not "::" in address:
      return False  # not enough groups and none are collapsed
    elif address.count("::") > 1 or ":::" in address:
      return False  # multiple groupings of zeros can't be collapsed

    found_ipv4_on_previous_entry = False
    for entry in address.split(":"):
      # If an IPv6 address has an embedded IPv4 address,
      # it must be the last entry
      if found_ipv4_on_previous_entry:
        return False
      if not re.match("^[0-9a-fA-f]{0,4}$", entry):
        if not Candidate.is_valid_ipv4_address(entry):
          return False
        else:
          found_ipv4_on_previous_entry = True

    return True

  def _split_dirport(self):
    # Split the dir_address into dirip and dirport
    (self.dirip, _dirport) = self._data['dir_address'].split(':', 2)
    self.dirport = int(_dirport)

  def _compute_orport(self):
    # Choose the first ORPort that's on the same IPv4 address as the DirPort.
    # In rare circumstances, this might not be the primary ORPort address.
    # However, _stable_sort_or_addresses() ensures we choose the same one
    # every time, even if onionoo changes the order of the secondaries.
    self._split_dirport()
    self.orport = None
    for i in self._data['or_addresses']:
      if i != self._data['or_addresses'][0]:
        logging.debug('Secondary IPv4 Address Used for %s: %s'%(self._fpr, i))
      (ipaddr, port) = i.rsplit(':', 1)
      if (ipaddr == self.dirip) and Candidate.is_valid_ipv4_address(ipaddr):
        self.orport = int(port)
        return

  def _compute_ipv6addr(self):
    # Choose the first IPv6 address that uses the same port as the ORPort
    # Or, choose the first IPv6 address in the list
    # _stable_sort_or_addresses() ensures we choose the same IPv6 address
    # every time, even if onionoo changes the order of the secondaries.
    self.ipv6addr = None
    self.ipv6orport = None
    # Choose the first IPv6 address that uses the same port as the ORPort
    for i in self._data['or_addresses']:
      (ipaddr, port) = i.rsplit(':', 1)
      if (port == self.orport) and Candidate.is_valid_ipv6_address(ipaddr):
        self.ipv6addr = ipaddr
        self.ipv6orport = port
        return
    # Choose the first IPv6 address in the list
    for i in self._data['or_addresses']:
      (ipaddr, port) = i.rsplit(':', 1)
      if Candidate.is_valid_ipv6_address(ipaddr):
        self.ipv6addr = ipaddr
        self.ipv6orport = port
        return

  @staticmethod
  def _extract_generic_history(history, which='unknown'):
    # given a tree like this:
    #   {
    #     "1_month": {
    #         "count": 187,
    #         "factor": 0.001001001001001001,
    #         "first": "2015-02-27 06:00:00",
    #         "interval": 14400,
    #         "last": "2015-03-30 06:00:00",
    #         "values": [
    #             999,
    #             999
    #         ]
    #     },
    #     "1_week": {
    #         "count": 169,
    #         "factor": 0.001001001001001001,
    #         "first": "2015-03-23 07:30:00",
    #         "interval": 3600,
    #         "last": "2015-03-30 07:30:00",
    #         "values": [ ...]
    #     },
    #     "1_year": {
    #         "count": 177,
    #         "factor": 0.001001001001001001,
    #         "first": "2014-04-11 00:00:00",
    #         "interval": 172800,
    #         "last": "2015-03-29 00:00:00",
    #         "values": [ ...]
    #     },
    #     "3_months": {
    #         "count": 185,
    #         "factor": 0.001001001001001001,
    #         "first": "2014-12-28 06:00:00",
    #         "interval": 43200,
    #         "last": "2015-03-30 06:00:00",
    #         "values": [ ...]
    #     }
    #   },
    # extract exactly one piece of data per time interval,
    # using smaller intervals where available.
    #
    # returns list of (age, length, value) dictionaries.

    generic_history = []

    periods = history.keys()
    periods.sort(key = lambda x: history[x]['interval'])
    now = datetime.datetime.utcnow()
    newest = now
    for p in periods:
      h = history[p]
      interval = datetime.timedelta(seconds = h['interval'])
      this_ts = parse_ts(h['last'])

      if (len(h['values']) != h['count']):
        logging.warn('Inconsistent value count in %s document for %s'
                     %(p, which))
      for v in reversed(h['values']):
        if (this_ts <= newest):
          agt1 = now - this_ts
          agt2 = interval
          agetmp1 = (agt1.microseconds + (agt1.seconds + agt1.days * 24 * 3600)
                     * 10**6) / 10**6
          agetmp2 = (agt2.microseconds + (agt2.seconds + agt2.days * 24 * 3600)
                     * 10**6) / 10**6
          generic_history.append(
            { 'age': agetmp1,
              'length': agetmp2,
              'value': v
            })
          newest = this_ts
        this_ts -= interval

      if (this_ts + interval != parse_ts(h['first'])):
        logging.warn('Inconsistent time information in %s document for %s'
                     %(p, which))

    #print json.dumps(generic_history, sort_keys=True,
    #                  indent=4, separators=(',', ': '))
    return generic_history

  @staticmethod
  def _avg_generic_history(generic_history):
    a = []
    for i in generic_history:
      if i['age'] > (ADDRESS_AND_PORT_STABLE_DAYS * 24 * 3600):
        continue
      if (i['length'] is not None
          and i['age'] is not None
          and i['value'] is not None):
        w = i['length'] * math.pow(AGE_ALPHA, i['age']/(3600*24))
        a.append( (i['value'] * w, w) )

    sv = math.fsum(map(lambda x: x[0], a))
    sw = math.fsum(map(lambda x: x[1], a))

    if sw == 0.0:
      svw = 0.0
    else:
      svw = sv/sw
    return svw

  def _add_generic_history(self, history):
    periods = r['read_history'].keys()
    periods.sort(key = lambda x: r['read_history'][x]['interval'] )

    print periods

  def add_running_history(self, history):
    pass

  def add_uptime(self, uptime):
    logging.debug('Adding uptime %s.'%(self._fpr,))

    # flags we care about: Running, V2Dir, Guard
    if not 'flags' in uptime:
      logging.debug('No flags in document for %s.'%(self._fpr,))
      return

    for f in ['Running', 'Guard', 'V2Dir']:
      if not f in uptime['flags']:
        logging.debug('No %s in flags for %s.'%(f, self._fpr,))
        return

    running = self._extract_generic_history(uptime['flags']['Running'],
                                            '%s-Running'%(self._fpr))
    guard = self._extract_generic_history(uptime['flags']['Guard'],
                                          '%s-Guard'%(self._fpr))
    v2dir = self._extract_generic_history(uptime['flags']['V2Dir'],
                                          '%s-V2Dir'%(self._fpr))
    if 'BadExit' in uptime['flags']:
      badexit = self._extract_generic_history(uptime['flags']['BadExit'],
                                              '%s-BadExit'%(self._fpr))

    self._running = self._avg_generic_history(running) / ONIONOO_SCALE_ONE
    self._guard = self._avg_generic_history(guard) / ONIONOO_SCALE_ONE
    self._v2dir = self._avg_generic_history(v2dir) / ONIONOO_SCALE_ONE
    self._badexit = None
    if 'BadExit' in uptime['flags']:
      self._badexit = self._avg_generic_history(badexit) / ONIONOO_SCALE_ONE

  def is_candidate(self):
    must_be_running_now = (PERFORM_IPV4_DIRPORT_CHECKS
                           or PERFORM_IPV6_DIRPORT_CHECKS)
    if (must_be_running_now and not self.is_running()):
      logging.info('%s not a candidate: not running now, unable to check ' +
                   'DirPort consensus download', self._fpr)
      return False
    if (self._data['last_changed_address_or_port'] >
        self.CUTOFF_ADDRESS_AND_PORT_STABLE):
      logging.info('%s not a candidate: changed address/port recently (%s)',
                   self._fpr, self._data['last_changed_address_or_port'])
      return False
    if self._running < CUTOFF_RUNNING:
      logging.info('%s not a candidate: running avg too low (%lf)',
                   self._fpr, self._running)
      return False
    if self._v2dir < CUTOFF_V2DIR:
      logging.info('%s not a candidate: v2dir avg too low (%lf)',
                   self._fpr, self._v2dir)
      return False
    if self._badexit is not None and self._badexit > PERMITTED_BADEXIT:
      logging.info('%s not a candidate: badexit avg too high (%lf)',
                   self._fpr, self._badexit)
      return False
    # if the relay doesn't report a version, also exclude the relay
    if (not self._data.has_key('recommended_version')
        or not self._data['recommended_version']):
      logging.info('%s not a candidate: version not recommended', self._fpr)
      return False
    if self._guard < CUTOFF_GUARD:
      logging.info('%s not a candidate: guard avg too low (%lf)',
                   self._fpr, self._guard)
      return False
    if (not self._data.has_key('consensus_weight')
        or self._data['consensus_weight'] < 1):
      logging.info('%s not a candidate: consensus weight invalid', self._fpr)
      return False
    return True

  def is_in_whitelist(self, relaylist):
    """ A fallback matches if each key in the whitelist line matches:
          ipv4
          dirport
          orport
          id
          ipv6 address and port (if present)
        If the fallback has an ipv6 key, the whitelist line must also have
        it, and vice versa, otherwise they don't match. """
    for entry in relaylist:
      if  entry['id'] != self._fpr:
        # can't log here, every relay's fingerprint is compared to the entry
        continue
      if entry['ipv4'] != self.dirip:
        logging.info('%s is not in the whitelist: fingerprint matches, but ' +
                     'IPv4 (%s) does not match entry IPv4 (%s)',
                     self._fpr, self.dirip, entry['ipv4'])
        continue
      if int(entry['dirport']) != self.dirport:
        logging.info('%s is not in the whitelist: fingerprint matches, but ' +
                     'DirPort (%d) does not match entry DirPort (%d)',
                     self._fpr, self.dirport, int(entry['dirport']))
        continue
      if int(entry['orport']) != self.orport:
        logging.info('%s is not in the whitelist: fingerprint matches, but ' +
                     'ORPort (%d) does not match entry ORPort (%d)',
                     self._fpr, self.orport, int(entry['orport']))
        continue
      has_ipv6 = self.ipv6addr is not None and self.ipv6orport is not None
      if (entry.has_key('ipv6') and has_ipv6):
        ipv6 = self.ipv6addr + ':' + self.ipv6orport
        # if both entry and fallback have an ipv6 address, compare them
        if entry['ipv6'] != ipv6:
          logging.info('%s is not in the whitelist: fingerprint matches, ' +
                       'but IPv6 (%s) does not match entry IPv6 (%s)',
                       self._fpr, ipv6, entry['ipv6'])
          continue
      # if the fallback has an IPv6 address but the whitelist entry
      # doesn't, or vice versa, the whitelist entry doesn't match
      elif entry.has_key('ipv6') and not has_ipv6:
        logging.info('%s is not in the whitelist: fingerprint matches, but ' +
                     'it has no IPv6, and entry has IPv6 (%s)', self._fpr,
                     entry['ipv6'])
        logging.warning('%s excluded: has it lost its former IPv6 address %s?',
                        self._fpr, entry['ipv6'])
        continue
      elif not entry.has_key('ipv6') and has_ipv6:
        logging.info('%s is not in the whitelist: fingerprint matches, but ' +
                     'it has IPv6 (%s), and entry has no IPv6', self._fpr,
                     ipv6)
        logging.warning('%s excluded: has it gained an IPv6 address %s?',
                        self._fpr, ipv6)
        continue
      return True
    return False

  def is_in_blacklist(self, relaylist):
    """ A fallback matches a blacklist line if a sufficiently specific group
        of attributes matches:
          ipv4 & dirport
          ipv4 & orport
          id
          ipv6 & dirport
          ipv6 & ipv6 orport
        If the fallback and the blacklist line both have an ipv6 key,
        their values will be compared, otherwise, they will be ignored.
        If there is no dirport and no orport, the entry matches all relays on
        that ip. """
    for entry in relaylist:
      for key in entry:
        value = entry[key]
        if key == 'id' and value == self._fpr:
          logging.info('%s is in the blacklist: fingerprint matches',
                       self._fpr)
          return True
        if key == 'ipv4' and value == self.dirip:
          # if the dirport is present, check it too
          if entry.has_key('dirport'):
            if int(entry['dirport']) == self.dirport:
              logging.info('%s is in the blacklist: IPv4 (%s) and ' +
                           'DirPort (%d) match', self._fpr, self.dirip,
                           self.dirport)
              return True
          # if the orport is present, check it too
          elif entry.has_key('orport'):
            if int(entry['orport']) == self.orport:
              logging.info('%s is in the blacklist: IPv4 (%s) and ' +
                           'ORPort (%d) match', self._fpr, self.dirip,
                           self.orport)
              return True
          else:
            logging.info('%s is in the blacklist: IPv4 (%s) matches, and ' +
                         'entry has no DirPort or ORPort', self._fpr,
                         self.dirip)
            return True
        has_ipv6 = self.ipv6addr is not None and self.ipv6orport is not None
        ipv6 = (self.ipv6addr + ':' + self.ipv6orport) if has_ipv6 else None
        if (key == 'ipv6' and has_ipv6):
        # if both entry and fallback have an ipv6 address, compare them,
        # otherwise, disregard ipv6 addresses
          if value == ipv6:
            # if the dirport is present, check it too
            if entry.has_key('dirport'):
              if int(entry['dirport']) == self.dirport:
                logging.info('%s is in the blacklist: IPv6 (%s) and ' +
                             'DirPort (%d) match', self._fpr, ipv6,
                             self.dirport)
                return True
            # we've already checked the ORPort, it's part of entry['ipv6']
            else:
              logging.info('%s is in the blacklist: IPv6 (%s) matches, and' +
                           'entry has no DirPort', self._fpr, ipv6)
              return True
        elif (key == 'ipv6' or has_ipv6):
          # only log if the fingerprint matches but the IPv6 doesn't
          if entry.has_key('id') and entry['id'] == self._fpr:
            logging.info('%s skipping IPv6 blacklist comparison: relay ' +
                         'has%s IPv6%s, but entry has%s IPv6%s', self._fpr,
                         '' if has_ipv6 else ' no',
                         (' (' + ipv6 + ')') if has_ipv6 else  '',
                         '' if key == 'ipv6' else ' no',
                         (' (' + value + ')') if key == 'ipv6' else '')
            logging.warning('Has %s %s IPv6 address %s?', self._fpr,
                            'gained an' if has_ipv6 else 'lost its former',
                            ipv6 if has_ipv6 else value)
    return False

  def cw_to_bw_factor(self):
    # any relays with a missing or zero consensus weight are not candidates
    # any relays with a missing advertised bandwidth have it set to zero
    return self._data['advertised_bandwidth'] / self._data['consensus_weight']

  # since advertised_bandwidth is reported by the relay, it can be gamed
  # to avoid this, use the median consensus weight to bandwidth factor to
  # estimate this relay's measured bandwidth, and make that the upper limit
  def measured_bandwidth(self, median_cw_to_bw_factor):
    cw_to_bw= median_cw_to_bw_factor
    # Reduce exit bandwidth to make sure we're not overloading them
    if self.is_exit():
      cw_to_bw *= EXIT_BANDWIDTH_FRACTION
    measured_bandwidth = self._data['consensus_weight'] * cw_to_bw
    if self._data['advertised_bandwidth'] != 0:
      # limit advertised bandwidth (if available) to measured bandwidth
      return min(measured_bandwidth, self._data['advertised_bandwidth'])
    else:
      return measured_bandwidth

  def set_measured_bandwidth(self, median_cw_to_bw_factor):
    self._data['measured_bandwidth'] = self.measured_bandwidth(
                                                      median_cw_to_bw_factor)

  def is_exit(self):
    return 'Exit' in self._data['flags']

  def is_guard(self):
    return 'Guard' in self._data['flags']

  def is_running(self):
    return 'Running' in self._data['flags']

  @staticmethod
  def fallback_consensus_dl_speed(dirip, dirport, nickname, max_time):
    download_failed = False
    downloader = DescriptorDownloader()
    start = datetime.datetime.utcnow()
    # some directory mirrors respond to requests in ways that hang python
    # sockets, which is why we long this line here
    logging.info('Initiating consensus download from %s (%s:%d).', nickname,
                 dirip, dirport)
    # there appears to be about 1 second of overhead when comparing stem's
    # internal trace time and the elapsed time calculated here
    TIMEOUT_SLOP = 1.0
    try:
      downloader.get_consensus(endpoints = [(dirip, dirport)],
                               timeout = (max_time + TIMEOUT_SLOP),
                               validate = True,
                               retries = 0,
                               fall_back_to_authority = False).run()
    except Exception, stem_error:
      logging.debug('Unable to retrieve a consensus from %s: %s', nickname,
                    stem_error)
      status = 'error: "%s"' % (stem_error)
      level = logging.WARNING
      download_failed = True
    elapsed = (datetime.datetime.utcnow() - start).total_seconds()
    if elapsed > max_time:
      status = 'too slow'
      level = logging.WARNING
      download_failed = True
    else:
      status = 'ok'
      level = logging.DEBUG
    logging.log(level, 'Consensus download: %0.1fs %s from %s (%s:%d), ' +
                 'max download time %0.1fs.', elapsed, status, nickname,
                 dirip, dirport, max_time)
    return download_failed

  def fallback_consensus_dl_check(self):
    # include the relay if we're not doing a check, or we can't check (IPv6)
    ipv4_failed = False
    ipv6_failed = False
    if PERFORM_IPV4_DIRPORT_CHECKS:
      ipv4_failed = Candidate.fallback_consensus_dl_speed(self.dirip,
                                                self.dirport,
                                                self._data['nickname'],
                                                CONSENSUS_DOWNLOAD_SPEED_MAX)
    if self.ipv6addr is not None and PERFORM_IPV6_DIRPORT_CHECKS:
      # Clients assume the IPv6 DirPort is the same as the IPv4 DirPort
      ipv6_failed = Candidate.fallback_consensus_dl_speed(self.ipv6addr,
                                                self.dirport,
                                                self._data['nickname'],
                                                CONSENSUS_DOWNLOAD_SPEED_MAX)
    # Now retry the relay if it took too long the first time
    if (PERFORM_IPV4_DIRPORT_CHECKS and ipv4_failed
        and CONSENSUS_DOWNLOAD_RETRY):
      ipv4_failed = Candidate.fallback_consensus_dl_speed(self.dirip,
                                                self.dirport,
                                                self._data['nickname'],
                                                CONSENSUS_DOWNLOAD_SPEED_MAX)
    if (self.ipv6addr is not None and PERFORM_IPV6_DIRPORT_CHECKS
        and ipv6_failed and CONSENSUS_DOWNLOAD_RETRY):
      ipv6_failed = Candidate.fallback_consensus_dl_speed(self.ipv6addr,
                                                self.dirport,
                                                self._data['nickname'],
                                                CONSENSUS_DOWNLOAD_SPEED_MAX)
    return ((not ipv4_failed) and (not ipv6_failed))

  def fallbackdir_line(self, dl_speed_ok, fallbacks, prefilter_fallbacks):
    # /*
    # nickname
    # flags
    # [contact]
    # [identical contact counts]
    # */
    # "address:dirport orport=port id=fingerprint"
    # "[ipv6=addr:orport]"
    # "weight=FALLBACK_OUTPUT_WEIGHT",
    #
    # Multiline C comment
    s = '/*'
    s += '\n'
    s += cleanse_c_multiline_comment(self._data['nickname'])
    s += '\n'
    s += 'Flags: '
    s += cleanse_c_multiline_comment(' '.join(sorted(self._data['flags'])))
    s += '\n'
    if self._data['contact'] is not None:
      s += cleanse_c_multiline_comment(self._data['contact'])
      if CONTACT_COUNT or CONTACT_BLACKLIST_COUNT:
        fallback_count = len([f for f in fallbacks
                              if f._data['contact'] == self._data['contact']])
        if fallback_count > 1:
          s += '\n'
          s += '%d identical contacts listed' % (fallback_count)
      if CONTACT_BLACKLIST_COUNT:
        prefilter_count = len([f for f in prefilter_fallbacks
                               if f._data['contact'] == self._data['contact']])
        filter_count = prefilter_count - fallback_count
        if filter_count > 0:
          if fallback_count > 1:
            s += ' '
          else:
            s += '\n'
          s += '%d blacklisted' % (filter_count)
      s += '\n'
    s += '*/'
    s += '\n'
    # Comment out the fallback directory entry if it's too slow
    # See the debug output for which address and port is failing
    if not dl_speed_ok:
      s += '/* Consensus download failed or was too slow:\n'
    # Multi-Line C string with trailing comma (part of a string list)
    # This makes it easier to diff the file, and remove IPv6 lines using grep
    # Integers don't need escaping
    s += '"%s orport=%d id=%s"'%(
            cleanse_c_string(self._data['dir_address']),
            self.orport,
            cleanse_c_string(self._fpr))
    s += '\n'
    if self.ipv6addr is not None:
      s += '" ipv6=%s:%s"'%(
            cleanse_c_string(self.ipv6addr), cleanse_c_string(self.ipv6orport))
      s += '\n'
    s += '" weight=%d",'%(FALLBACK_OUTPUT_WEIGHT)
    if not dl_speed_ok:
      s += '\n'
      s += '*/'
    return s

## Fallback Candidate List Class

class CandidateList(dict):
  def __init__(self):
    pass

  def _add_relay(self, details):
    if not 'dir_address' in details: return
    c = Candidate(details)
    self[ c.get_fingerprint() ] = c

  def _add_uptime(self, uptime):
    try:
      fpr = uptime['fingerprint']
    except KeyError:
      raise Exception("Document has no fingerprint field.")

    try:
      c = self[fpr]
    except KeyError:
      logging.debug('Got unknown relay %s in uptime document.'%(fpr,))
      return

    c.add_uptime(uptime)

  def _add_details(self):
    logging.debug('Loading details document.')
    d = fetch('details',
        fields=('fingerprint,nickname,contact,last_changed_address_or_port,' +
                'consensus_weight,advertised_bandwidth,or_addresses,' +
                'dir_address,recommended_version,flags'))
    logging.debug('Loading details document done.')

    if not 'relays' in d: raise Exception("No relays found in document.")

    for r in d['relays']: self._add_relay(r)

  def _add_uptimes(self):
    logging.debug('Loading uptime document.')
    d = fetch('uptime')
    logging.debug('Loading uptime document done.')

    if not 'relays' in d: raise Exception("No relays found in document.")
    for r in d['relays']: self._add_uptime(r)

  def add_relays(self):
    self._add_details()
    self._add_uptimes()

  def count_guards(self):
    guard_count = 0
    for fpr in self.keys():
      if self[fpr].is_guard():
        guard_count += 1
    return guard_count

  # Find fallbacks that fit the uptime, stability, and flags criteria,
  # and make an array of them in self.fallbacks
  def compute_fallbacks(self):
    self.fallbacks = map(lambda x: self[x],
                         filter(lambda x: self[x].is_candidate(),
                                self.keys()))

  # sort fallbacks by their consensus weight to advertised bandwidth factor,
  # lowest to highest
  # used to find the median cw_to_bw_factor()
  def sort_fallbacks_by_cw_to_bw_factor(self):
    self.fallbacks.sort(key=lambda x: self[x].cw_to_bw_factor())

  # sort fallbacks by their measured bandwidth, highest to lowest
  # calculate_measured_bandwidth before calling this
  def sort_fallbacks_by_measured_bandwidth(self):
    self.fallbacks.sort(key=lambda x: self[x].self._data['measured_bandwidth'],
                        reverse=True)

  @staticmethod
  def load_relaylist(file_name):
    """ Read each line in the file, and parse it like a FallbackDir line:
        an IPv4 address and optional port:
          <IPv4 address>:<port>
        which are parsed into dictionary entries:
          ipv4=<IPv4 address>
          dirport=<port>
        followed by a series of key=value entries:
          orport=<port>
          id=<fingerprint>
          ipv6=<IPv6 address>:<IPv6 orport>
        each line's key/value pairs are placed in a dictonary,
        (of string -> string key/value pairs),
        and these dictionaries are placed in an array.
        comments start with # and are ignored """
    relaylist = []
    file_data = read_from_file(file_name, MAX_LIST_FILE_SIZE)
    if file_data is None:
      return relaylist
    for line in file_data.split('\n'):
      relay_entry = {}
      # ignore comments
      line_comment_split = line.split('#')
      line = line_comment_split[0]
      # cleanup whitespace
      line = cleanse_whitespace(line)
      line = line.strip()
      if len(line) == 0:
        continue
      for item in line.split(' '):
        item = item.strip()
        if len(item) == 0:
          continue
        key_value_split = item.split('=')
        kvl = len(key_value_split)
        if kvl < 1 or kvl > 2:
          print '#error Bad %s item: %s, format is key=value.'%(
                                                 file_name, item)
        if kvl == 1:
          # assume that entries without a key are the ipv4 address,
          # perhaps with a dirport
          ipv4_maybe_dirport = key_value_split[0]
          ipv4_maybe_dirport_split = ipv4_maybe_dirport.split(':')
          dirl = len(ipv4_maybe_dirport_split)
          if dirl < 1 or dirl > 2:
            print '#error Bad %s IPv4 item: %s, format is ipv4:port.'%(
                                                        file_name, item)
          if dirl >= 1:
            relay_entry['ipv4'] = ipv4_maybe_dirport_split[0]
          if dirl == 2:
            relay_entry['dirport'] = ipv4_maybe_dirport_split[1]
        elif kvl == 2:
          relay_entry[key_value_split[0]] = key_value_split[1]
      relaylist.append(relay_entry)
    return relaylist

  # apply the fallback whitelist and blacklist
  def apply_filter_lists(self):
    excluded_count = 0
    logging.debug('Applying whitelist and blacklist.')
    # parse the whitelist and blacklist
    whitelist = self.load_relaylist(WHITELIST_FILE_NAME)
    blacklist = self.load_relaylist(BLACKLIST_FILE_NAME)
    filtered_fallbacks = []
    for f in self.fallbacks:
      in_whitelist = f.is_in_whitelist(whitelist)
      in_blacklist = f.is_in_blacklist(blacklist)
      if in_whitelist and in_blacklist:
        if BLACKLIST_EXCLUDES_WHITELIST_ENTRIES:
          # exclude
          excluded_count += 1
          logging.warning('Excluding %s: in both blacklist and whitelist.',
                          f._fpr)
        else:
          # include
          filtered_fallbacks.append(f)
      elif in_whitelist:
        # include
        filtered_fallbacks.append(f)
      elif in_blacklist:
        # exclude
        excluded_count += 1
        logging.debug('Excluding %s: in blacklist.', f._fpr)
      else:
        if INCLUDE_UNLISTED_ENTRIES:
          # include
          filtered_fallbacks.append(f)
        else:
          # exclude
          excluded_count += 1
          logging.info('Excluding %s: in neither blacklist nor whitelist.',
                       f._fpr)
    self.fallbacks = filtered_fallbacks
    return excluded_count

  @staticmethod
  def summarise_filters(initial_count, excluded_count):
    return '/* Whitelist & blacklist excluded %d of %d candidates. */'%(
                                                excluded_count, initial_count)

  # calculate each fallback's measured bandwidth based on the median
  # consensus weight to advertised bandwdith ratio
  def calculate_measured_bandwidth(self):
    self.sort_fallbacks_by_cw_to_bw_factor()
    median_fallback = self.fallback_median(True)
    median_cw_to_bw_factor = median_fallback.cw_to_bw_factor()
    for f in self.fallbacks:
      f.set_measured_bandwidth(median_cw_to_bw_factor)

  # remove relays with low measured bandwidth from the fallback list
  # calculate_measured_bandwidth for each relay before calling this
  def remove_low_bandwidth_relays(self):
    if MIN_BANDWIDTH is None:
      return
    above_min_bw_fallbacks = []
    for f in self.fallbacks:
      if f._data['measured_bandwidth'] >= MIN_BANDWIDTH:
        above_min_bw_fallbacks.append(f)
      else:
        # the bandwidth we log here is limited by the relay's consensus weight
        # as well as its adverttised bandwidth. See set_measured_bandwidth
        # for details
        logging.info('%s not a candidate: bandwidth %.1fMB/s too low, must ' +
                     'be at least %.1fMB/s', f._fpr,
                     f._data['measured_bandwidth']/(1024.0*1024.0),
                     MIN_BANDWIDTH/(1024.0*1024.0))
    self.fallbacks = above_min_bw_fallbacks

  # the minimum fallback in the list
  # call one of the sort_fallbacks_* functions before calling this
  def fallback_min(self):
    if len(self.fallbacks) > 0:
      return self.fallbacks[-1]
    else:
      return None

  # the median fallback in the list
  # call one of the sort_fallbacks_* functions before calling this
  def fallback_median(self, require_advertised_bandwidth):
    # use the low-median when there are an evan number of fallbacks,
    # for consistency with the bandwidth authorities
    if len(self.fallbacks) > 0:
      median_position = (len(self.fallbacks) - 1) / 2
      if not require_advertised_bandwidth:
        return self.fallbacks[median_position]
      # if we need advertised_bandwidth but this relay doesn't have it,
      # move to a fallback with greater consensus weight until we find one
      while not self.fallbacks[median_position]._data['advertised_bandwidth']:
        median_position += 1
        if median_position >= len(self.fallbacks):
          return None
      return self.fallbacks[median_position]
    else:
      return None

  # the maximum fallback in the list
  # call one of the sort_fallbacks_* functions before calling this
  def fallback_max(self):
    if len(self.fallbacks) > 0:
      return self.fallbacks[0]
    else:
      return None

  def summarise_fallbacks(self, eligible_count, guard_count, target_count,
                          max_count):
    # Report:
    #  whether we checked consensus download times
    #  the number of fallback directories (and limits/exclusions, if relevant)
    #  min & max fallback bandwidths
    #  #error if below minimum count
    if PERFORM_IPV4_DIRPORT_CHECKS or PERFORM_IPV6_DIRPORT_CHECKS:
      s = '/* Checked %s%s%s DirPorts served a consensus within %.1fs. */'%(
            'IPv4' if PERFORM_IPV4_DIRPORT_CHECKS else '',
            ' and ' if (PERFORM_IPV4_DIRPORT_CHECKS
                        and PERFORM_IPV6_DIRPORT_CHECKS) else '',
            'IPv6' if PERFORM_IPV6_DIRPORT_CHECKS else '',
            CONSENSUS_DOWNLOAD_SPEED_MAX)
    else:
      s = '/* Did not check IPv4 or IPv6 DirPort consensus downloads. */'
    s += '\n'
    # Multiline C comment with #error if things go bad
    s += '/*'
    s += '\n'
    # Integers don't need escaping in C comments
    fallback_count = len(self.fallbacks)
    if FALLBACK_PROPORTION_OF_GUARDS is None:
      fallback_proportion = ''
    else:
      fallback_proportion = ', Target %d (%d * %f)'%(target_count, guard_count,
                                                 FALLBACK_PROPORTION_OF_GUARDS)
    s += 'Final Count: %d (Eligible %d%s'%(fallback_count,
                                           eligible_count,
                                           fallback_proportion)
    if MAX_FALLBACK_COUNT is not None:
      s += ', Clamped to %d'%(MAX_FALLBACK_COUNT)
    s += ')\n'
    if eligible_count != fallback_count:
      s += 'Excluded:     %d (Eligible Count Exceeded Target Count)'%(
                                              eligible_count - fallback_count)
      s += '\n'
    min_fb = self.fallback_min()
    min_bw = min_fb._data['measured_bandwidth']
    max_fb = self.fallback_max()
    max_bw = max_fb._data['measured_bandwidth']
    s += 'Bandwidth Range: %.1f - %.1f MB/s'%(min_bw/(1024.0*1024.0),
                                              max_bw/(1024.0*1024.0))
    s += '\n'
    s += '*/'
    if fallback_count < MIN_FALLBACK_COUNT:
      # We must have a minimum number of fallbacks so they are always
      # reachable, and are in diverse locations
      s += '\n'
      s += '#error Fallback Count %d is too low. '%(fallback_count)
      s += 'Must be at least %d for diversity. '%(MIN_FALLBACK_COUNT)
      s += 'Try adding entries to the whitelist, '
      s += 'or setting INCLUDE_UNLISTED_ENTRIES = True.'
    return s

## Main Function

def list_fallbacks():
  """ Fetches required onionoo documents and evaluates the
      fallback directory criteria for each of the relays """

  # find relays that could be fallbacks
  candidates = CandidateList()
  candidates.add_relays()

  # work out how many fallbacks we want
  guard_count = candidates.count_guards()
  if FALLBACK_PROPORTION_OF_GUARDS is None:
    target_count = guard_count
  else:
    target_count = int(guard_count * FALLBACK_PROPORTION_OF_GUARDS)
  # the maximum number of fallbacks is the least of:
  # - the target fallback count (FALLBACK_PROPORTION_OF_GUARDS * guard count)
  # - the maximum fallback count (MAX_FALLBACK_COUNT)
  if MAX_FALLBACK_COUNT is None:
    max_count = target_count
  else:
    max_count = min(target_count, MAX_FALLBACK_COUNT)

  candidates.compute_fallbacks()
  prefilter_fallbacks = copy.copy(candidates.fallbacks)

  # filter with the whitelist and blacklist
  initial_count = len(candidates.fallbacks)
  excluded_count = candidates.apply_filter_lists()
  print candidates.summarise_filters(initial_count, excluded_count)
  eligible_count = len(candidates.fallbacks)

  # calculate the measured bandwidth of each relay,
  # then remove low-bandwidth relays
  candidates.calculate_measured_bandwidth()
  candidates.remove_low_bandwidth_relays()
  # make sure the list is sorted by bandwidth when we output it
  # so that we include the active fallbacks with the greatest bandwidth
  candidates.sort_fallbacks_by_measured_bandwidth()

  # print the raw fallback list
  #for x in candidates.fallbacks:
  #  print x.fallbackdir_line(True)
  #  print json.dumps(candidates[x]._data, sort_keys=True, indent=4,
  #                   separators=(',', ': '), default=json_util.default)

  if len(candidates.fallbacks) > 0:
    print candidates.summarise_fallbacks(eligible_count, guard_count,
                                         target_count, max_count)
  else:
    print '/* No Fallbacks met criteria */'

  for s in fetch_source_list():
    print describe_fetch_source(s)

  active_count = 0
  for x in candidates.fallbacks:
    dl_speed_ok = x.fallback_consensus_dl_check()
    print x.fallbackdir_line(dl_speed_ok, candidates.fallbacks,
                             prefilter_fallbacks)
    if dl_speed_ok:
      # this fallback is included in the list
      active_count += 1
      if active_count >= max_count:
        # we have enough fallbacks
        break

if __name__ == "__main__":
  list_fallbacks()