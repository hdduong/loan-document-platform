# Security policy

This public repository implements mortgage-document workflows and must be treated as security-sensitive even though source control contains no customer documents, account/tenant identifiers, or deployment credentials.

Report suspected vulnerabilities through GitHub **Security → Advisories → Report a vulnerability**, which is enabled for private disclosure. Do not open a public issue containing tokens, tenant/account identifiers, document content, extracted values, signed URLs, private keys, or exploit details against a deployed environment.

Never commit production or test-customer PDFs, OCR text, extraction output, `.env` files, access tokens, AWS keys, certificate private keys, presigned URLs, or deployment state. Use synthetic documents and identities for automated tests. If sensitive material is committed, stop distribution, rotate affected credentials, remove the object from Git history, and follow the incident process; deleting only the latest file is insufficient.

Production changes require reviewed pull requests, passing validation, an inspected CloudFormation change set, and a gated GitHub environment deployment. GitHub authenticates to AWS with OIDC-derived temporary credentials; long-lived AWS access keys are prohibited.
