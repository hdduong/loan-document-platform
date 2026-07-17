# Spec Kit project files

This repository is initialized as a single root-level [GitHub Spec Kit](https://github.com/github/spec-kit) project using the Claude integration and PowerShell scripts. The immutable upstream version is recorded in `../vendor/spec-kit.lock.json`; generated metadata is recorded in `init-options.json` and `integration.json`.

## Managed infrastructure

The following files are generated and versioned together:

- `scripts/powershell/` — feature discovery and artifact setup scripts used by the Claude skills.
- `templates/` — canonical templates for specifications, plans, tasks, checklists, and the constitution.
- `integrations/` — hashes and integration state used to detect missing or locally modified generated files.
- `workflows/` — the installed Spec Kit workflow definition and registry.
- `../.claude/skills/` — Claude Code entry points generated from Spec Kit command templates.

Do not edit generated skills, scripts, integration manifests, or templates by hand. Make a deliberate, pinned Spec Kit upgrade and review the generated diff instead.

## Project-owned artifacts

`memory/constitution.md` is the governing project constitution. It is not disposable generated content: review it like source code and preserve it during upgrades.

`feature.json` records the active feature directory so Claude skills work on custom Git branch names without relying on branch parsing. It currently selects the brownfield baseline at `specs/001-loan-document-platform` and should change with the feature on feature-specific branches.

Feature artifacts live outside this directory under `specs/<number>-<feature>/`:

- `spec.md` defines user scenarios, requirements, edge cases, and measurable success criteria.
- `plan.md` defines the technical design and constitution checks.
- `research.md` records decisions and resolved unknowns.
- `data-model.md` defines entities, identity, lifecycle, and validation rules.
- `quickstart.md` gives an executable validation path.
- `contracts/` contains feature-specific contract artifacts or links to canonical contracts.
- `tasks.md` is the dependency-ordered implementation checklist.
- `checklists/*.md` contains optional requirements-quality checklists.

For this project, `contracts/openapi/loan-api.yaml` at the repository root remains the canonical HTTP API. A feature contract document should link to it instead of creating a divergent OpenAPI copy.

## Windows and PowerShell

The integration was initialized with `--script ps`. Core skills therefore call `.specify/scripts/powershell/*.ps1` from the repository root. Use PowerShell and keep paths project-relative with forward slashes inside Spec Kit configuration.

Useful integrity commands are:

```powershell
specify version
specify integration status --json
specify check
```

To refresh an equivalent checkout from the immutable upstream commit and run repository validation:

```powershell
./scripts/sync-spec-kit.ps1
```

Do not run a force refresh casually: it replaces shared generated files. The current initializer preserves an existing constitution, but commit or back it up first and review every resulting change.

## Agent context extension

Current Spec Kit does not modify `CLAUDE.md` during core initialization. Agent-context synchronization is an optional extension and is not required by this repository. If it is installed later, configure it to manage only the root `CLAUDE.md`; the root instructions and skills are already inherited by sessions started in `apps/web`.

The extension's PowerShell updater discovers an explicit plan path first, then `.specify/feature.json`, then the most recently modified `specs/*/plan.md`. It replaces only its delimited block and preserves the rest of the context file.

See the [Spec Kit core command reference](https://github.github.com/spec-kit/reference/core.html), [integration reference](https://github.github.com/spec-kit/reference/integrations.html), and [monorepo guidance](https://github.github.com/spec-kit/guides/monorepo.html).
