# Atomic reservation scheduler (metadata only)

This milestone implements standard-library SQLite capacity reservations and a caller-driven bounded queue. It does **not** launch, detect, reuse, kill or measure AGY workers. There is no transport, credential reader, identity provisioning, live process pool, background scheduler, Hermes provider or stream cancellation implementation.

## Store contract

`LeaseStore(path, max_workers=..., account_limits={account: max_concurrency, ...})` owns one SQLite connection. Open a separate store in each process/thread; do not inherit connections across fork/spawn. Use the same local SQLite file and exactly the same limits for all clients. Different files, account aliases for the same physical identity, uncooperative clients or direct database modifications bypass this coordination boundary. This is not an authorization sandbox or distributed/network-filesystem lock.

All capacities are positive integers (not bool), below `2**63`. Empty/invalid account maps and mismatched persisted limits are rejected; constructor failure closes its connection. Limit reconfiguration and database migrations are not implemented. Do not replace a database while live or unknown reservations exist.

`reserve(eligible_accounts, group=..., model=..., session=..., owner=...)` executes `BEGIN IMMEDIATE`, counts **all** leases globally and by account (not by group/model), chooses an eligible account, writes the reservation, affinity and cursor, then commits. Exceptions roll back the whole transaction. `None` means full capacity, empty candidates or transient SQLite contention; never fabricate an upstream `Failure(CAPACITY)` for this result. Store initialization has a five-second SQLite busy timeout; subsequent transactions fail promptly on contention. Reservation converts `SQLITE_BUSY`/`SQLITE_LOCKED` to `None`; other errors propagate. Release/idle mutation contention raises `sqlite3.OperationalError`: retain the token and retry through the host's bounded lifecycle, never assume release succeeded.

A returned immutable `Lease(token, account, owner)` has a cryptographically random opaque token; it is an ownership capability, **not an OAuth/auth token**. Never expose it in logs. Use a unique owner incarnation including process-start identity, not a reusable PID alone. `release(token, owner=...)` deletes only a matching token/owner, returns True once and False on repeats/wrong ownership. An old token cannot delete a replacement lease. `set_idle(token, owner=...)` marks metadata idle; idle leases still count against both hard caps. Connection close/context exit does not release any leases.

There is deliberately no heartbeat expiry or stale-owner reaper. Even process exit does not automatically delete reservations. The owner/supervisor must first confirm its worker has terminated, or prove no worker was launched, before explicit release. An uncertain release fails closed. This store does not itself verify process ownership or termination. Persisted reservations survive reconnection; crashes can strand capacity until an authorized supervisor with the original ownership capability resolves it. No administrative force-recovery API is shipped.

## Selection

Only the supplied eligible account tuple is considered. Sticky bindings are keyed by `(group, canonical_model, session)`; cursors by `(group, canonical_model)`. A binding is preferred only while present in the eligible set and below capacity. Otherwise round-robin starts after the last non-sticky selection in the eligible tuple. A sticky hit does not advance that cursor. Capacity-full and busy results do not modify selection state. Cursors/bindings survive reopen and are updated atomically across processes. Affinity does not mean a live persistent worker/history is being reused. Session IDs must be opaque metadata, never prompts. Affinity retention/garbage collection and effort/rules-digest worker keys are future work.

## Runtime bridge and queue

`RequestRouter.next_attempt(reserve=callback)` calls `callback(requested_model, canonical_model, eligible_accounts)` only after checking model/group membership, enabled state, verified catalog, required capabilities, health, visited pairs and request budget/deadline. Callback returns one supplied account or None. An outside account raises ValueError without consuming an attempt. None leaves READY and does not spend an attempt, exhaust a model or grant fallback. Existing direct `next_attempt()` callers retain configured-order behavior; use the reservation bridge for hard-cap scheduling.

`Scheduler(store, queue_timeout_s=..., max_pending=..., clock=...)` is **single-owner, non-thread-safe**, with a bounded in-memory queue per instance, not a shared database queue. `max_pending` is an explicit host limit (positive integer below `2**63`), not a new config schema field. Supply the same monotonic clock/domain as `Health`/`RequestRouter`; backward/invalid clocks are not a supported scheduling environment. Times/durations must be finite and nonnegative; timeout must be positive.

- `submit(request, session=..., owner=...)` accepts an unqueued READY request; records `min(request.deadline, now + queue_timeout_s)`. The queue raises OverflowError at `max_pending`; it does not retain overflowed requests. Invalid identifiers/limits are rejected. Do not submit a request to multiple scheduler instances.
- `poll()` makes one finite submission-order pass, checks each current deadline and attempts at most one atomic reservation per eligible request. Blocked tickets stay queued; later tickets can use other free capacity. No sleeps, fake-clock spin loops, timer threads or indefinite waits are hidden here. The host must invoke poll again on its own bounded cadence/capacity notification, and must poll to observe expiry. Filesystem/SQLite latency is not a real-time execution guarantee.
- Late metadata reservations are explicitly released before committing a routing attempt because no backend has launched. Do not launch inside a store override or callback. Return tickets with status `reserved`, their runtime `attempt` and `lease`. Caller owns the routing result and lease thereafter, including visible-output marking, fail/succeed, worker termination and release. Treat ticket fields as read-only; retain every returned ticket, including tickets dispatched before a later polling error.
- `cancel(ticket)` cancels only queued tickets, is idempotent and stops READY routing. It returns False for already-dispatched tickets, since cancellation alone is not proof a live worker has stopped. External router cancellation is observed on the next poll.
- `close()`/context exit cancels queued tickets, never closes the caller's store or releases dispatched live/idle/unknown reservations. Repeated close is safe; submit/poll after close raises RuntimeError.

### Construction example (requires a verified catalog supplied separately)

```python
from agy_pool.config import load_config
from agy_pool.runtime import RequestRouter
from agy_pool.errors import Failure, Kind
from agy_pool.scheduler import Scheduler
from agy_pool.state import LeaseStore

config = load_config("examples/pools.json")
# health is a Health instance created with a verified Catalog and monotonic clock.
with LeaseStore("pool-state.sqlite", max_workers=config.limits.max_workers,
                account_limits={a: v.max_concurrency for a, v in config.accounts.items()}) as store:
    with Scheduler(store, queue_timeout_s=config.limits.queue_timeout_s,
                   max_pending=64, clock=health.now) as scheduler:
        request = RequestRouter(config, "interactive/quality", health)
        ticket = scheduler.submit(request, session="opaque-session", owner="unique-owner-incarnation")
        ready = scheduler.poll()  # Metadata reservation, NOT inference execution.
        for dispatched in ready:
            # No backend exists here; explicitly retire the unused reservation.
            dispatched.request.fail(dispatched.attempt, Failure(Kind.INVALID_REQUEST))
            store.release(dispatched.lease.token, owner=dispatched.lease.owner)
```

Do not run this example with an invented verified catalog and call it live evidence. Direct dataclass construction remains outside config validation. Health/capability state stays in memory and is not cross-process synchronized by this store.

## Executed local evidence

Base was clean `main` at `b51fbeaed118aad89a58d8e2c13900f44848cb14`; branch `feat/atomic-scheduler`. Local Python 3.14.9 on Windows, stdlib only:

| Vertical behavior | Observed RED | Observed GREEN |
| --- | --- | --- |
| Shared account and global hard reservation caps | state module absent (1 failing test) | 16 full-suite tests passed |
| Opaque owned release; idle/unknown reservations persist | release method absent | 17 full-suite tests passed |
| Strict persisted limits and connection lifecycle | invalid numeric limits accepted; failing run exposed open-handle cleanup | 18 full-suite tests passed |
| Persisted sticky/round-robin eligible selection | new session selected a instead of b | 19 full-suite tests passed |
| Runtime reservation bridge | reserve keyword unsupported | 22 full-suite tests passed |
| Bounded queue deadlines and cancellation | scheduler module absent | 23 full-suite tests passed |
| Dispatch/queue resource lifecycle and invalid submissions | context manager absent; invalid session accepted | 25 full-suite tests passed |
| Deadline crossing during poll/reservation | second ticket reserved after deadline | 26 full-suite tests passed |
| Invalid scheduler clock | negative timestamp accepted on submit | 27 full-suite tests passed |

Additional real regression evidence (no new behavior claim): six `multiprocessing.get_context('spawn')` clients use independent connections and compete on one SQLite database across groups; at most one a reservation and two total winners. A separate four-client shared-account race yields exactly one reservation while global headroom remains for another account. Children exit without release and later reserved capacity remains occupied. Bounded gate/result waits, joins and cleanup are portable to Windows/Linux. SQLite lock contention and an aborting trigger verify prompt blocked reservation and transactional rollback. These are real metadata-client processes, **not AGY worker execution**. Pure queue tests use deterministic clocks and fixed finite poll counts. Final full suite: 28 test methods passed.

Commands: `python -m unittest discover -s tests -v`, `python -m agy_pool validate examples/pools.json`, `python -m agy_pool plan examples/pools.json interactive/quality --dry-run`, `git diff --check`. CI matrix remains Windows/Linux Python 3.11/3.14; Linux/3.11 execution and remote CI are not claimed by this local-only milestone.
