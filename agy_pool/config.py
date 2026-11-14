"""Immutable configuration models for the offline pool core."""
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class Limits:
    max_workers: int
    queue_timeout_s: float
    request_timeout_s: float
    max_attempts: int


@dataclass(frozen=True)
class Account:
    backend: str
    identity_ref: str
    max_concurrency: int
    enabled: bool


@dataclass(frozen=True)
class Group:
    accounts: tuple[str, ...]
    selection: str


@dataclass(frozen=True)
class Route:
    group: str
    models: tuple[str, ...]
    fallback_on: tuple[str, ...]
    required_capabilities: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    schema_version: int
    limits: Limits
    accounts: Mapping[str, Account]
    groups: Mapping[str, Group]
    routes: Mapping[str, Route]


class ConfigError(ValueError):
    """Configuration does not satisfy the explicit schema."""


CAPABILITIES = frozenset({'tools', 'vision', 'schema'})
FALLBACK_REASONS = frozenset({'unsupported_model', 'quota', 'capacity'})


def _object(value, fields=None):
    if not isinstance(value, dict) or (fields is not None and set(value) != set(fields)):
        raise ConfigError('Expected object with exactly the documented fields')
    return value


def _id(value, *, route=False):
    import re
    pattern = r'[A-Za-z0-9][A-Za-z0-9_.-]*'
    if route:
        pattern += r'(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*'
    if not isinstance(value, str) or re.fullmatch(pattern, value, flags=re.ASCII) is None:
        raise ConfigError('Invalid identifier')


def _number(value, *, integer=False):
    import math
    if type(value) not in (int, float) or (integer and type(value) is not int):
        raise ConfigError('Expected a positive number (not boolean)')
    if value <= 0 or (type(value) is float and not math.isfinite(value)):
        raise ConfigError('Expected a positive finite number')


def _strings(value, *, nonempty=False, allowed=None):
    if not isinstance(value, list) or (nonempty and not value):
        raise ConfigError('Expected a list with the documented cardinality')
    for item in value:
        _id(item)
        if allowed is not None and item not in allowed:
            raise ConfigError('Unknown reference or policy value')
    if len(value) != len(set(value)):
        raise ConfigError('Duplicate list entry')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError('Duplicate JSON object key')
        result[key] = value
    return result


def _constant(_value):
    raise ConfigError('Non-finite JSON constant')


def load_config(path: str | Path) -> Config:
    return parse_config(Path(path).read_text(encoding='utf-8'))


def parse_config(text: str) -> Config:
    try:
        data = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        raise ConfigError('Invalid strict JSON configuration') from exc
    _object(data, {'schema_version', 'limits', 'accounts', 'groups', 'routes'})
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ConfigError('Only schema_version 1 is supported')
    limits = _object(data['limits'], {'max_workers', 'queue_timeout_s', 'request_timeout_s', 'max_attempts'})
    for key, value in limits.items():
        _number(value, integer=key in {'max_workers', 'max_attempts'})
    for section in ('accounts', 'groups', 'routes'):
        entries = _object(data[section])
        if not entries:
            raise ConfigError('Empty configuration section')
        for key in entries:
            _id(key, route=section == 'routes')
    for account in data['accounts'].values():
        _object(account, {'backend', 'identity_ref', 'max_concurrency', 'enabled'})
        if account['backend'] != 'agy' or type(account['enabled']) is not bool:
            raise ConfigError('Expected agy backend and boolean enabled')
        _id(account['identity_ref'])
        _number(account['max_concurrency'], integer=True)
    for group in data['groups'].values():
        _object(group, {'accounts', 'selection'})
        _strings(group['accounts'], nonempty=True, allowed=data['accounts'])
        if group['selection'] != 'sticky_round_robin':
            raise ConfigError('Unknown selection policy')
    for route in data['routes'].values():
        _object(route, {'group', 'models', 'fallback_on', 'required_capabilities'})
        _id(route['group'])
        if route['group'] not in data['groups']:
            raise ConfigError('Unknown route group')
        _strings(route['models'], nonempty=True)
        _strings(route['fallback_on'], allowed=FALLBACK_REASONS)
        _strings(route['required_capabilities'], allowed=CAPABILITIES)
    return Config(
        data['schema_version'], Limits(**data['limits']),
        MappingProxyType({k: Account(**v) for k, v in data['accounts'].items()}),
        MappingProxyType({k: Group(tuple(v['accounts']), v['selection'])
                          for k, v in data['groups'].items()}),
        MappingProxyType({k: Route(v['group'], tuple(v['models']),
                                   tuple(v['fallback_on']), tuple(v['required_capabilities']))
                          for k, v in data['routes'].items()}),
    )
