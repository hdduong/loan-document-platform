# Spec-driven development

This repository uses GitHub Spec Kit to keep requirements, design decisions, tasks, implementation, and verification traceable. It is a brownfield workflow: feature artifacts explain and evolve the production design, while the existing security controls and canonical contracts remain authoritative until deliberately changed together.

## Source authority

Use this order when sources disagree:

1. `.specify/memory/constitution.md` and `docs/security.md` define governance and mandatory production controls.
2. `contracts/openapi/loan-api.yaml` and `contracts/runtime-config.schema.json` define externally consumed contracts.
3. `docs/architecture.md` defines identity, lifecycle, processing, and trust-boundary invariants.
4. The active feature's `spec.md` defines approved behavior and acceptance criteria within those constraints.
5. The active feature's `plan.md` and `tasks.md` define implementation choices and work order.
6. Supporting feature research, models, quickstarts, and general documentation explain decisions and verification.

A feature may intentionally change a higher-authority source, but the change is incomplete until that source, affected tests, and downstream documentation are updated in the same review. Never resolve a disagreement by silently implementing the lower-authority artifact.

## Artifact lifecycle

Each feature lives at `specs/<number>-<short-name>/`. `.specify/feature.json` selects the active directory independently of the Git branch name. The feature then progresses through these stages:

1. Specify: write `spec.md` with prioritized user stories, independent acceptance scenarios, edge cases, functional requirements, and measurable success criteria. Keep technology choices out of the requirement unless they are genuine constraints.
2. Clarify: resolve material ambiguities before planning. Record each answer in the specification rather than leaving decisions only in chat history.
3. Plan: create `plan.md` and supporting `research.md`, `data-model.md`, `quickstart.md`, and `contracts/` artifacts. Complete the constitution checks before design and re-check them after design.
4. Tasks: create `tasks.md` organized by independently testable user story. Every task must name the file or artifact it changes and identify safe parallel work.
5. Analyze: check requirement, design, task, and constitution coverage before implementation.
6. Implement: execute tasks in dependency order, mark only completed and verified work as complete, and keep tests and documentation with the behavior they cover.
7. Converge: compare the repository with the specification, plan, and task list; append concrete remaining work rather than declaring completion with unexplained gaps.

Optional checklists assess whether requirements are complete, clear, consistent, and testable. They validate the quality of the written requirements, not whether the implementation happens to pass tests.

## Claude Code workflow

The generated Claude skills live under `.claude/skills/` and use hyphenated names:

```text
/speckit-constitution
/speckit-specify <feature description>
/speckit-clarify
/speckit-plan <technical constraints and architecture guidance>
/speckit-tasks
/speckit-analyze
/speckit-implement
/speckit-converge
```

Run Claude Code from the repository root for cross-cutting platform work. A session started in `apps/web` inherits the root skills and root `CLAUDE.md`, then applies `apps/web/CLAUDE.md` to UI work.

Before changing behavior, Claude must read the constitution and the active feature's specification, plan, and tasks. For UI work it must also read the OpenAPI contract and UI handoff. For API or processing work it must read the architecture and security documents relevant to the task.

## Brownfield rules for this repository

- Preserve stable identifiers and archive semantics. A plan cannot redefine `loanId`, `loanInstanceId`, `documentId`, `uploadId`, or processing execution identity without an explicit architecture and contract change.
- Preserve the two-pass IDP strategy and the full extraction accuracy baseline unless regression evidence supports a reviewed change.
- Reference the root OpenAPI contract from feature contracts. Do not maintain a second copy of endpoint paths or DTOs.
- Keep real mortgage documents, OCR text, extracted PII, deployment identities, credentials, and private keys out of feature artifacts and examples.
- Use synthetic values in scenarios and quickstarts.
- Treat an archived loan as an immutable loan-instance freeze; do not turn planning prose into an object-copy workflow.
- Keep the browser as a public client using Entra authorization code with PKCE. A feature specification cannot authorize browser-held secrets or AWS credentials.

## Review and completion

A feature is ready to implement when:

- no material clarification remains;
- every prioritized user story has independent acceptance scenarios;
- the plan passes constitution and security checks;
- contract and data-model effects are explicit;
- tasks cover each requirement and required test;
- dependencies and parallel tasks are correctly marked.

A feature is complete when:

- its acceptance scenarios are demonstrated by tests or a documented verification step;
- canonical contracts and documentation match the implementation;
- security, repository, and infrastructure validation pass;
- the task list reflects actual completion;
- `/speckit-converge` finds no unexplained required work.

Do not commit local workflow run state, Claude local settings, credentials, or generated deployment outputs. Keep project-owned specifications and reviewed feature artifacts in version control.

## Upstream references

- [GitHub Spec Kit](https://github.com/github/spec-kit)
- [Core command reference](https://github.github.com/spec-kit/reference/core.html)
- [Claude integration reference](https://github.github.com/spec-kit/reference/integrations.html)
- [Using Spec Kit in a monorepo](https://github.github.com/spec-kit/guides/monorepo.html)
- [Claude Code project memory](https://code.claude.com/docs/en/memory)
- [Claude Code skills](https://code.claude.com/docs/en/slash-commands)
