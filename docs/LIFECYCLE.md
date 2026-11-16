# Synchronous worker / lease lifecycle

`agy_pool.lifecycle.Session` consumes **one already dispatched** scheduler Ticket
(`status == 'reserved'`). It owns that ticket until worker shutdown, owner-scoped
lease release, and request finalization have all succeeded. It does not poll the
scheduler, launch AGY, implement automatic failover, or provide a production backend.

## Contract

- Construct `Session(ticket, store, backend)` and retain it **before** calling
  `session.start()`. Never use a temporary `Session(...).start()` in production:
  startup cleanup can fail, and the handle must remain reachable for retry.
- `Backend.prepare(LaunchContext(attempt, session))` returns an **inert** Worker.
  It must not allocate external resources or begin execution. If preparation raises,
  there must be nothing to clean up. Startup occurs only after the session owns the
  handle, so partial-start failures remain closeable.
- `Worker.verified_account` must be trusted adapter evidence of the identity the
  worker will use, not an echo of the requested account. Missing/None/mismatched
  evidence fails closed before `start`. The protocol cannot authenticate arbitrary
  injected Python code: real identity discovery/isolation remains an M0 blocker.
- `Worker.read()` yields `Output(Fragment, value)` or `Metadata('usage'|'finish', value)`,
  raises `StopIteration` for EOF, or raises an error. Metadata is non-visible; hosts
  must not expose it as user-visible output if they intend to allow a retry. Every
  output fragment is marked visible **before** delivery.
  All fragment types, including reasoning and tool fragments, prohibit later replay.
  `BackendError(Failure(...))` supplies typed routing policy during preparation,
  startup, or reading; other errors are unknown and nonretryable. No retry is
  performed by Session. A pre-output typed failure may
  leave RequestRouter READY only after cleanup; a host must explicitly requeue it.
- `Worker.close()` stops execution and waits. Only literal `True` confirms termination.
  It must work before startup, after partial startup, and on repeated calls following
  failure. EOF also invokes close: end-of-stream alone is not termination evidence.
- Only confirmed worker shutdown permits owner/token-scoped DB release. Failed close,
  unconfirmed shutdown, DB contention, or unconfirmed release retains ownership and
  `cleanup_pending`. RequestRouter stays IN_FLIGHT during worker/release cleanup, so
  another attempt cannot begin. Retry **the same session's `close()`**; confirmed
  shutdown is not repeated just because DB release failed. A failed finalization after
  successful release can also be retried without releasing twice.
- `cancel()` and explicit `close()` stop an unfinished attempt. Repeated completed
  calls are no-ops; a pending EOF/error outcome is not overwritten by cancellation.
  If cleanup raises during error handling, the original error remains in Python's
  exception context. Do not swallow cleanup failures or drop their session handles.
- `KeyboardInterrupt` and `SystemExit` during prepare/start/read also attempt cleanup
  before propagating. If cleanup itself is interrupted or fails, retain the session
  and retry close. This is not protection against forced process termination or
  asynchronous interruption at arbitrary bytecode boundaries.

## Host responsibilities

This module intentionally does **not** orchestrate Scheduler.poll. A caller must
consume both normal returned tickets **and** `PollError.dispatched`, even when a later
reservation in that same pass failed. Preserve the exception for reporting/retry;
first register every dispatched ticket with a retained Session (or explicitly clean
it up). Scheduler.close only cancels pending tickets, not dispatched sessions.

Call start/iteration/close in one owner thread. On early loop break, consumer error,
or shutdown, explicitly call close in a finally block; if it fails, retain the session
in the host's pending-cleanup registry and retry. Keep LeaseStore open until cleanup
completes. Do not mutate the Ticket/RequestRouter or externally release/reclaim its
lease while Session owns it. No GC/destructor cleanup, background cleanup thread,
auto-expiry/reclamation, async iterator, concurrent cancellation, or blocking-read
interruption is implemented. A host process crash needs future supervised recovery.

## Optional driver

`agy_pool.driver.Driver` bridges one request to an exclusively owned Scheduler and
Session. It discards metadata, retries typed pre-visible failures only when the
RequestRouter permits, and forwards visible output. It does not wait for capacity:
`CapacityBlocked` means call `next` again after capacity changes. It is synchronous,
single-owner and requires explicit `close()` even after exceptions or early exit;
if close fails retain the same driver and retry. Do not share or mutate its scheduler
or request. Blocking reads have no timeout or concurrent cancellation. Poll errors
propagate, and any dispatched session remains owned by the driver for cleanup.
Tests use a synthetic Python subprocess, not an AGY adapter or identity proof.

## Evidence and limits

`tests/test_lifecycle.py` uses an **injected fake backend** to test stream events,
identity rejection, EOF/errors, explicit close, repeated close, startup failures,
close failures, and release contention (including a real second SQLite writer).
Its static `primary` identity is a fixture label, not upstream identity evidence.

The cancellation test starts a real local Python child, waits for its ready signal,
terminates and waits for it, and asserts it has exited at the exact lease-release
boundary. It invokes neither AGY nor authentication. This proves ordering for that
**direct child only**, not descendant/process-tree termination or a live adapter.
Adapters that own descendants must independently prove shutdown of all owned work
before returning True. No multi-account readiness, auth isolation, process-tree
safety, Hermes tool-approval integration, or production readiness is claimed.

## Local RED → GREEN evidence

The inherited lifecycle work passed 52 tests on takeover; its earlier RED/GREEN
history was not independently replayed. Review added two observed RED → GREEN
regressions: typed startup failures were incorrectly finalized as unknown failures,
and KeyboardInterrupt/SystemExit bypassed cleanup during prepare/start/read.
The corrected suite passes 54 tests locally on Windows / Python 3.14.9. These are
local results, not a claim that the Linux/Python 3.11 CI matrix has run for this commit.
