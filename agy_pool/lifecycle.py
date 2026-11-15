"""Single-owner synchronous dispatched-ticket lifecycle; no live backend."""
from dataclasses import dataclass
from typing import Protocol
from .runtime import Attempt, Fragment
from .errors import Failure


@dataclass(frozen=True)
class LaunchContext:
    attempt: Attempt
    session: str


@dataclass(frozen=True)
class Output:
    fragment: Fragment
    value: object


@dataclass(frozen=True)
class Metadata:
    """Non-visible usage or finish information; not a replay boundary."""
    kind: str
    value: object

    def __post_init__(self):
        if self.kind not in ('usage', 'finish'):
            raise ValueError('Unknown metadata kind')


class BackendError(Exception):
    def __init__(self, failure: Failure):
        super().__init__('Backend attempt failed')
        self.failure = failure


class Worker(Protocol):
    # Trusted adapter evidence, never merely the requested account label.
    verified_account: str | None

    def start(self) -> None:
        """Start only this identity; retain partial resources on failure."""
        ...

    def read(self) -> Output | Metadata:
        """Return a typed event, raise StopIteration at EOF, or an error."""
        ...

    def close(self) -> bool:
        """Stop execution and wait; True only after all owned work has ended.

        Must work before/after failed start and support retries after failure.
        """
        ...


class Backend(Protocol):
    def prepare(self, context: LaunchContext) -> Worker:
        """Return an inert handle; MUST NOT start work or acquire resources.

        Identity evidence must be independent of the requested account label.
        A preparation exception must leave no resource needing cleanup.
        """
        ...


class Session:
    """Own one dispatched ticket until explicit, confirmed cleanup.

    Single-threaded only. Does not poll, retry attempts, or reap on GC.
    Retain this object and retry close when cleanup raises.
    """
    def __init__(self, ticket, store, backend: Backend):
        if ticket.status != 'reserved' or ticket.lease is None or ticket.attempt is None:
            raise ValueError('A dispatched, leased ticket is required')
        ticket.status = 'executing'
        self.ticket = ticket
        self.store = store
        self.backend = backend
        self.worker = None
        self._outcome = None
        self._worker_closed = False
        self._closed = False
        self._failure = Failure()
        self._started = False

    def start(self):
        if self._started or self._outcome is not None:
            raise RuntimeError('Session cannot be restarted')
        self._started = True
        try:
            self.worker = self.backend.prepare(LaunchContext(self.ticket.attempt, self.ticket.session))
            if getattr(self.worker, 'verified_account', None) != self.ticket.attempt.account:
                raise ValueError('Backend identity is unverified or mismatched')
            self.worker.start()
        except BaseException as exc:
            self._failure = exc.failure if isinstance(exc, BackendError) else Failure()
            self.close()
            raise
        return self

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        if self._outcome is not None:
            raise RuntimeError('Cleanup pending; call close again')
        if not self._started or self.worker is None:
            raise RuntimeError('Session has not started')
        try:
            event = self.worker.read()
        except StopIteration:
            self._outcome = 'succeeded'
            self._close_for_iteration()
            raise
        except BaseException as exc:
            self._failure = exc.failure if isinstance(exc, BackendError) else Failure()
            self._close_for_iteration()
            raise
        try:
            try:
                if isinstance(event, Metadata):
                    return event
                self.ticket.request.visible(self.ticket.attempt, event.fragment)
                return event
            except StopIteration as exc:
                raise RuntimeError('Post-read processing raised StopIteration') from exc
        except BaseException as exc:
            self._failure = exc.failure if isinstance(exc, BackendError) else Failure()
            self._close_for_iteration()
            raise

    def _close_for_iteration(self):
        try:
            self.close()
        except StopIteration as exc:
            raise RuntimeError('Cleanup raised StopIteration') from exc

    def cancel(self):
        if self._outcome is None:
            self._outcome = 'cancelled'
        self.close()

    def close(self):
        if self._closed:
            return
        if self._outcome is None:
            self._outcome = 'stopped'
        self.ticket.status = 'cleanup_pending'
        if self.worker is not None and not self._worker_closed:
            if self.worker.close() is not True:
                raise RuntimeError('Worker termination unconfirmed')
            self._worker_closed = True
        if self.ticket.lease is not None:
            if self.store.release(self.ticket.lease.token, owner=self.ticket.lease.owner) is not True:
                raise RuntimeError('Lease release unconfirmed; ownership retained')
            self.ticket.lease = None
        if self._outcome == 'succeeded':
            self.ticket.request.succeed(self.ticket.attempt)
        else:
            self.ticket.request.fail(self.ticket.attempt, self._failure)
        self.ticket.status = self._outcome
        self._closed = True
