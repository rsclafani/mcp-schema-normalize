# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-28

Initial extraction from a working LiteLLM deployment, where the hook unblocked an MCP server (paperclip) whose `zod-to-json-schema`-generated schemas were crashing local llama.cpp backends.

### Added

- `normalize_schema(schema) -> (new_schema, telemetry)` — single-schema normalization
- `normalize_tools(tools) -> (new_tools, telemetry)` — OpenAI-format tool-list normalization
- `resolve_pointer(root, pointer)` — RFC 6901 JSON Pointer resolver
- `build_ref_graph(root)` — extract all `$ref` edges from a schema
- `find_ref_cycles(root)` — Tarjan SCC over the ref graph
- `is_lossy_telemetry(telemetry) -> bool` — convenience for log-level escalation
- `LOSSY_KEYS`, `MODIFYING_KEYS` — public telemetry-classification tuples
- `WalkContext` — public state-holder class for advanced use
- Module constants: `SIZE_BUDGET`, `MAX_INLINE_DEPTH`, `MAX_PER_SCHEMA_REF_WARNINGS`, `STRICT_UNRESOLVED_REFS`
- `mcp_schema_normalize.integrations.litellm.NormalizeToolSchemasHandler` — LiteLLM `CustomLogger` pre-call hook (install via `[litellm]` extra)
- 40 unit tests covering pure-core transforms and the LiteLLM integration

### Pipeline implemented

1. `$ref` inlining with depth-first cycle detection + size budget + bounded-depth coarsening
2. Dangling-`$ref` permissive `{}` fallback (opt-out via `STRICT_UNRESOLVED_REFS = True`)
3. `anyOf`/`oneOf` beside-siblings distribution with inner-union flatten
4. `{"not": {}}` sentinel stripping
5. Empty-union dropping + `anyOf`/`oneOf` coexistence refusal (with telemetry)
