# Contributing to mcp-schema-normalize

Thanks for considering a contribution.

## Scope

This repo implements a JSON Schema 2020-12 → llama.cpp-grammar-compatible-subset normalizer, plus optional gateway integrations (currently LiteLLM). Before opening an issue or PR, check whether your concern belongs here vs. upstream:

| Concern | Right home |
|---|---|
| A new schema pattern llama.cpp rejects that we don't yet handle | **here** — open an issue with a minimal repro schema |
| LiteLLM proxy config / callback registration | LiteLLM core (https://github.com/BerriAI/litellm) |
| llama.cpp's grammar converter itself | https://github.com/ggml-org/llama.cpp/issues |
| MCP server emitting broken schemas (e.g. zod-to-json-schema bugs) | The MCP server's repo, OR https://github.com/StefanTerdell/zod-to-json-schema |

If your change is more appropriate for upstream, we'll help you redirect it.

## Getting Started

```bash
git clone https://github.com/rsclafani/mcp-schema-normalize.git
cd mcp-schema-normalize
uv sync --extra dev --extra litellm
uv run pytest
```

Python 3.10+ required.

## Development Workflow

1. Fork → branch off `main` using `feat/`, `fix/`, `chore/`, `docs/`, or `test/` prefixes
2. Keep PRs focused — one concern per PR
3. Run `uv run pytest` and confirm no regressions before opening PR
4. Add/update tests for any behavior change (this codebase is TDD'd; we expect tests-first)
5. Run `uv run ruff check` and `uv run ruff format` before pushing

## Commit Style

Follow Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`. This enables automatic changelog generation later.

Example: `feat(core): handle prefixItems in tuple distribution`

## What We Won't Merge

- Changes that silently change normalization semantics without telemetry to surface them — every lossy rewrite must increment a counter and emit a WARN log
- PRs without tests for new behavior
- PRs that drop `STRICT_UNRESOLVED_REFS = True` support — operators who want hard-fail-on-broken-schemas need that escape hatch
- Reformatting-only PRs (open a separate `style:` PR if necessary)

## Reporting Bugs

Use the **Bug Report** issue template. The single most useful piece of evidence is a minimal schema that triggers the bug — ideally a `.json` file with the `function.parameters` block that fails, plus the exact error message from llama.cpp / your gateway.

We hit one of these in the wild ourselves; the "diagnostic dump" pattern (write the failing schema + error to disk, share it) is how the original library got built. If you can produce that artifact, you'll get a fast turnaround.

## Code of Conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).

## Response SLAs

Solo-maintained project on a side-project cadence. Bug reports with minimal reproductions get triaged within a week; feature requests may take longer. If something is blocking you, say so in the issue.
