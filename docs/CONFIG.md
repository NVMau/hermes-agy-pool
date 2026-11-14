# Implemented offline contract

Run the commands in the README from a checkout with Python 3.11+. No dependencies, credentials, network calls or Hermes installation are needed. The public validated construction boundary is `load_config(path)` / `parse_config(text)`; direct dataclass construction is not validation.

## Strict schema version 1

The complete schema is illustrated by `examples/pools.json`. Every shown field is required; additional fields at any object level, duplicate JSON keys and duplicate list entries are rejected. All three named sections must be nonempty. JSON must be UTF-8; NaN and Infinity are rejected.

- IDs use ASCII `[A-Za-z0-9][A-Za-z0-9_.-]*`. Route IDs additionally allow slash-separated ID segments. No whitespace, paths or normalization. Model IDs are exact opaque identifiers; there is no alias resolution or model registry yet.
- `schema_version`: integer 1, not boolean or float.
- `limits`: positive integers `max_workers`, `max_attempts`; positive finite numbers `queue_timeout_s`, `request_timeout_s`. Booleans are never numbers.
- Accounts: `backend` must be `agy` (a declared intent, not a working backend), `identity_ref` is an opaque ID, `max_concurrency` is a positive integer, `enabled` is a JSON boolean.
- Groups: nonempty ordered `accounts` with valid references; `selection` must be `sticky_round_robin`. This policy is validated but not executed by the planner.
- Routes: existing `group`, nonempty unique ordered `models`, explicit `fallback_on` and `required_capabilities` arrays. Empty fallback means no model fallback. Empty capabilities means no extra declared requirements, not proof of model availability.
- Fallback vocabulary: `unsupported_model`, `quota`, `capacity`. Unknown failures, authentication errors, invalid requests, context overflow and post-output failures cannot grant fallback permission.
- Capability vocabulary: `tools`, `vision`, `schema`. These are requirements, never inferred support.

Parsed models are frozen dataclasses with tuple sequences and read-only mapping wrappers. Account aliases may appear in multiple groups; future scheduling must share their limits.

## Conditional planning semantics

`plan_route(config, route_id)` is deterministic and performs no I/O. Unknown routes raise `PlanError`. It returns a frozen `Plan` labeled `simulation`:

1. Only enabled account aliases in the exact route group appear, in configured order. All-disabled groups yield no stages, never a different group.
2. A stage contains one model and all candidate accounts for that model. Exhaust eligible accounts in that stage before considering the next stage.
3. Later stages exist only when `fallback_on` is nonempty. Each is gated by `requires_failure`: an explicitly allowed pre-output failure from the preceding stage after same-model candidates are exhausted. A successful attempt stops; this is not a list to execute unconditionally.
4. `required_capabilities` applies to **every** account/model pair in **every** stage. Unknown support is not eligible for live execution. Identity verification, capability verification, health/cooldown/capacity and pre-output safety appear as unresolved prerequisites. The planner has no discovery backend and therefore does not claim any candidate is runnable.
5. `max_attempts` is reported, not consumed or enforced. Stages are alternatives, so their total pair count may exceed this budget. Deadlines, leases, affinity, round-robin cursors and failure classification remain unimplemented.

CLI success is exit 0. Configuration, file, route and usage errors return exit 2, without raw configuration contents or tracebacks. `plan` requires `--dry-run`. No command sends inference, reads credentials or launches AGY.

## In-memory runtime policy semantics

The in-memory policy modules separate transport execution from routing decisions:

1. **Structured Errors (`agy_pool.errors`)**:
   - `Kind`: `UNKNOWN`, `INVALID_REQUEST`, `CONTEXT`, `AUTH`, `UNSUPPORTED_MODEL`, `QUOTA`, `CAPACITY`.
   - `Scope`: `UNKNOWN`, `ACCOUNT`, `MODEL` (account + canonical model).
   - `Failure(kind, scope, retry_after_s)` enforces finite nonnegative durations. Non-retryable kinds (`UNKNOWN`, `INVALID_REQUEST`, `CONTEXT`) fail closed.

2. **Capability Catalog (`agy_pool.capabilities`)**:
   - `Catalog(aliases, verified_accounts, support)`: frozen snapshot with read-only mappings. Aliases map to canonical models and cannot chain or self-reference.
   - Unknown capabilities or unverified accounts/models are strictly ineligible for routing.

3. **Health & Cooldown (`agy_pool.health`)**:
   - In-memory `Health(catalog, clock, cooldown_s)` supports pluggable deterministic fake clocks.
   - Conservative failure scoping: if an error scope is `UNKNOWN`, the cooldown applies across the entire account. Model-scoped errors only cool down that account + canonical model pair.
   - Authentication failures (`Kind.AUTH`) place the account in quarantine until administrative `reset_auth(account)`.
   - Upstream retry hints (`retry_after_s`) override default cooldowns.

4. **Request State Machine (`agy_pool.runtime`)**:
   - `RequestRouter(config, route_id, health, deadline=None)` coordinates single-request lifecycle (`READY`, `IN_FLIGHT`, `SUCCEEDED`, `STOPPED`).
   - Prioritizes accounts within the route's configured group on the primary model before testing explicit `fallback_on` conditions.
   - Tracks visited `(account, canonical_model)` pairs and halts when `limits.max_attempts` or request deadline is met.
   - Never re-attempts or falls back if visible stream fragments (`Fragment.TEXT`, `Fragment.REASONING`, `Fragment.TOOL`) were emitted (`stop_reason='visible_output'`).

## Local TDD evidence

Executed with Windows Python 3.14.9 using `python -m unittest discover -s tests -v`:

| Slice | Observed RED | Observed GREEN |
| --- | --- | --- |
| Immutable config loading | 1 test failed: config package missing | 1 test passed |
| Strict JSON/schema validation | 2 tests, strict parser missing | 2 tests passed, including invalid-input subtests |
| Conditional plan shape | 3 tests, planner missing | 3 tests passed |
| Fail-closed planning boundaries | 6 tests, 3 failures (disabled account, empty fallback, missing typed error) | 6 tests passed |
| CLI subprocess behavior | 8 tests, 5 failures because module entrypoint absent | 8 tests passed |

CLI smoke commands returned `valid: true` / `mode: offline-validation`, and a two-stage `mode: simulation` plan for `interactive/quality`, with tools required and `unsupported_model` gating the second stage.

CI is configured for Ubuntu/Windows and Python 3.11/3.14. Action pins were checked read-only with `git ls-remote`: checkout v4.2.2 = `11bd71901bbe5b1630ceea73d27597364c9af683`; setup-python v6 = `ece7cb06caefa5fff74198d8649806c4678c61a1`. CI results are not claimed here: this milestone was committed locally, not pushed. No live auth or multi-account readiness is demonstrated.
