"""Tests for the MCP tool-schema normalizer (Phase A).

Phase A scope (minimum viable for the Paperclip MCP failure):
  1. anyOf-beside-properties rewrite — distribute sibling keywords into
     each anyOf branch, eliminating the llama.cpp #7703 failure mode
     ("anyOf not in {...}").
  2. Strip `not: {}` sentinels emitted by zod-to-json-schema (llama.cpp
     #17574).

Out of scope for Phase A (will land in Phase B):
  - $ref re-hoisting
  - Tarjan SCC cycle detection
  - MAX_REPETITION_THRESHOLD size budgeting
  - Bounded inlining
"""

from __future__ import annotations

from mcp_schema_normalize import (
    build_ref_graph,
    find_ref_cycles,
    normalize_schema,
    normalize_tools,
    resolve_pointer,
)

# ─── 1. anyOf-beside-properties rewrite ─────────────────────────────────


def test_anyof_alone_is_left_alone():
    """anyOf with no sibling keywords needs no rewriting."""
    schema = {"anyOf": [{"type": "string"}, {"type": "number"}]}
    out, telemetry = normalize_schema(schema)
    assert out == schema
    assert telemetry["anyof_rewrites"] == 0


def test_properties_alone_is_left_alone():
    """Plain object schema with no anyOf needs no rewriting."""
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    out, telemetry = normalize_schema(schema)
    assert out == schema
    assert telemetry["anyof_rewrites"] == 0


def test_anyof_beside_properties_distributes_siblings():
    """The core failure mode.

    A node with both `anyOf` and a sibling `properties` (or `type`,
    `required`, etc.) is rewritten so the siblings move *inside* each
    anyOf branch, producing a top-level anyOf of self-contained objects.
    Semantics preserved: any value matching the original schema matches
    exactly one branch of the rewritten one.
    """
    schema = {
        "type": "object",
        "properties": {"shared": {"type": "string"}},
        "required": ["shared"],
        "anyOf": [
            {"properties": {"a": {"type": "number"}}},
            {"properties": {"b": {"type": "boolean"}}},
        ],
    }
    out, telemetry = normalize_schema(schema)

    assert "anyOf" in out
    assert "properties" not in out
    assert "required" not in out
    assert "type" not in out
    assert len(out["anyOf"]) == 2

    branch0 = out["anyOf"][0]
    assert branch0["type"] == "object"
    assert branch0["required"] == ["shared"]
    assert branch0["properties"] == {
        "shared": {"type": "string"},
        "a": {"type": "number"},
    }

    branch1 = out["anyOf"][1]
    assert branch1["properties"] == {
        "shared": {"type": "string"},
        "b": {"type": "boolean"},
    }

    assert telemetry["anyof_rewrites"] == 1


def test_anyof_beside_properties_recurses_into_nested_structures():
    """Paperclip's actual shape: nested rows.items.anyOf inside an outer
    properties.metadata block. The rewrite must reach the inner anyOf."""
    schema = {
        "type": "object",
        "properties": {
            "metadata": {
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"label": {"type": "string"}},
                            "anyOf": [
                                {"properties": {"variant": {"const": "text"}}},
                                {"properties": {"variant": {"const": "image"}}},
                            ],
                        },
                    },
                },
            },
        },
    }
    out, telemetry = normalize_schema(schema)
    items = out["properties"]["metadata"]["properties"]["rows"]["items"]
    assert "anyOf" in items
    assert "properties" not in items
    assert len(items["anyOf"]) == 2
    assert "label" in items["anyOf"][0]["properties"]
    assert "variant" in items["anyOf"][0]["properties"]
    assert telemetry["anyof_rewrites"] == 1


def test_anyof_branch_overrides_sibling_property():
    """If an anyOf branch defines the same property as the sibling,
    the branch wins (more-specific overrides less-specific)."""
    schema = {
        "properties": {"x": {"type": "string"}},
        "anyOf": [
            {"properties": {"x": {"const": "fixed"}}},
            {"properties": {"y": {"type": "number"}}},
        ],
    }
    out, _ = normalize_schema(schema)
    assert out["anyOf"][0]["properties"]["x"] == {"const": "fixed"}
    assert out["anyOf"][1]["properties"]["x"] == {"type": "string"}


def test_oneof_beside_properties_distributes_too():
    """Same fix applies to oneOf — same llama.cpp converter path."""
    schema = {
        "type": "object",
        "properties": {"shared": {"type": "string"}},
        "oneOf": [
            {"properties": {"a": {"type": "number"}}},
            {"properties": {"b": {"type": "boolean"}}},
        ],
    }
    out, telemetry = normalize_schema(schema)
    assert "oneOf" in out
    assert "properties" not in out
    assert telemetry["oneof_rewrites"] == 1


# ─── 2. Strip `not: {}` sentinels ───────────────────────────────────────


def test_not_empty_branch_removed_from_anyof():
    """zod-to-json-schema emits `{"not": {}}` as a 'never' sentinel.
    llama.cpp #17574: the converter rejects it. Drop the branch."""
    schema = {
        "anyOf": [
            {"type": "string"},
            {"not": {}},
            {"type": "number"},
        ],
    }
    out, telemetry = normalize_schema(schema)
    assert out == {"anyOf": [{"type": "string"}, {"type": "number"}]}
    assert telemetry["not_drops"] == 1


def test_not_empty_sibling_removed():
    """`not: {}` appearing as a sibling keyword (not inside anyOf)
    is also dropped — same sentinel, same llama.cpp rejection."""
    schema = {"type": "string", "not": {}}
    out, telemetry = normalize_schema(schema)
    assert out == {"type": "string"}
    assert telemetry["not_drops"] == 1


def test_not_with_nonempty_body_is_preserved():
    """`not` with an actual subschema is legitimate JSON Schema and
    must NOT be stripped (even though llama.cpp also can't compile it;
    that's a different problem for Phase B)."""
    schema = {"type": "string", "not": {"const": "forbidden"}}
    out, telemetry = normalize_schema(schema)
    assert out == schema
    assert telemetry["not_drops"] == 0


def test_anyof_collapses_to_single_branch_when_others_were_not_empty():
    """If stripping `not: {}` leaves one branch, the anyOf collapses
    to that branch (avoids unnecessary union)."""
    schema = {"anyOf": [{"type": "string"}, {"not": {}}]}
    out, _ = normalize_schema(schema)
    assert out == {"type": "string"}


# ─── 2b. Cross-cutting cases the first pass missed ──────────────────────


def test_distribute_does_not_re_emit_anyof_beside_properties():
    """Bug Opus caught: outer distribute merging siblings into a branch
    that *itself* contains an `anyOf` recreates the exact failure pattern
    this hook exists to eliminate. Pre-flatten union-of-union before
    distributing siblings."""
    schema = {
        "properties": {"x": {"type": "string"}},
        "anyOf": [
            {"anyOf": [{"type": "string"}, {"type": "number"}]},
        ],
    }
    out, telemetry = normalize_schema(schema)
    # The inner anyOf must have been flattened into the outer one
    # before sibling distribution, leaving two top-level branches with
    # merged `properties.x`.
    assert "properties" not in out
    assert "anyOf" in out
    assert len(out["anyOf"]) == 2
    for branch in out["anyOf"]:
        assert "anyOf" not in branch, "union-of-union re-emerged in branch"
        assert branch["properties"] == {"x": {"type": "string"}}
    assert telemetry["anyof_rewrites"] >= 1


def test_anyof_oneof_coexistence_is_left_untouched():
    """When `anyOf` and `oneOf` appear at the same level the semantics
    are 'must match anyOf AND oneOf' — distributing one into the other
    would either explode (cartesian) or change semantics. Phase A
    refuses: leave the node untouched, log, defer to Phase B."""
    schema = {
        "properties": {"x": {"type": "string"}},
        "anyOf": [{"properties": {"a": {"type": "number"}}}],
        "oneOf": [{"properties": {"b": {"type": "boolean"}}}],
    }
    out, telemetry = normalize_schema(schema)
    assert out == schema
    assert telemetry["anyof_rewrites"] == 0
    assert telemetry["oneof_rewrites"] == 0
    assert telemetry["union_coexistence_skipped"] == 1


def test_anyof_with_all_branches_filtered_drops_union():
    """`{"anyOf": [{"not": {}}]}` originally meant 'match nothing'.
    Filtering leaves `{"anyOf": []}`, which is also invalid for
    llama.cpp. Drop the empty union entirely. With no siblings, the
    result is `{}` (permissive) — note the semantic loosening in
    telemetry so operators can see it."""
    schema = {"anyOf": [{"not": {}}]}
    out, telemetry = normalize_schema(schema)
    assert out == {}
    assert telemetry["not_drops"] == 1
    assert telemetry["empty_union_drops"] == 1


def test_anyof_with_all_branches_filtered_drops_union_keeps_siblings():
    """Same case with siblings. Drop the empty union; keep the siblings.
    Net effect: original 'X AND never' (= never) becomes just X — strict
    loosening, logged via `empty_union_drops`."""
    schema = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "anyOf": [{"not": {}}],
    }
    out, telemetry = normalize_schema(schema)
    assert out == {
        "type": "object",
        "properties": {"x": {"type": "string"}},
    }
    assert telemetry["empty_union_drops"] == 1


# ─── 2c. allOf, additionalProperties, deep nesting ──────────────────────


def test_allof_beside_anyof_is_distributed_into_each_branch():
    """`allOf` is not a union key; the distribute pass treats it as an
    opaque sibling. Semantically: `anyOf AND allOf` == `(branch1 AND
    allOf) OR (branch2 AND allOf)`, so distribution preserves meaning
    and llama.cpp's converter handles each branch in isolation."""
    schema = {
        "type": "object",
        "allOf": [{"required": ["common"]}],
        "anyOf": [
            {"properties": {"a": {"type": "number"}}},
            {"properties": {"b": {"type": "boolean"}}},
        ],
    }
    out, _ = normalize_schema(schema)
    assert "allOf" not in out
    assert "anyOf" in out
    assert len(out["anyOf"]) == 2
    for branch in out["anyOf"]:
        assert branch["type"] == "object"
        assert branch["allOf"] == [{"required": ["common"]}]
    assert "a" in out["anyOf"][0]["properties"]
    assert "b" in out["anyOf"][1]["properties"]


def test_additional_properties_false_distributes_into_branches():
    """`additionalProperties: false` distributes into each anyOf branch.
    Net effect: a previously-contradictory schema (outer says 'only x
    allowed', branch adds 'a') becomes consistent — the branch's
    `properties` is merged in *before* `additionalProperties: false`
    evaluates, so `a` is now an in-schema property and accepted. This
    is a *loosening* of any contradictory input but a faithful encoding
    of any non-contradictory one. Document by example."""
    schema = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "additionalProperties": False,
        "anyOf": [
            {"properties": {"a": {"type": "number"}}},
            {"properties": {"b": {"type": "boolean"}}},
        ],
    }
    out, _ = normalize_schema(schema)
    assert "anyOf" in out
    assert len(out["anyOf"]) == 2
    for branch in out["anyOf"]:
        assert branch["additionalProperties"] is False
        # Outer properties survived the merge.
        assert "x" in branch["properties"]
    assert "a" in out["anyOf"][0]["properties"]
    assert "b" in out["anyOf"][1]["properties"]


def test_three_level_anyof_nesting_flattens_completely():
    """Confirms `_flatten_inner_union` works bottom-up through arbitrary
    nesting depth: an anyOf-of-anyOf-of-anyOf with outer siblings
    collapses to a single-level anyOf with distributed siblings on every
    leaf branch."""
    schema = {
        "properties": {"x": {"type": "string"}},
        "anyOf": [
            {
                "anyOf": [
                    {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "number"},
                        ]
                    },
                ]
            },
        ],
    }
    out, _ = normalize_schema(schema)
    assert "anyOf" in out
    assert len(out["anyOf"]) == 2
    # No anyOf survives in any branch
    for branch in out["anyOf"]:
        assert "anyOf" not in branch
        assert branch["properties"] == {"x": {"type": "string"}}
    assert {b["type"] for b in out["anyOf"]} == {"string", "number"}


# ─── Phase B: JSON Pointer resolver ─────────────────────────────────────


def test_resolve_pointer_root():
    schema = {"properties": {"x": {"type": "string"}}}
    assert resolve_pointer(schema, "#") is schema


def test_resolve_pointer_into_properties():
    schema = {"properties": {"x": {"type": "string"}}}
    assert resolve_pointer(schema, "#/properties/x") == {"type": "string"}


def test_resolve_pointer_into_array_index():
    schema = {"anyOf": [{"type": "string"}, {"type": "number"}]}
    assert resolve_pointer(schema, "#/anyOf/1") == {"type": "number"}


def test_resolve_pointer_returns_none_for_missing_path():
    schema = {"properties": {"x": {"type": "string"}}}
    assert resolve_pointer(schema, "#/properties/missing") is None


def test_resolve_pointer_returns_none_for_external_ref():
    """External refs (anything not starting with `#`) unsupported."""
    schema = {"properties": {"x": {"type": "string"}}}
    assert resolve_pointer(schema, "https://example.com/schema") is None


def test_resolve_pointer_handles_json_pointer_escape_codes():
    """`~1` decodes to `/`, `~0` decodes to `~` (RFC 6901)."""
    schema = {"foo/bar": {"baz~quux": "value"}}
    assert resolve_pointer(schema, "#/foo~1bar/baz~0quux") == "value"


# ─── Phase B: ref graph + cycle detection ───────────────────────────────


def test_build_ref_graph_finds_all_refs():
    schema = {
        "properties": {
            "a": {"$ref": "#/$defs/A"},
            "b": {"$ref": "#/$defs/B"},
        },
        "$defs": {
            "A": {"properties": {"a2": {"$ref": "#/$defs/B"}}},
            "B": {"type": "string"},
        },
    }
    graph = build_ref_graph(schema)
    # Edges from each ref-site path → ref target.
    targets = {target for _, target in graph}
    assert "#/$defs/A" in targets
    assert "#/$defs/B" in targets


def test_find_ref_cycles_detects_self_loop():
    """A schema whose `$defs/Node` references itself forms a 1-cycle."""
    schema = {
        "$defs": {
            "Node": {
                "properties": {"child": {"$ref": "#/$defs/Node"}},
            },
        },
    }
    cycles = find_ref_cycles(schema)
    assert "#/$defs/Node" in cycles


def test_find_ref_cycles_detects_mutual_recursion():
    """A↔B mutual recursion is a 2-cycle. Both nodes must be flagged."""
    schema = {
        "$defs": {
            "A": {"properties": {"to_b": {"$ref": "#/$defs/B"}}},
            "B": {"properties": {"to_a": {"$ref": "#/$defs/A"}}},
        },
    }
    cycles = find_ref_cycles(schema)
    assert "#/$defs/A" in cycles
    assert "#/$defs/B" in cycles


def test_find_ref_cycles_empty_for_dag():
    """Acyclic ref graph yields no cycles."""
    schema = {
        "$defs": {
            "A": {"$ref": "#/$defs/B"},
            "B": {"type": "string"},
        },
    }
    assert find_ref_cycles(schema) == set()


# ─── Phase B: $ref inlining (the paperclip case) ────────────────────────


def test_acyclic_ref_is_inlined():
    """A `$ref` pointing at a non-cyclic target is replaced by a
    deep-copy of the target schema."""
    schema = {
        "properties": {
            "x": {"$ref": "#/$defs/Label"},
        },
        "$defs": {
            "Label": {"type": "string", "minLength": 1},
        },
    }
    out, telemetry = normalize_schema(schema)
    assert out["properties"]["x"] == {"type": "string", "minLength": 1}
    assert telemetry["refs_inlined"] == 1


def test_multiple_refs_to_same_target_each_inlined_independently():
    """Two `$ref`s to the same target each get their own copy.
    Independence matters because the post-inline distribute pass may
    rewrite each copy differently based on its context."""
    schema = {
        "properties": {
            "a": {"$ref": "#/$defs/Label"},
            "b": {"$ref": "#/$defs/Label"},
        },
        "$defs": {"Label": {"type": "string"}},
    }
    out, telemetry = normalize_schema(schema)
    assert out["properties"]["a"] == {"type": "string"}
    assert out["properties"]["b"] == {"type": "string"}
    # Same target, two inlines.
    assert telemetry["refs_inlined"] == 2
    # Deep-copied, not shared:
    out["properties"]["a"]["minLength"] = 1
    assert "minLength" not in out["properties"]["b"]


def test_nested_ref_is_resolved_transitively():
    """A ref target that itself contains a ref: both get inlined."""
    schema = {
        "properties": {"x": {"$ref": "#/$defs/A"}},
        "$defs": {
            "A": {"$ref": "#/$defs/B"},
            "B": {"type": "number"},
        },
    }
    out, _ = normalize_schema(schema)
    assert out["properties"]["x"] == {"type": "number"}


def test_self_cyclic_ref_is_left_as_is():
    """Self-recursive `$defs/Node` is left as a `$ref` — inlining
    would not terminate. llama.cpp's converter handles self-cycles
    natively via its rule-memoization path."""
    schema = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/Node"}},
            },
        },
        "properties": {"root": {"$ref": "#/$defs/Node"}},
    }
    out, telemetry = normalize_schema(schema)
    # The `root` ref to `Node` IS inlined (Node itself is cyclic but
    # the use-site reference is not part of the cycle). The inner
    # `child` ref, which closes the cycle, stays as `$ref`.
    inner = out["properties"]["root"]
    assert inner["type"] == "object"
    assert inner["properties"]["child"] == {"$ref": "#/$defs/Node"}
    assert telemetry["cycles_preserved"] >= 1


def test_per_ref_warning_is_rate_limited_per_schema(caplog):
    """A broken MCP server can emit hundreds of dangling refs in a
    single tool's schema; one WARN log per ref would drown out signal.
    Cap per-ref WARN lines at MAX_PER_SCHEMA_REF_WARNINGS, then emit
    one "rate-limited" summary noting how many more occurred. The
    telemetry counter (`refs_unresolved`) still counts all of them."""
    import logging as _logging

    from mcp_schema_normalize import MAX_PER_SCHEMA_REF_WARNINGS

    # Construct a schema with twice the cap of dangling refs.
    refs = MAX_PER_SCHEMA_REF_WARNINGS * 2
    schema = {
        "properties": {f"p{i}": {"$ref": f"#/$defs/Missing{i}"} for i in range(refs)},
        "$defs": {},
    }
    with caplog.at_level(_logging.WARNING, logger="mcp_schema_normalize"):
        _, telemetry = normalize_schema(schema)

    per_ref_warnings = [r for r in caplog.records if "unresolvable $ref replaced" in r.getMessage()]
    rate_limit_summaries = [r for r in caplog.records if "rate-limited" in r.getMessage()]
    # All refs counted in telemetry.
    assert telemetry["refs_unresolved"] == refs
    # Only MAX_PER_SCHEMA_REF_WARNINGS individual warns surfaced.
    assert len(per_ref_warnings) == MAX_PER_SCHEMA_REF_WARNINGS
    # Exactly one rate-limit summary follows.
    assert len(rate_limit_summaries) == 1
    assert str(refs - MAX_PER_SCHEMA_REF_WARNINGS) in rate_limit_summaries[0].getMessage()


def test_summary_log_attaches_telemetry_as_structured_extra(caplog):
    """The handler's summary log emits each telemetry counter as a
    structured LogRecord attribute (via `extra={...}`) so log
    aggregators can index them as fields instead of regexing the
    message string."""
    import asyncio
    import logging as _logging

    from mcp_schema_normalize.integrations.litellm import normalize_tool_schemas_handler

    tools = [
        {
            "type": "function",
            "function": {
                "name": "t",
                "parameters": {
                    "properties": {"x": {"type": "string"}},
                    "anyOf": [{"properties": {"a": {"type": "number"}}}],
                },
            },
        }
    ]
    data = {"model": "test-model", "tools": tools}

    with caplog.at_level(_logging.INFO, logger="mcp_schema_normalize.integrations.litellm"):
        asyncio.run(
            normalize_tool_schemas_handler.async_pre_call_hook(
                user_api_key_dict=None,
                cache=None,
                data=data,
                call_type="completion",
            )
        )

    summary = next(r for r in caplog.records if "summary" in r.getMessage())
    # Counters present as LogRecord attributes for structured ingest.
    assert getattr(summary, "tools_modified", None) == 1
    assert getattr(summary, "tools_seen", None) == 1
    assert getattr(summary, "model", None) == "test-model"
    assert hasattr(summary, "anyof_rewrites")
    assert hasattr(summary, "refs_inlined")


def test_strict_unresolved_refs_leaves_ref_in_place():
    """With ``STRICT_UNRESOLVED_REFS = True``, dangling refs are
    preserved verbatim so llama.cpp's converter fails loudly.
    Provided as an OSS opt-out for operators who prefer hard-fail
    over silent degradation."""
    import mcp_schema_normalize._core as mod

    original = mod.STRICT_UNRESOLVED_REFS
    mod.STRICT_UNRESOLVED_REFS = True
    try:
        schema = {
            "properties": {"x": {"$ref": "#/$defs/Missing"}},
            "$defs": {},
        }
        out, telemetry = normalize_schema(schema)
        assert out["properties"]["x"] == {"$ref": "#/$defs/Missing"}
        assert telemetry["refs_unresolved"] == 1
    finally:
        mod.STRICT_UNRESOLVED_REFS = original


def test_unresolvable_ref_is_replaced_with_permissive_fallback():
    """A dangling `$ref` is replaced with ``{}`` (match-anything).
    Leaving the `$ref` in place crashes llama.cpp's grammar converter,
    which makes the whole tool unusable; replacing with a permissive
    schema loosens type validation for that one field but lets the
    request through. Telemetry + WARN log surface the upstream bug
    (commonly `zod-to-json-schema` emitting refs to envelopes its
    own union-collapse pass has removed)."""
    schema = {
        "properties": {"x": {"$ref": "#/$defs/Missing"}},
        "$defs": {},
    }
    out, telemetry = normalize_schema(schema)
    assert out["properties"]["x"] == {}
    assert telemetry["refs_unresolved"] == 1


def test_paperclip_shape_ref_into_sibling_anyof_branch():
    """The paperclip MCP pattern: a row variant's `label` is a `$ref`
    pointing at the label spec defined in the FIRST variant. After
    inlining, the variant carries the label spec verbatim and the
    downstream grammar converter has no ref to chase."""
    schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "kind": {"const": "text"},
                                "label": {
                                    "anyOf": [
                                        {"type": "string", "minLength": 1},
                                        {"type": "null"},
                                    ],
                                },
                            },
                        },
                        {
                            "type": "object",
                            "properties": {
                                "kind": {"const": "code"},
                                "label": {
                                    "$ref": "#/properties/rows/items/anyOf/0/properties/label",
                                },
                            },
                        },
                    ],
                },
            },
        },
    }
    out, telemetry = normalize_schema(schema)
    code_variant = out["properties"]["rows"]["items"]["anyOf"][1]
    label_spec = code_variant["properties"]["label"]
    assert "$ref" not in label_spec
    assert label_spec == {
        "anyOf": [
            {"type": "string", "minLength": 1},
            {"type": "null"},
        ],
    }
    assert telemetry["refs_inlined"] == 1


# ─── Phase B: size budget pre-flight ────────────────────────────────────


def test_size_budget_overflow_triggers_coarsening():
    """When the projected post-inline schema exceeds the size budget
    (a stand-in for llama.cpp's `MAX_REPETITION_THRESHOLD=2000`), the
    deepest anyOf is coarsened to `{"type": "object"}` and the event
    is logged via telemetry."""
    # Construct a schema that explodes when inlined: 50 refs to a
    # ref target that itself is a 50-branch anyOf.
    big_target = {
        "anyOf": [
            {"type": "object", "properties": {f"k{i}": {"type": "string"}}} for i in range(50)
        ]
    }
    schema = {
        "properties": {f"p{i}": {"$ref": "#/$defs/Big"} for i in range(50)},
        "$defs": {"Big": big_target},
    }
    _, telemetry = normalize_schema(schema)
    assert telemetry["size_coarsenings"] >= 1


# ─── 3. End-to-end on a tools list ──────────────────────────────────────


def test_normalize_tools_passes_through_non_function_entries_untouched():
    """The strip_invalid_tools hook handles non-function entries.
    This hook only touches `function.parameters`."""
    tools = [
        {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}},
        {"type": "namespace", "name": "weird"},
    ]
    out, _ = normalize_tools(tools)
    assert out[1] == {"type": "namespace", "name": "weird"}


def test_normalize_tools_rewrites_each_tools_parameters():
    """Each tool's `function.parameters` is normalized; counts aggregate."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "t1",
                "parameters": {
                    "properties": {"x": {"type": "string"}},
                    "anyOf": [{"properties": {"a": {"type": "number"}}}],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "t2",
                "parameters": {"anyOf": [{"type": "string"}, {"not": {}}]},
            },
        },
    ]
    out, telemetry = normalize_tools(tools)
    # t1: anyOf distribute → single-branch collapse → merged properties
    assert out[0]["function"]["parameters"] == {
        "properties": {
            "x": {"type": "string"},
            "a": {"type": "number"},
        },
    }
    # t2: not-empty stripped, anyOf collapsed to its surviving branch
    assert out[1]["function"]["parameters"] == {"type": "string"}

    assert telemetry["tools_seen"] == 2
    assert telemetry["tools_modified"] == 2
    assert telemetry["not_drops"] == 1
    assert telemetry["anyof_rewrites"] >= 1


def test_normalize_tools_does_not_mutate_input():
    """Pure transform — input must not be modified."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "t",
                "parameters": {
                    "properties": {"x": {"type": "string"}},
                    "anyOf": [{"properties": {"a": {"type": "number"}}}],
                },
            },
        },
    ]
    import copy

    snapshot = copy.deepcopy(tools)
    normalize_tools(tools)
    assert tools == snapshot
