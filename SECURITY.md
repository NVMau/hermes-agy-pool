# Security policy

This project is pre-release and has no production support promise.

Never post tokens, OAuth payloads, prompts, account addresses or raw authentication logs in an issue. Report sensitive findings through GitHub private vulnerability reporting if enabled; otherwise contact the maintainer privately before disclosing details publicly.

Authentication remains backend-owned. Routing metadata must not contain credentials. Account pooling must respect provider terms, entitlements and reset hints. Model/tool output is untrusted; Hermes remains the tool approval boundary.
