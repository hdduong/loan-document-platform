# Feature contracts

This feature does not maintain a second public API definition.

- The [root OpenAPI contract](../../../contracts/openapi/loan-api.yaml) is authoritative for public paths, operations, Entra permissions, request and response schemas, platform identifiers, errors, idempotency, archive variants, upload initialization/completion, data-point reads, and artifact grants.
- The [runtime configuration schema](../../../contracts/runtime-config.schema.json) is authoritative for public SPA configuration, including the Azure API base URL and Entra identifiers.
- The [feature specification](../spec.md) defines the Azure ownership boundary and migration requirements that are not HTTP wire schemas.
- [Architecture](../../../docs/architecture.md) and [security controls](../../../docs/security.md) define storage, federation, exact-version, and telemetry invariants.

Any change to a public path or DTO must update the root OpenAPI, generated clients, implementation/contract tests, and affected feature artifacts in the same pull request. Copying the OpenAPI into this directory would create competing sources of truth and is a contract defect.

## Public boundary

The React SPA and approved service clients call only the Entra-protected Azure API hostname. They never call AWS API Gateway, AppSync, the optional IDP Jobs REST API, DynamoDB, Lambda, Step Functions, or an S3 service endpoint as a product API.

The only intentional browser-to-AWS data transfer is a short-lived, condition-constrained S3 upload or download grant returned by an already-authorized Azure API operation. A grant is an ephemeral response value, not a stable endpoint or AWS credential, and it must not be logged or persisted.

## Internal AWS execution seam

The Azure FastAPI host owns cryptographic authorization, bounded HTTP handling,
correlation IDs, and mapping sanitized host/dependency failures to the root
OpenAPI problem format. It passes a normalized, validated request envelope to
the retained lifecycle dispatch entry point. The lifecycle module still owns
the domain rules, platform identity allocation, idempotency, archive sequencing,
response shaping, and its DynamoDB/S3/Lambda SDK operations; this migration does
not claim that module is boto3-free or independent of the retained normalized
Lambda-compatible envelope.

Before each serialized domain dispatch, the Azure host rebinds those AWS SDK
clients to the current botocore refreshable session obtained through managed
identity and STS. No caller bearer token, static AWS credential, or public AWS
endpoint crosses this seam. A future domain-port refactor may replace these SDK
operations with repository/gateway interfaces without changing the public
contract, but such interfaces are not asserted as part of this feature.

The initial adapter supports these capability groups:

| Capability | Internal responsibility | Required invariant |
|---|---|---|
| Federated AWS session | Obtain a managed-identity token for the dedicated federation audience, exchange it through STS, cache/refresh temporary credentials, and create scoped AWS clients | Never exchange the caller's bearer token; trust and tests pin issuer, audience, and managed-identity subject |
| Registry | Read/query and conditionally transact the retained DynamoDB loan, document, upload, idempotency, archive, and artifact-reference records | DynamoDB remains the only mutable registry during this migration; conditional/idempotent semantics remain authoritative |
| Quarantine upload | Create a presigned POST for the one opaque key and declared PDF metadata, then inspect the exact completed object version | Enforce content type, size, checksum, KMS fields, short expiry, and exact `VersionId`; no PDF bytes traverse Azure |
| Processing reconciliation | Record client completion and invoke/reconcile only the retained upload processor needed for a clean-version workflow | Duplicate or reordered completion/scan facts may advance only the same current object version |
| IDP submission/status | Preserve the mapping between the platform processing execution and the S3/event-driven headless IDP workflow | Only the AWS validation processor stages a malware-clean, parser-valid exact version to IDP; no AppSync or Jobs REST dependency |
| Artifact read/grant | Read bounded JSON or issue a short-lived presigned GET for an exact authorized artifact version | Azure authorizes ownership first; bucket/key/version/checksum/media type must match the registry reference |

These are implementation capabilities, not new HTTP endpoints. Exact Python method names may evolve without changing the public contract, provided the invariants and tests remain intact.

## Headless IDP contract

The pinned IDP deployment uses its supported S3/event integration:

1. The retained AWS upload processor copies only a validated, malware-clean exact version to the IDP input bucket with the reviewed screening configuration version.
2. Screening uses `screen/{processingExecutionId}/{documentId}/{uploadId}.pdf`; the selected-page full-extraction input uses `full/{processingExecutionId}/{documentId}/{uploadId}.pdf`. Both deterministic keys are registry mappings, not public identifiers.
3. The IDP workflow and postprocessor record upstream object/execution identities separately from platform `documentId`, `uploadId`, and `processingExecutionId`.
4. The postprocessor materializes versioned selected/data-point artifacts and updates the retained registry.
5. The Azure API obtains status and artifacts from that registry and exact S3 versions after product authorization.

This feature does not contract against AppSync because `deploymentMode: headless` removes it. It also does not contract against the optional Jobs REST API, which is a separate private-VPC/Cognito deployment and is not enabled or required. Adding either interface requires a separate feature, least-privilege review, contract tests, and an intentional change to the pinned deployment model.

## Identifier and failure rules

- `loanInstanceId`, `documentId`, `uploadId`, and `processingExecutionId` are platform identities allocated by the Azure API.
- S3 `VersionId`, bucket/key, IDP input object identity, and workflow ARN are upstream coordinates and must never be substituted for a platform identity.
- Public responses disclose only fields defined by the root OpenAPI. Internal AWS coordinates stay in registry/provenance records except for opaque signed grant fields already defined by that contract.
- Authorization failures occur before the adapter is invoked.
- STS, DynamoDB, S3, Lambda, event, and IDP failures become sanitized, retry-safe problem responses or processing states. They must not expose tokens, credentials, signed URLs, sensitive filenames, raw AWS errors, workflow inputs/outputs, document text, or extracted values.
