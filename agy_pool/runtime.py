"""Explicit request transitions, not a transport, scheduler or retry loop."""
from dataclasses import dataclass
from enum import Enum
from .config import Config
from .errors import Failure, finite_number
from .health import Health
from .router import PlanError


class Fragment(str, Enum):
    TEXT = 'text'
    REASONING = 'reasoning'
    TOOL = 'tool'


class Status(str, Enum):
    READY = 'ready'
    IN_FLIGHT = 'in_flight'
    SUCCEEDED = 'succeeded'
    STOPPED = 'stopped'


@dataclass(frozen=True)
class Attempt:
    account: str
    model: str
    canonical_model: str
    number: int


class RequestRouter:
    def __init__(self, config: Config, route_id: str, health: Health, *, deadline=None):
        if route_id not in config.routes:
            raise PlanError('Unknown route')
        self._config = config
        self._route = config.routes[route_id]
        self._health = health
        timeout = finite_number(config.limits.request_timeout_s, positive=True)
        if type(config.limits.max_attempts) is not int or config.limits.max_attempts <= 0:
            raise ValueError('Expected positive integer max_attempts')
        limit = finite_number(health.now() + timeout)
        self._deadline = limit if deadline is None else min(limit, finite_number(deadline))
        self._stage = 0
        self._visited = set()
        self._active = None
        self._status = Status.READY
        self._last_failure = None
        self._stop_reason = None
        self._visible = False

    @property
    def status(self):
        return self._status

    @property
    def visited(self):
        return frozenset(self._visited)

    @property
    def stop_reason(self):
        return self._stop_reason

    def _stop(self, reason):
        self._status = Status.STOPPED
        self._stop_reason = reason

    def next_attempt(self) -> Attempt | None:
        if self.status == Status.IN_FLIGHT:
            raise RuntimeError('Complete the active attempt first')
        if self.status != Status.READY:
            return None
        if self._health.now() >= self._deadline:
            self._stop('deadline')
            return None
        if len(self._visited) >= self._config.limits.max_attempts:
            self._stop('max_attempts')
            return None
        while self._stage < len(self._route.models):
            model = self._route.models[self._stage]
            canonical = self._health.catalog.canonical(model)
            for account in self._config.groups[self._route.group].accounts:
                pair = (account, canonical)
                if (pair not in self._visited and self._config.accounts[account].enabled
                        and self._health.catalog.eligible(account, model, self._route.required_capabilities)
                        and self._health.available(account, model)):
                    self._visited.add(pair)
                    self._active = Attempt(account, model, canonical, len(self._visited))
                    self._status = Status.IN_FLIGHT
                    return self._active
            if (self._last_failure is None
                    or self._last_failure.kind.value not in self._route.fallback_on):
                break
            self._stage += 1
            self._last_failure = None
        self._stop('exhausted')
        return None

    def _check_active(self, attempt):
        if self.status != Status.IN_FLIGHT or attempt is not self._active:
            raise RuntimeError('Expected the active attempt token')

    def fail(self, attempt: Attempt, failure: Failure):
        self._check_active(attempt)
        self._health.record(attempt.account, attempt.model, failure)
        self._last_failure = failure
        self._active = None
        self._status = Status.READY
        if self._visible:
            self._stop('visible_output')
        elif not failure.retryable:
            self._stop(failure.kind.value)

    def visible(self, attempt: Attempt, fragment: Fragment):
        """Call before exposing any text, reasoning or tool fragment."""
        self._check_active(attempt)
        if not isinstance(fragment, Fragment):
            raise ValueError('Expected typed fragment')
        self._visible = True

    def succeed(self, attempt: Attempt):
        self._check_active(attempt)
        self._active = None
        self._status = Status.SUCCEEDED
