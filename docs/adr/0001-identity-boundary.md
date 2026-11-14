# ADR 0001: Separate routing from credential identity

Status: accepted architectural constraint; concrete AGY identity mechanism unresolved.

## Context

Credential pooling is established in API proxy architectures. An external CLI may instead resolve a shared operating-system login, regardless of per-process HOME. Conflating the two risks routing multiple account aliases to one actual credential.

## Decision

The routing engine handles opaque account references. A backend must verify identity isolation, use the same context for discovery and inference, and fail closed when the requested identity is absent. It must not read/copy the current host token merely to satisfy the router.

Official CLI-supported isolated stores are preferred if verified. Separate principals or isolated worker environments are possible alternatives with explicit operator consent. A future API backend needs its own supported auth and transport design; it is not implicitly part of MVP.

## Acceptance evidence

Distinct A/B identity evidence (not just different model catalogs); concurrent requests; restart/refresh isolation; missing-context failure; host login unchanged. Keep evidence redacted and never commit credentials or account email addresses.

## Consequences

The pure router can be tested independently. Multi-account runtime readiness cannot be claimed until this gate passes. This costs early investigation but prevents a misleading pool implementation.
