# mcp-schema-normalize

*Bridge MCP tool schemas to llama.cpp's grammar-compatible subset.*

[![CI](https://github.com/rsclafani/mcp-schema-normalize/actions/workflows/ci.yml/badge.svg)](https://github.com/rsclafani/mcp-schema-normalize/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/mcp-schema-normalize.svg)](https://pypi.org/project/mcp-schema-normalize/)
[![Python versions](https://img.shields.io/pypi/pyversions/mcp-schema-normalize.svg)](https://pypi.org/project/mcp-schema-normalize/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Normalize MCP / OpenAI-format tool JSON schemas into the narrower subset llama.cpp's grammar converter accepts. Bridges the standards gap between [MCP-mandated JSON Schema 2020-12 (SEP-1613)](https://modelcontextprotocol.io/seps/1613-establish-json-schema-2020-12-as-default-dialect-f) and what local grammar-constrained sampling backends actually compile.

If your MCP tool calls work fine against Anthropic / OpenAI hosted APIs but die with `Unable to generate parser for this template` or `Error resolving ref … anyOf not in {…}` when routed through llama.cpp (`llama-server`, llama-swap, Ollama, etc.) — this library is for you.

---

## What it fixes

| Failure mode | llama.cpp issue | What this library does |
|---|---|---|
| `anyOf` (or `oneOf`) beside `properties` / `type` / `required` / `additionalProperties` | [#7703](https://github.com/ggml-org/llama.cpp/issues/7703) | Distribute siblings into each union branch, producing self-contained objects |
| `{"not": {}}` sentinel from `zod-to-json-schema` | [#17574](https://github.com/ggml-org/llama.cpp/issues/17574) | Drop empty-`not` keywords; preserve non-empty `not` schemas |
| Schemas with `$ref` pointing into `anyOf` nodes | (related to converter ref resolution) | Inline non-cyclic refs; preserve cyclic refs (llama.cpp handles cycles natively) |
| Schemas that expand past `MAX_REPETITION_THRESHOLD = 2000` | [#21228](https://github.com/ggml-org/llama.cpp/issues/21228) | Coarsen inlines that would blow the budget instead of silently falling back to unconstrained generation ([#19051](https://github.com/ggml-org/llama.cpp/issues/19051)) |
| Dangling `$ref` (paths that don't exist) — common `zod-to-json-schema` artifact when singleton unions collapse | (upstream schema bug) | **Replace with permissive `{}` so the request still completes.** See the load-bearing caveat below. |

---

## Install

This package is **pure Python, zero runtime dependencies** for the core. The LiteLLM proxy hook lives behind an optional extra so consumers who only need the schema transforms don't pull in LiteLLM.

```bash
# Pure-core: just the schema transforms (normalize_schema, normalize_tools,
# resolve_pointer, build_ref_graph, find_ref_cycles). No third-party deps.
pip install mcp-schema-normalize

# Add the LiteLLM CustomLogger pre-call hook. Pulls litellm>=1.0.
pip install mcp-schema-normalize[litellm]

# Development (pytest, ruff).
pip install mcp-schema-normalize[dev]
```

Equivalent `uv` invocations:

```bash
uv add mcp-schema-normalize                     # pure core
uv add 'mcp-schema-normalize[litellm]'          # + LiteLLM hook
```

Import the public API from the top-level package; integrations live under their own submodule path:

```python
# Pure-core API — always available
from mcp_schema_normalize import normalize_schema, normalize_tools

# LiteLLM hook — only available with [litellm] extra installed
from mcp_schema_normalize.integrations.litellm import normalize_tool_schemas_handler
```

---

## Quick start

### Direct use (any framework, any backend)

```python
from mcp_schema_normalize import normalize_tools

# Your OpenAI-format tool list as received from an MCP server
tools = [
    {
        "type": "function",
        "function": {
            "name": "paperclipUpdateIssue",
            "parameters": {
                # ... a JSON Schema 2020-12 tool definition with $ref, anyOf,
                # not:{} sentinels, etc. — whatever zod-to-json-schema emits
            },
        },
    },
]

normalized, telemetry = normalize_tools(tools)
# `normalized` is safe to forward to llama.cpp
# `telemetry` is a dict of counters you should log / alert on
```

### LiteLLM proxy

Register the hook in your `config.yaml`:

```yaml
litellm_settings:
  callbacks:
    - "mcp_schema_normalize.integrations.litellm.normalize_tool_schemas_handler"
    # ... any other callbacks (after this one)
```

That's it — the hook will rewrite every tool's `function.parameters` in-flight on chat-completion, responses, and other tool-carrying calls. One INFO-level summary log per modified request, escalated to WARN if anything lossy fires. All telemetry counters land as structured `extra=` fields for log aggregators (Loki, Datadog, etc.) to index.

---

## ⚠️ When NOT to use this — load-bearing assumption

This library will make your request **go through** even when your MCP server emits broken schemas. The cost is that affected fields lose their type spec and the model may emit structurally wrong values (e.g. a number where the schema said string-or-null).

The most common case: `zod-to-json-schema`'s singleton-union-collapse bug, where `z.union([X, ...])` collapses to its sole concrete variant but the generated `$ref` strings still expect the pre-collapse `anyOf` envelope. The library detects these dangling refs and replaces them with `{}` (match-anything) so the request completes; the original schema is malformed and gets silently loosened.

**Telemetry surfaces every event but you must be watching for it.** The library emits:

- `refs_unresolved` counter — incremented per dangling ref
- WARN-level per-ref log line — `unresolvable $ref replaced with permissive {} fallback`
- WARN-level per-request summary log — escalated whenever any lossy counter is non-zero
- Per-schema WARN-line rate limiting (default 10 per schema) so a runaway broken server can't flood logs; aggregate counter still reflects every event

If your observability stack doesn't alert on either the counter or the WARN log, you will not notice schemas are degrading silently. In that case **set `STRICT_UNRESOLVED_REFS = True`** to opt out of the fallback — dangling refs are then left in place, llama.cpp's grammar converter rejects the tool, and the failure surfaces as a 400 instead of a degraded response.

```python
import mcp_schema_normalize
mcp_schema_normalize.STRICT_UNRESOLVED_REFS = True  # fail loudly
```

Other lossy events the library also surfaces:

- `empty_union_drops` — `anyOf: [{"not": {}}]` collapsed; siblings retained (strict loosening)
- `union_coexistence_skipped` — `anyOf` and `oneOf` at the same level; we refuse to rewrite (correct handling needs allOf-wrapping; not yet implemented)
- `size_coarsenings` — inline would blow `SIZE_BUDGET = 1500`; deepest inline coarsened to `{"type": "object"}`
- `max_inline_depth_reached` — `$ref` chain exceeded `MAX_INLINE_DEPTH = 5`; tail coarsened to `{"type": "object"}`

---

## Telemetry reference

`normalize_schema()` and `normalize_tools()` return `(new_schema, telemetry)` and `(new_tools, telemetry)` respectively. The telemetry dict's keys, what they mean, and when to alert:

| Counter | Meaning | Lossy? | Routine on… |
|---|---|---|---|
| `refs_inlined` | Number of `$ref`s successfully inlined | no | Schemas with shared types |
| `cycles_preserved` | Cyclic `$ref`s left in place for llama.cpp to handle | no | Recursive types (TreeNode-style) |
| `refs_unresolved` | Dangling `$ref`s replaced with `{}` | **yes** | Broken MCP servers |
| `size_coarsenings` | Inlines coarsened due to size budget | **yes** | Pathologically large schemas |
| `max_inline_depth_reached` | Inline chains hit the depth cap | **yes** | Deeply nested ref graphs |
| `anyof_rewrites` | `anyOf`-beside-siblings distributions performed | no | Well-typed MCP schemas |
| `oneof_rewrites` | `oneOf`-beside-siblings distributions performed | no | Same |
| `not_drops` | `{"not": {}}` sentinels removed | no | zod-emitted schemas |
| `empty_union_drops` | Unions that became empty after `not:{}` filtering | **yes** | zod bugs |
| `union_coexistence_skipped` | Skipped node had both `anyOf` and `oneOf` | **yes** | Unusual schemas |

A reasonable Grafana alert: `sum(rate(refs_unresolved[5m])) by model > 0` pages whenever any tool schema starts emitting dangling refs.

---

## Configuration

All knobs are module-level constants you can monkey-patch before use:

```python
import mcp_schema_normalize

mcp_schema_normalize.SIZE_BUDGET = 1500              # llama.cpp threshold proxy
mcp_schema_normalize.MAX_INLINE_DEPTH = 5            # ref-chain depth cap
mcp_schema_normalize.MAX_PER_SCHEMA_REF_WARNINGS = 10  # per-schema log rate limit
mcp_schema_normalize.STRICT_UNRESOLVED_REFS = False  # True = no permissive fallback
```

---

## Backends and frameworks

The library is structurally agnostic — it operates on JSON Schema. It's been tested with:

- **LiteLLM proxy** → llama-swap → llama.cpp server (primary use case; first-class integration shipped)
- **Direct llama-server** via OpenAI-compatible API (use the pure-core `normalize_tools()` in your own client)
- **Ollama** (same llama.cpp grammar converter underneath; pure-core API applies)

Adding integrations for vLLM, TabbyAPI, or other proxies is a matter of writing a thin adapter that calls `normalize_tools()`. PRs welcome.

---

## Status

`0.1.0`, alpha. API may change before 1.0. The pipeline and telemetry surface are stable in intent; specific field names and module constants may move based on user feedback.

## Originating incident

This library was extracted from a real production incident — a paperclip MCP server emitting schemas that crashed Qwen3-Coder and Nemotron-Nano local backends with `Unable to generate parser for this template`. The investigation post-mortem (including "what we should have done differently") is in the LiteLLM repo it was extracted from; if you want the long-form story, ping me and I'll publish it as a blog post.

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md). Bug reports especially welcome — the more broken MCP schemas we see in the wild, the better this library gets at handling them.

## License

[MIT](./LICENSE).
