# Contributing

Read `CLAUDE.md`, the [project constitution](.specify/memory/constitution.md), the active feature specification/plan/tasks, the OpenAPI contract, architecture, and security documentation before changing behavior. Follow the [Spec Kit workflow](docs/spec-driven-development.md) and reconcile artifacts whenever implementation changes their documented state. Authorized maintainers work on a short-lived branch, add focused tests, and submit a pull request. External contributions are not accepted until the owner adopts an explicit contribution and source license. Keep generated API types derived from OpenAPI and keep the React application inside `apps/web`.

Before review, run the repository validator, Python tests/lint, OpenAPI validation, CloudFormation lint, and—once present—the React lint/test/build suite. Use only synthetic data. IDP configuration changes must update their reviewed digest and include accuracy, selection, latency, and cost regression evidence.
