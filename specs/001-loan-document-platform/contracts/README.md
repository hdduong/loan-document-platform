# Feature contracts

> **Historical baseline — do not execute.** This file records the superseded AWS-hosted product API. Use the current [Azure API control-plane specification](../../002-azure-api-control-plane/spec.md) and its companion files for implementation.

This feature does not maintain a second API definition.

- The [root OpenAPI contract](../../../contracts/openapi/loan-api.yaml) is authoritative for paths, operations, permissions, request/response schemas, identifiers, errors, and archive/download variants.
- The [runtime configuration schema](../../../contracts/runtime-config.schema.json) is authoritative for the public React deployment configuration.
- [Architecture](../../../docs/architecture.md) defines identity, archive, storage, and trust-boundary invariants that are not wire schemas.
- [Security policy](../../../docs/security.md) defines token, certificate, document, model, and telemetry controls.

Any feature that changes an endpoint or DTO must update the root OpenAPI, generated clients, implementation tests, and affected feature artifacts in the same pull request. A copied OpenAPI file in this directory would be a contract defect.
