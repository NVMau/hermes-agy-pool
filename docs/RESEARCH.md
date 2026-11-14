# Research notes

Research snapshot: 2026-11-14. These are design inputs, not proof of runtime compatibility.

## CLIProxyAPI

Repository: https://github.com/router-for-me/CLIProxyAPI

Inspected revision: `1aaf56bc6a0fc6fce19049ba7ae8554c882304a6`.

| Topic | Source at inspected revision |
| --- | --- |
| Configuration, aliases, routing | [config.example.yaml](https://github.com/router-for-me/CLIProxyAPI/blob/1aaf56bc6a0fc6fce19049ba7ae8554c882304a6/config.example.yaml) |
| Selection | [selector.go](https://github.com/router-for-me/CLIProxyAPI/blob/1aaf56bc6a0fc6fce19049ba7ae8554c882304a6/sdk/cliproxy/auth/selector.go) |
| Cooldowns | [conductor_cooldown.go](https://github.com/router-for-me/CLIProxyAPI/blob/1aaf56bc6a0fc6fce19049ba7ae8554c882304a6/sdk/cliproxy/auth/conductor_cooldown.go) |
| Model candidates | [conductor_models.go](https://github.com/router-for-me/CLIProxyAPI/blob/1aaf56bc6a0fc6fce19049ba7ae8554c882304a6/sdk/cliproxy/auth/conductor_models.go) |
| Streaming | [conductor_stream.go](https://github.com/router-for-me/CLIProxyAPI/blob/1aaf56bc6a0fc6fce19049ba7ae8554c882304a6/sdk/cliproxy/auth/conductor_stream.go) |

Useful ideas: eligible-account filtering; independent account/model failure states; bounded candidate attempts; first-payload bootstrap; explicit aliases and namespaces. Do not assume its OAuth path has universal arbitrary-model fallback, or copy historical config fields into a new schema.

## Hermes

- [Model provider plugin API](https://hermes-agent.nousresearch.com/docs/developer-guide/model-provider-plugin)
- [Public plugin API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/public-api)
- [Credential pools](https://hermes-agent.nousresearch.com/docs/user-guide/features/credential-pools)
- [Fallback providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers)

Core credential rotation does not automatically select a different external CLI identity. Avoid nested retry loops and relying on soft capacity limits for a hard process cap.

## Existing AGY provider

Reference: https://github.com/neerazz/hermes-antigravity-oauth

The inspected local provider uses official AGY processes, one persistent worker per client and one-shot processes on contention. Its Windows credential probe references `gemini:antigravity`; isolated HOME directories alone do not isolate that OS-level credential. Local sources may include changes not represented by the upstream revision, so implementation must pin and review the source actually reused.

## Cloud proxy vs local CLI

A functioning multi-account proxy establishes that credential pooling is feasible in its own auth/transport architecture. It does not establish that AGY can select those credentials, that exporting them is supported, or that a new plugin can reuse their storage unchanged. Preserve the proven routing ideas; independently verify each backend boundary.
