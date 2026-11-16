"""Local-only OpenAI-compatible model catalog connectivity probe."""
import ipaddress
import json
import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.error import HTTPError, URLError

from .config import ConfigError, _constant, _pairs


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_proxy_config(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        raise ConfigError('Invalid proxy configuration') from exc
    if not isinstance(data, dict) or set(data) != {'schema_version', 'base_url', 'api_key_env', 'timeout_s'}:
        raise ConfigError('Invalid proxy configuration fields')
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ConfigError('Invalid proxy schema version')
    url = data['base_url']
    if not isinstance(url, str):
        raise ConfigError('Expected loopback HTTP base URL with port and /v1 path')
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        address = ipaddress.ip_address(host or '')
        # IPv4-mapped IPv6 must be checked as IPv4, not as ordinary IPv6.
        local = (address.ipv4_mapped or address).is_loopback if isinstance(address, ipaddress.IPv6Address) else address.is_loopback
    except ValueError:
        raise ConfigError('Expected loopback HTTP base URL with port and /v1 path') from None
    if not local or parts.scheme != 'http' or parts.username is not None or parts.password is not None or parts.path != '/v1' or parts.query or parts.fragment or not port:
        raise ConfigError('Expected loopback HTTP base URL with port and /v1 path')
    name = data['api_key_env']
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name, flags=re.ASCII):
        raise ConfigError('Invalid API key environment variable name')
    timeout = data['timeout_s']
    if type(timeout) not in (int, float) or not 0.1 <= timeout <= 30 or not math.isfinite(timeout):
        raise ConfigError('Timeout must be between 0.1 and 30 seconds')
    return data


def hermes_fragment(config, model):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', model, re.ASCII):
        raise ConfigError('Invalid model identifier')
    return {'model': {'default': model, 'provider': 'custom:agy-local'},
            'providers': {'agy-local': {'api': config['base_url'], 'key_env': config['api_key_env']}}}


def probe(config):
    key = os.environ.get(config['api_key_env'])
    if not key or not key.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ConfigError('API key environment variable missing or invalid')
    request = Request(config['base_url'] + '/models', headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=config['timeout_s']) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ConfigError('Proxy response too large')
        data = json.loads(body)
        models = data['data']
        if not isinstance(models, list) or len(models) > 10000:
            raise ValueError()
        ids = [entry['id'] for entry in models]
        if any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', i, re.ASCII) or key in i for i in ids):
            raise ValueError()
    except HTTPError as exc:
        raise ConfigError(f'Proxy returned HTTP {exc.code}') from None
    except (URLError, OSError, TimeoutError):
        raise ConfigError('Proxy connection failed or timed out') from None
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise ConfigError('Invalid proxy model catalog response') from None
    return {'mode': 'proxy-connectivity', 'models': ids, 'account_selection': 'proxy-managed'}
