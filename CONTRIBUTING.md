# Contributing

This is an offline pure-core project, not a live provider. Start with a focused issue or small PR aligned with the [roadmap](docs/ROADMAP.md).

## Engineering rules

1. Write a behavior test, observe its expected failure, implement minimally, then run the regression suite.
2. Keep one fallback owner; never replay after a visible stream delta.
3. Keep identity, routing policy and transport separate.
4. No secrets, personal filesystem paths, account email addresses or real auth dumps in fixtures.
5. Do not change the user's existing provider or login.
6. Preserve licenses when adapting upstream code; record the exact source revision.
7. State whether evidence is a unit fixture, subprocess simulation or real upstream request.

Use Python 3.11+ from the repository root; no third-party dependencies. Local checks:

```sh
python -m unittest discover -s tests -v
python -m agy_pool validate examples/pools.json
python -m agy_pool plan examples/pools.json interactive/quality --dry-run
git diff --check
```

Future code PRs must add their exact test/setup commands rather than relying on hypothetical commands from the design docs. Keep PRs narrow and describe failure cases, cancellation behavior and security impact.
