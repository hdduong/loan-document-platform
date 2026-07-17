# Feature specifications

This directory contains the project-owned GitHub Spec Kit artifacts. Each feature uses a numbered directory and keeps product intent, technical decisions, data design, verification, and implementation tasks reviewable with the code.

The active feature is selected by [`.specify/feature.json`](../.specify/feature.json). The current packet, [`001-loan-document-platform`](001-loan-document-platform/spec.md), is a brownfield baseline: it records what the repository already implements and leaves real UI, cloud deployment, and operational acceptance work unchecked.

Use the [project constitution](../.specify/memory/constitution.md) and [spec-driven workflow](../docs/spec-driven-development.md) before changing a feature. The root [OpenAPI contract](../contracts/openapi/loan-api.yaml) remains authoritative for HTTP paths and schemas; feature contract documents link to it instead of duplicating it.

Never place real mortgage documents, OCR text, extracted data, tenant/account identifiers, credentials, private keys, or deployment output in a feature directory. Use synthetic examples only.
