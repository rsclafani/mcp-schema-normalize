"""Normalize JSON Schema 2020-12 tool definitions into a llama.cpp-compatible subset.

See ``mcp_schema_normalize._core`` module docstring for the full pipeline
description, the "When NOT to use this" semantic-tradeoff caveat, and the
``STRICT_UNRESOLVED_REFS`` opt-out.

LiteLLM integration lives at ``mcp_schema_normalize.integrations.litellm`` and
is only importable when installed via the ``[litellm]`` extra::

    pip install mcp-schema-normalize           # pure core
    pip install mcp-schema-normalize[litellm]  # core + LiteLLM pre-call hook
"""

from mcp_schema_normalize._core import (
    LOSSY_KEYS,
    MAX_INLINE_DEPTH,
    MAX_PER_SCHEMA_REF_WARNINGS,
    MODIFYING_KEYS,
    SIZE_BUDGET,
    STRICT_UNRESOLVED_REFS,
    WalkContext,
    build_ref_graph,
    find_ref_cycles,
    is_lossy_telemetry,
    normalize_schema,
    normalize_tools,
    resolve_pointer,
)

__all__ = [
    "LOSSY_KEYS",
    "MAX_INLINE_DEPTH",
    "MAX_PER_SCHEMA_REF_WARNINGS",
    "MODIFYING_KEYS",
    "SIZE_BUDGET",
    "STRICT_UNRESOLVED_REFS",
    "WalkContext",
    "build_ref_graph",
    "find_ref_cycles",
    "is_lossy_telemetry",
    "normalize_schema",
    "normalize_tools",
    "resolve_pointer",
]

__version__ = "0.1.0"
