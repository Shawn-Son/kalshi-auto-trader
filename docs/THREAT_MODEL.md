# Threat model

## Protected assets

- RSA private key and API key ID.
- Cash, positions, and order-placement authority.
- Signal integrity and freshness.
- Local order journal and audit logs.

## Main failure modes

- Credential theft: secrets are loaded from environment variables and a file path, never TOML.
  Production deployment should use an OS or cloud secret manager and a minimally privileged user.
- Duplicate orders: every intent has a durable client order ID before submission; retries preserve
  that ID.
- Stale/corrupt signals: schema, range, timestamp, model version, and age are validated.
- Runaway sizing: integer pre-trade limits fail closed in Python or the optional C++ kernel.
- State divergence: ambiguous requests become `unknown`; they are not assumed rejected.
- Operator error: paper/demo/live separation and three independent production interlocks.
- Dependency/API drift: pin deployed artifacts, monitor Kalshi's changelog, and run contract tests.

## Out of scope

This repository does not secure a compromised host, prove that a probability model is correct,
provide high availability, calculate taxes, interpret market settlement rules, or satisfy every
jurisdiction's legal requirements.
