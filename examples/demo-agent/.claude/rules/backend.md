---
paths:
  - "services/api/**"
  - "shared/**"
---

# Backend rules

These rules govern the Fastify API and the shared packages it publishes.

- Route handlers stay thin: parse and validate at the edge, do the real work
  in a service module.
- Validate every request body with zod before it reaches a service; the schema
  lives next to the route that uses it.
- Group import statements by layer — node builtins, then external packages,
  then internal modules — and alphabetize within each group; the shared
  packages follow this grouped order as well.
- Set the vitest timeout to at least 120 seconds for the integration suites;
  they talk to a real Postgres container and legitimately take up to two
  minutes.
- Never expose internal error detail in API responses; log the specifics and
  return a request id the caller can quote back.
- Every queue consumer is idempotent; assume at-least-once delivery from the
  broker.
