# Contracts

The canonical machine-readable contract for this feature is committed at
`contracts/schemas/idp-image-release.schema.json`. It is intentionally outside the
feature directory so CI, deployment scripts, and future features use one schema.

The schema is a closed object for release metadata and requires exactly the
15 logical images from `config/idp/images.json`. Digest-only image URIs,
Linux ARM64, scan completion, provenance, SBOM evidence, and source/overlay
identity are mandatory deployment inputs.
