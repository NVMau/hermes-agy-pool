# Architecture

Status: strict configuration, offline conditional planning, in-memory runtime policy and atomic metadata scheduling implemented; backend, live worker and provider architecture remains proposed. See [scheduler contract](SCHEDULER.md). No inference implementation is shipped. See [implemented contract](CONFIG.md).

## Design constraints

Keep the routing engine independent of credential storage and transport. Start with one AGY backend; do not build a generic gateway or plugin framework inside this plugin. Authenticated cloud proxy operation demonstrates a viable multi-credential architecture, not interchangeability with AGY's OS keyring.

The provider will register as `antigravity-pool`, separate from `antigravity-oauth`. No core patches or edits to the existing provider are required by design. Verify the actual SDK contract before implementation.

## Boundaries

| Module (planned) | Responsibility | Must not do |
| --- | --- | --- |
| `agy_pool/config.py` | Strict config schema and references | Accept secrets or arbitrary shell commands |
| `agy_pool/capabilities.py` | Account/model capabilities and negative cache | Treat unknown support as guaranteed |
| `agy_pool/errors.py` | Narrow typed failure classification | Turn unknown errors into quota/context errors |
| `agy_pool/router.py` | Route order, budget and fallback decisions | Retry after visible stream output |
| `agy_pool/state.py` | Atomic SQLite metadata transactions | Store tokens, prompts or raw stderr |
| `agy_pool/scheduler.py` | Shared capacity, affinity and queue deadline | Spawn unbounded one-shot workers |
| `agy_pool/backends/base.py` | Identity and worker contract | Assume a HOME is an identity |
| `agy_pool/backends/agy.py` | Verified AGY launch context | Mutate process-global environment |
| `agy_pool/stream.py` | Sync/async lifecycle and stream commit | Release leases when create() merely returns |
| `agy_pool/provider.py` | Supported Hermes provider API | Perform I/O during import |

## Routing algorithm

1. Resolve an exact route; reject unknown routes locally.
2. Iterate the configured flat model list in order.
3. Select only enabled, identity-verified accounts within the route's group.
4. Filter by capabilities, account/model health, cooldown and hard capacity.
5. Prefer a healthy session binding; otherwise use round-robin.
6. Acquire a lease atomically, including shared account and global worker limits.
7. Execute using immutable request and account context.
8. On a retryable pre-output failure, retire the failed worker and try an unvisited account/model pair.
9. Move to another model only for an explicitly allowed failure category.
10. Stop on deadline, attempt budget, invalid request or visible output failure.

A group is a routing boundary, not an authorization sandbox. Shared accounts across groups share capacity and cooldown. Aliases resolving to one canonical model share failure state.

## Failure semantics

| Failure | Scope | Pre-output response |
| --- | --- | --- |
| Unsupported / missing entitlement | Account + canonical model | Try another account; explicit model fallback only |
| Explicit quota | Documented upstream scope | Honor reset hint; select another eligible account |
| Capacity/rate limit | Model unless account scope is proven | Bounded cooldown; no blind full-account disable |
| Invalid authentication | Account | Mark unavailable pending reauthentication |
| Invalid schema/request | Request | Stop without rotating credentials |
| Context overflow | Request | Surface to Hermes compression policy |
| Unknown failure | Request | Stop with sanitized diagnostic |
| Error after visible output | Stream | Stop; never concatenate a replacement completion |

A single plugin-owned attempt budget spans creation and iteration. Hermes outer retries require an integration contract so partial output cannot trigger replay at another layer.

## Identity backend contract

- `verify_identity(identity_ref)` returns redacted proof of a supported identity boundary.
- `discover_models(identity_ref)` uses the same context as inference.
- `open(identity_ref, session_id, model, effort)` returns a single-owner worker.
- No fallback to the host login if the selected identity is absent.
- No token data enters router state, diagnostics or configuration.

An official isolated auth store, isolated OS principal or isolated worker environment may satisfy the contract after verification. An API credential backend is a future option only after its supported authentication protocol and maintenance cost are assessed; do not quietly replace official AGY with reverse-engineered OAuth.

## Worker and state lifecycle

Use profile-local metadata-only SQLite and short transactions, never inference inside a transaction. Track reservations, cooldowns, affinity, cursors and capability expiry. Count idle persistent workers against the global cap. Key workers by identity/session/model/effort/rules digest; do not share histories across sessions.

Keep the lease until EOF, explicit close, cancellation or error. Use owner UUID plus process-start identity; a stale heartbeat alone cannot prove a process has stopped. Ambiguous ownership fails closed until termination is confirmed. Reaping deletes only plugin-owned temporary workspaces, never authentication stores.

## Security and observability

Hermes remains the tool executor and approval authority. Diagnostics expose account aliases, route, resolved model, attempt, latency and reason codes — not email addresses, tokens, prompts, completions or raw auth output. Backend launch configuration is trusted administrative input, not request input.

## Release gates

Real A/B identity proof; missing-identity negative test; bounded multi-process concurrency; stream cancellation; no replay after text/reasoning/tool fragments; preserved required capabilities; sync/async/auxiliary Hermes integration; original host login unchanged. Fixture tests and documentation CI do not satisfy live acceptance.
