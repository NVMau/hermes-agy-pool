"""Immutable, caller-attested discovery snapshot; no identity probes or I/O."""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
from .config import CAPABILITIES


def _name(value):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError('Expected a nonempty exact identifier')


@dataclass(frozen=True)
class ModelSupport:
    verified: bool
    capabilities: frozenset[str]

    def __post_init__(self):
        if type(self.verified) is not bool:
            raise ValueError('Expected boolean verification')
        capabilities = frozenset(self.capabilities)
        if not capabilities <= CAPABILITIES:
            raise ValueError('Unknown capability')
        object.__setattr__(self, 'capabilities', capabilities)


@dataclass(frozen=True)
class Catalog:
    aliases: Mapping[str, str]
    verified_accounts: frozenset[str]
    support: Mapping[tuple[str, str], ModelSupport]

    def __post_init__(self):
        aliases = dict(self.aliases)
        accounts = frozenset(self.verified_accounts)
        support = dict(self.support)
        for alias, canonical in aliases.items():
            _name(alias)
            _name(canonical)
            if canonical in aliases:
                raise ValueError('Aliases must point directly to canonical IDs')
        for account in accounts:
            _name(account)
        for (account, model), entry in support.items():
            _name(account)
            _name(model)
            if model in aliases or not isinstance(entry, ModelSupport):
                raise ValueError('Support must use canonical keys and ModelSupport')
        object.__setattr__(self, 'aliases', MappingProxyType(aliases))
        object.__setattr__(self, 'verified_accounts', accounts)
        object.__setattr__(self, 'support', MappingProxyType(support))

    def canonical(self, model: str) -> str:
        return self.aliases.get(model, model)

    def eligible(self, account: str, model: str, required=()) -> bool:
        entry = self.support.get((account, self.canonical(model)))
        return (account in self.verified_accounts and entry is not None
                and entry.verified and set(required) <= entry.capabilities)
