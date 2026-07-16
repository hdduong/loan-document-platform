## What changed

<!-- Describe the user-visible or operational outcome. -->

## Security and data review

- [ ] No PDFs, OCR text, extracted mortgage data, tokens, signed URLs, private keys, secrets, or deployment outputs are included.
- [ ] Identity, archive, immutable S3 version, and idempotency invariants remain intact.
- [ ] IDP config/model changes include regression and cost evidence.
- [ ] Infrastructure changes were reviewed as a CloudFormation change set.

## Validation

<!-- List tests, contract checks, and synthetic validation performed. -->

- [ ] Every affected production Python file remains at least 80% line-covered individually and overall.
- [ ] Every affected React/TypeScript production file remains at least 80% covered per file for statements, lines, functions, and branches, or no UI source changed.
- [ ] Playwright integration tests were added or updated for affected browser journeys and passed, or no browser behavior changed.
- [ ] Coverage thresholds, source inclusion, and exclusions were not weakened.
