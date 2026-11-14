"""Bounded, caller-driven metadata scheduler; no sleep loop or live execution."""
from dataclasses import dataclass
import time
from .errors import finite_number
from .runtime import Attempt, RequestRouter, Status
from .state import Lease


@dataclass(eq=False)
class Ticket:
    request: RequestRouter
    session: str
    owner: str
    deadline: float
    status: str = 'queued'
    attempt: Attempt | None = None
    lease: Lease | None = None


class Scheduler:
    def __init__(self, store, *, queue_timeout_s, max_pending, clock=time.monotonic):
        self.store = store
        self.queue_timeout_s = finite_number(queue_timeout_s, positive=True)
        if type(max_pending) is not int or not 0 < max_pending < 2**63:
            raise ValueError('Expected positive integer max_pending')
        self.max_pending = max_pending
        self.clock = clock
        self._pending = []
        self._closed = False

    @property
    def pending_count(self):
        return len(self._pending)

    def _check_open(self):
        if self._closed:
            raise RuntimeError('Scheduler is closed')

    def close(self):
        if not self._closed:
            for ticket in tuple(self._pending):
                self.cancel(ticket)
            self._closed = True

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *_):
        self.close()

    def submit(self, request, *, session, owner):
        self._check_open()
        if any(not isinstance(v, str) or not v for v in (session, owner)):
            raise ValueError('Expected nonempty session and owner identifiers')
        if request.status != Status.READY or any(t.request is request for t in self._pending):
            raise ValueError('Expected unqueued READY request')
        if len(self._pending) >= self.max_pending:
            raise OverflowError('Pending queue is full')
        deadline = min(request.deadline, finite_number(finite_number(self.clock()) + self.queue_timeout_s))
        ticket = Ticket(request, session, owner, deadline)
        self._pending.append(ticket)
        return ticket

    def cancel(self, ticket):
        if ticket not in self._pending:
            return False
        self._pending.remove(ticket)
        ticket.status = 'cancelled'
        ticket.request.cancel()
        return True

    def poll(self):
        """One finite pass; host must schedule another poll after a capacity change."""
        self._check_open()
        dispatched = []
        for ticket in tuple(self._pending):
            if finite_number(self.clock()) >= ticket.deadline:
                ticket.status = 'expired'
                ticket.request.cancel('queue_deadline')
                self._pending.remove(ticket)
                continue
            def reserve(model, canonical, accounts):
                ticket.lease = self.store.reserve(accounts, group=ticket.request.group,
                                                  model=canonical, session=ticket.session, owner=ticket.owner)
                if ticket.lease is not None and finite_number(self.clock()) >= ticket.deadline:
                    # No backend has been launched; safe to release a late metadata reservation.
                    self.store.release(ticket.lease.token, owner=ticket.lease.owner)
                    ticket.lease = None
                    return None
                return None if ticket.lease is None else ticket.lease.account
            ticket.attempt = ticket.request.next_attempt(reserve=reserve)
            if ticket.attempt is not None:
                ticket.status = 'reserved'
                self._pending.remove(ticket)
                dispatched.append(ticket)
            elif finite_number(self.clock()) >= ticket.deadline:
                ticket.status = 'expired'
                ticket.request.cancel('queue_deadline')
                self._pending.remove(ticket)
            elif ticket.request.status != Status.READY:
                ticket.status = 'stopped'
                self._pending.remove(ticket)
        return tuple(dispatched)
