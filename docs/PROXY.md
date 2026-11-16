# Local CLIProxyAPI connector

This opt-in connector targets an independently installed, running local OpenAI-compatible proxy. It does not install, start, authenticate, configure, or manage the proxy. The proxy selects the upstream credential/account; this project cannot pin, verify, rotate, or attribute individual proxy-managed accounts. The offline pool's scheduler, leases, fallback and lifecycle do **not** participate in proxy requests. Hermes makes inference calls directly through its existing custom provider, not this probe. No inference, live login, or real proxy identity has been tested here.

## Configuration

`examples/proxy.json` is a separate strict JSON schema from `examples/pools.json`: exactly `schema_version` (integer 1), `base_url` (HTTP loopback IP or localhost, explicit port, exact `/v1`, no userinfo/query/fragment), `api_key_env` (environment variable name only), `timeout_s` (finite number 0.1–30). No inline credentials. Redirects and environment HTTP proxy settings are disabled for the probe. Timeout is a socket operation timeout, not a guaranteed total wall-clock deadline. Model lists are limited to 1 MiB. Diagnostics never print response bodies or credential values.

Example in Windows PowerShell, from the checkout, after starting and authorizing your proxy yourself:

```powershell
# Set CLIPROXY_API_KEY in the process environment securely yourself; never commit it.
python -m agy_pool proxy-probe examples/proxy.json
python -m agy_pool proxy-hermes examples/proxy.json fixture-model
```

Replace `fixture-model` with an ID returned by the probe. `proxy-probe` makes `GET /v1/models` using the environment key as a bearer token; 401/403 means proxy authorization failed, and connection failure means the service is unreachable or timed out. A successful catalog does not prove inference, account selection, entitlement, tool calling, or stream behavior. `proxy-hermes` only prints a JSON fragment compatible with the documented Hermes `model` custom-provider settings; it does not edit or apply configuration or require the secret. Use `hermes model` to configure the custom endpoint interactively, or use the [Hermes custom provider instructions](https://hermes-agent.nousresearch.com/docs/integrations/providers) and the printed `key_env` reference. Ensure the environment variable is available to the Hermes process. Back up configuration before any manual change. Never put the actual key in this repository or command arguments.

Existing `validate` and `plan --dry-run` retain their offline pool semantics. Local fixture tests exercise real HTTP via a subprocess with a fake environment key; they do not run CLIProxyAPI or Hermes inference.
