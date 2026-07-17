# Feature specifications

This directory contains the project-owned GitHub Spec Kit artifacts. Each feature uses a numbered directory and keeps product intent, technical decisions, data design, verification, and implementation tasks reviewable with the code.

The active feature is selected by [`.specify/feature.json`](../.specify/feature.json). [`001-loan-document-platform`](001-loan-document-platform/spec.md) is the historical AWS-hosted baseline. The current packet, [`002-azure-api-control-plane`](002-azure-api-control-plane/spec.md), migrates the public/domain API to Azure while retaining the headless AWS IDP data plane.

Use the [project constitution](../.specify/memory/constitution.md) and [spec-driven workflow](../docs/spec-driven-development.md) before changing a feature. The root [OpenAPI contract](../contracts/openapi/loan-api.yaml) remains authoritative for HTTP paths and schemas; feature contract documents link to it instead of duplicating it.

Never place real mortgage documents, OCR text, extracted data, tenant/account identifiers, credentials, private keys, or deployment output in a feature directory. Use synthetic examples only.
