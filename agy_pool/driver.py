"""Caller-driven bridge for a dedicated scheduler and one request.

No provider backend or account verification is supplied here. Backends are
trusted to meet lifecycle's inert prepare, identity and cleanup contracts.
"""
from .lifecycle import BackendError, Metadata, Session
from .runtime import Status
from .scheduler import PollError


class CapacityBlocked(RuntimeError):
    """No capacity in this poll; call next again after capacity changes."""


class Driver:
    def __init__(self, scheduler, request, backend, *, session, owner):
        if scheduler.pending_count:
            raise ValueError('Driver requires an empty dedicated scheduler')
        self.scheduler = scheduler
        self.request = request
        self.backend = backend
        self.session_id = session
        self.owner = owner
        self.ticket = None
        self.active = None
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        while True:
            if self.active is None:
                if self.request.status != Status.READY:
                    self.closed = True
                    raise StopIteration
                own_pending = self.ticket is not None and self.ticket in self.scheduler._pending
                if self.scheduler.pending_count != int(own_pending):
                    raise ValueError('Driver requires exclusive scheduler ownership')
                if self.ticket is None or self.ticket.status != 'queued':
                    self.ticket = self.scheduler.submit(self.request, session=self.session_id, owner=self.owner)
                try:
                    dispatched = self.scheduler.poll()
                except PollError as exc:
                    if exc.dispatched:
                        self.active = Session(exc.dispatched[0], self.scheduler.store, self.backend)
                    raise
                if not dispatched:
                    if self.ticket.status == 'queued':
                        raise CapacityBlocked('Capacity unavailable; poll again later')
                    self.closed = True
                    raise StopIteration
                self.active = Session(dispatched[0], self.scheduler.store, self.backend)
                try:
                    self.active.start()
                except BackendError:
                    if self.ticket.status == 'cleanup_pending':
                        raise
                    self.active = None
                    if self.request.status == Status.READY:
                        continue
                    raise
            try:
                event = next(self.active)
            except StopIteration:
                self.active = None
                self.closed = True
                raise
            except BackendError:
                if self.ticket.status == 'cleanup_pending':
                    raise
                self.active = None
                if self.request.status == Status.READY:
                    continue
                raise
            if not isinstance(event, Metadata):
                return event

    def close(self):
        if self.active is not None:
            self.active.cancel()
            self.active = None
        if self.ticket is not None and self.ticket.status in ('queued', 'cleanup_pending'):
            self.scheduler.cancel(self.ticket)
        self.closed = True
