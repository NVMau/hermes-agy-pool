# Roadmap

All unchecked items are planned. No runtime capability is implied by this roadmap.

## Foundation
- [x] Public design repository and project identity
- [x] Architecture, policy example, provenance and contributor guidelines
- [x] Documentation validation workflow

## M0 — Identity feasibility
- [ ] Record supported AGY account/auth-store configuration
- [ ] Prove distinct identities A and B without exposing credentials
- [ ] Concurrent probes and restart/refresh isolation
- [ ] Missing account fails closed; host login remains unchanged
- [ ] Choose official isolated backend or explicitly narrow scope

## M1 — Pure routing engine
- [x] Strict configuration validation and immutable parsed models
- [x] Deterministic conditional plan CLI (offline simulation only)
- [x] Group membership and canonical model capability rules
- [x] Typed errors, reset hints and model-scoped cooldown
- [x] Same-model account failover and opt-in model fallback
- [x] Global attempt budget, visited pairs and deadlines

## M2 — Scheduler
- [x] SQLite atomic metadata reservations across cooperating processes
- [x] Shared-account hard reservation capacity across groups
- [x] Global reservation cap includes idle metadata (not measured live processes)
- [x] Bounded queue timeout/cancellation and eligible-account session affinity
- [x] Owner-scoped idempotent release; unknown/stale owners fail closed without heartbeat reclamation
- [ ] Verified live/idle worker lifecycle, termination proof and safe administrative stale-owner recovery

See [implemented scheduler scope and evidence](SCHEDULER.md); metadata multiprocessing tests do not satisfy live worker release gates.

## M3 — Backend and stream
- [ ] Explicit immutable account launch context
- [ ] Preserve Hermes tool approval semantics
- [ ] Sync and nonblocking async iteration
- [ ] Cancellation, EOF, abandonment and process cleanup
- [ ] Pre-output failover; no replay after any visible delta

## M4 — Hermes integration
- [ ] Separate provider registration and route discovery
- [ ] SDK compatibility and auxiliary call tests
- [ ] Outer retry ownership verified
- [ ] Redacted status and diagnostics CLI

## M5 — Release evidence
- [ ] Windows/Linux unit and fake-process integration CI
- [ ] Approved real-account smoke tests
- [ ] Measured worker count, cold/warm latency and memory
- [ ] Safe install/uninstall; original provider untouched
- [ ] Tagged prerelease with explicit limitations

## First implementation PRs

Each PR should take one vertical behavior through RED → GREEN → regression suite, not add a large layer of speculative abstractions.

| PR | Test-first target | Implementation path |
| --- | --- | --- |
| Identity evidence | A/B identity and absent-context failure | `docs/adr/0001-identity-boundary.md` |
| Config contract | `tests/test_config.py` | `agy_pool/config.py` |
| Error scope | `tests/test_errors.py` | `agy_pool/errors.py` |
| Explicit fallback | `tests/test_router.py` | `agy_pool/router.py` |
| Atomic capacity | `tests/test_state.py`, `tests/test_scheduler.py` | `agy_pool/state.py`, `agy_pool/scheduler.py` |
| Stream commit | `tests/test_stream.py` | `agy_pool/stream.py` |

Do not merge a fake backend as the production backend. Tests may use fixtures, but release evidence must distinguish fixtures from real upstream execution.
