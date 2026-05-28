"""Normalize MCP-style tool JSON schemas into a llama.cpp-compatible subset.

**This is a workaround, not a fix.** llama.cpp's grammar converter
implements a narrower subset of JSON Schema 2020-12 than the spec MCP
mandates (SEP-1613); well-typed MCP servers emit standards-correct
schemas that the converter rejects. The fix belongs upstream in either
llama.cpp's converter or each individual MCP server. Until that lands,
this hook bridges the gap at the proxy layer.

**Pipeline (in order):**

1. **`$ref` inlining** (Phase B). Resolve every JSON-Pointer-style
   `$ref` by deep-copying its target schema into the use site. Cycles
   are detected via depth-first visited-set semantics: a `$ref` that
   would close a cycle is left as-is (llama.cpp handles self-cycles
   natively via its rule-memoization path). Schema size is tracked
   against a budget mirroring llama.cpp's `MAX_REPETITION_THRESHOLD =
   2000`; inlines that would blow the budget are coarsened to
   ``{"type": "object"}`` rather than silently producing a schema
   llama.cpp would fall back from to unconstrained generation (issue
   #19051).

   **Dangling-ref fallback (load-bearing assumption).** When a `$ref`
   points at a path that does not exist in the schema, the inliner
   replaces the ref node with the permissive empty schema ``{}``
   (match anything). The alternative — leaving the dangling ref in
   place — crashes llama.cpp's grammar converter and takes the entire
   tool down. This trades **type validation specificity for the
   affected field** in exchange for the request actually completing.

   Implications operators must understand:
     - The model may emit a structurally wrong value for that field
       (e.g. a number where the original schema said string-or-null).
       Tool implementations downstream should already validate inputs;
       this hook does not assume they don't.
     - The substitution is silent at the API surface; **only the
       telemetry (`refs_unresolved` counter) and the WARN log
       ("unresolvable $ref replaced with permissive {} fallback")
       surface it.** If your observability stack is not configured to
       alert on either, you will not notice schemas are silently
       loosening.
     - In practice the common cause is `zod-to-json-schema` emitting
       refs to envelopes its own singleton-union-collapse pass has
       removed (the 2026-05-28 paperclip investigation documents one
       instance). The fix belongs upstream in the MCP server's schema
       generator; this fallback is a gateway-side resilience measure,
       not a substitute.

   Set ``STRICT_UNRESOLVED_REFS = True`` (module constant) to opt out
   of the fallback: dangling refs are left in place, llama.cpp's
   grammar converter rejects the containing tool, and the failure
   surfaces as a 400 instead of a degraded response. Useful when you
   want upstream schema bugs to fail loudly rather than degrade
   silently.

2. **`anyOf`-beside-properties distribution** (Phase A). llama.cpp
   rejects nodes that mix `anyOf` (or `oneOf`) with sibling keywords
   like `properties`, `required`, `type`, `additionalProperties`
   (llama.cpp #7703). Push the siblings *into* each anyOf branch,
   producing a top-level anyOf of self-contained objects. Inner
   `{anyOf:[...]}` branches are flattened into the outer list *before*
   distribution to prevent re-emitting the failure pattern.

3. **Drop `not: {}` sentinels** (Phase A). `zod-to-json-schema` emits
   ``{"not": {}}`` as a "never" marker; llama.cpp rejects it (llama.cpp
   #17574). Empty-`not` keywords are dropped; non-empty `not` schemas
   are preserved.

When `anyOf` and `oneOf` appear at the same level the hook **refuses**
to rewrite (correct handling needs allOf-wrapping; not implemented).
When all union branches are filtered away, the union is dropped
entirely and siblings retained (strict loosening, logged so operators
can audit).

**Out of scope** (would land in a later phase): canonical-JSON-hash
re-hoisting of pre-inlined duplicates (only needed if an upstream MCP
server arrives with already-flattened schemas), `if`/`then`/`else`
distribution, `prefixItems` handling.

**Upstream status.** The failure modes this library addresses are
**documented permanent limitations** of llama.cpp's grammar converter,
authoritatively listed in
https://github.com/ochafik/llama.cpp/blob/master/grammars/README.md#json-schemas--gbnf
by the converter's implementer. The corresponding tracking issues
(#7703, #8073, #17574, #19051, #21228) are all closed — not because
they were fixed, but because they were accepted as won't-fix, closed
with a downstream-only workaround, or stale-bot closed without
resolution. There is no current upstream path to obsolete this hook;
a successor grammar engine (xgrammar etc.) might eventually do so,
but llama.cpp's own converter is unlikely to.

See the README for the originating incident write-up and links to the
upstream documentation.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger("mcp_schema_normalize")
# Deliberately no handler attach, no propagate override, no setLevel.
# The host application (LiteLLM proxy, or any consumer of this module
# as an OSS library) owns logging config. Pin level via the standard
# `LITELLM_LOG` / root-logger config; structured fields land via the
# `extra=` kwarg on each log call so JSON formatters index them.

# Iteration order is no longer load-bearing — the coexistence guard at
# the top of the union pass ensures we only enter the loop when exactly
# one of these keys is present. Order is kept for stable telemetry.
_UNION_KEYS = ("anyOf", "oneOf")

_UNION_TELEMETRY_KEY = {
    "anyOf": "anyof_rewrites",
    "oneOf": "oneof_rewrites",
}

# Stand-in for llama.cpp's `MAX_REPETITION_THRESHOLD`. Once total
# inlined-node count crosses this, further inlines are coarsened to
# ``{"type": "object"}`` and a `size_coarsenings` telemetry event is
# emitted. Reading llama.cpp source: the real ceiling is 2000 (PR
# #21003, issue #21228); we leave headroom for the converter's own
# anyOf/oneOf branch fan-out by capping at 1500.
SIZE_BUDGET = 1500

# Hard ceiling on transitive ref-inlining depth from any single use
# site. Prevents pathological resolution chains (a => b => c => ...)
# from accumulating; llama.cpp itself doesn't enforce a depth bound,
# but Outlines uses 3 (too aggressive); raised here based on
# observation of well-typed MCP schemas (GitHub MCP nests
# issue→user→org→team, 4 deep before tail).
MAX_INLINE_DEPTH = 5

# Cap on per-schema individual "unresolvable $ref" WARN lines. A broken
# MCP server (e.g. zod-to-json-schema with its singleton-union-collapse
# bug) can dump hundreds of dangling refs in one schema; logging each
# one drowns out real signal. After the cap, one rate-limit summary is
# emitted noting how many more occurred. The aggregate counter
# `refs_unresolved` in telemetry still counts every one of them, so
# nothing is lost from dashboards.
MAX_PER_SCHEMA_REF_WARNINGS = 10

# When a `$ref` points at a path that doesn't exist, default behavior
# (False) replaces the ref with the permissive empty schema `{}` so
# the request can still complete with degraded type specificity for
# the affected field. Set True to leave the `$ref` in place instead —
# llama.cpp's grammar converter will then reject the tool with a
# visible error, which is preferable when you want upstream schema
# bugs to fail loudly rather than degrade silently. See the
# "Dangling-ref fallback" section of the module docstring.
STRICT_UNRESOLVED_REFS = False


# ─── Telemetry ──────────────────────────────────────────────────────────


def _empty_telemetry() -> dict:
    return {
        # Phase A
        "anyof_rewrites": 0,
        "oneof_rewrites": 0,
        "not_drops": 0,
        "empty_union_drops": 0,
        "union_coexistence_skipped": 0,
        # Phase B
        "refs_inlined": 0,
        "cycles_preserved": 0,
        "refs_unresolved": 0,
        "size_coarsenings": 0,
        "max_inline_depth_reached": 0,
    }


#: Telemetry counters whose presence indicates the schema was modified
#: (i.e. not a pass-through). Useful for integrations deciding whether
#: to emit a per-request summary log.
MODIFYING_KEYS: tuple[str, ...] = (
    "anyof_rewrites",
    "oneof_rewrites",
    "not_drops",
    "empty_union_drops",
    "refs_inlined",
    "cycles_preserved",
    "size_coarsenings",
)

#: Telemetry counters whose presence indicates a lossy/risky rewrite
#: (semantic loosening, hard fallback, or a deferred-to-Phase-B refusal).
#: Integrations should escalate logs to WARN when any of these are
#: non-zero so operators surface upstream schema bugs.
LOSSY_KEYS: tuple[str, ...] = (
    "empty_union_drops",
    "union_coexistence_skipped",
    "size_coarsenings",
    "max_inline_depth_reached",
    "refs_unresolved",
)

# Back-compat alias used internally.
_MODIFYING_KEYS = MODIFYING_KEYS


def is_lossy_telemetry(telemetry: dict) -> bool:
    """Return True if the telemetry indicates any lossy rewrite happened.

    Convenience for integrations that want WARN-level escalation on
    semantic loosening (`empty_union_drops`), gave-up rewrites
    (`union_coexistence_skipped`), size-driven coarsening
    (`size_coarsenings`), depth-driven coarsening
    (`max_inline_depth_reached`), or dangling refs (`refs_unresolved`).
    """
    return any(telemetry.get(k) for k in LOSSY_KEYS)


# ─── JSON Pointer resolution (RFC 6901) ─────────────────────────────────


def resolve_pointer(root: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON Pointer against ``root``.

    Returns the addressed sub-tree, or ``None`` if the pointer is
    unresolvable or external (we don't follow external URIs — those
    are out of scope for an in-flight proxy hook).
    """
    if not isinstance(pointer, str):
        return None
    if pointer == "#":
        return root
    if not pointer.startswith("#/"):
        # External refs (http://, file://, foo.json#/bar, etc.) — out
        # of scope; we don't fetch remote schemas.
        return None
    node = root
    for raw in pointer[2:].split("/"):
        # RFC 6901 escape codes — order matters: decode ~1 before ~0.
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
        else:
            return None
    return node


# ─── Ref graph + cycle detection (Tarjan SCC) ───────────────────────────


def _collect_refs(node: Any, path: tuple, out: list) -> None:
    """Walk ``node`` recording each `$ref` as ``(use_site_path, target)``."""
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            out.append((path, node["$ref"]))
            # Do NOT recurse further into a $ref node — by spec the
            # sibling keys (if any) are ignored when $ref is present
            # in older drafts; in 2020-12 they're allowed but the ref
            # is the relevant edge for graph purposes.
            return
        for k, v in node.items():
            _collect_refs(v, (*path, k), out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _collect_refs(v, (*path, i), out)


def build_ref_graph(root: Any) -> list[tuple[tuple, str]]:
    """Return a list of ``(use_site_path, target_pointer)`` edges.

    Tested as a public API so callers can audit ref usage before
    deciding whether normalization is safe.
    """
    out: list = []
    _collect_refs(root, (), out)
    return out


def find_ref_cycles(root: Any) -> set[str]:
    """Return the set of `$ref` target pointers that participate in a
    cycle in ``root``'s ref graph.

    Algorithm: Tarjan-style strongly-connected components over the
    graph whose nodes are `$ref` targets and whose edges go from
    target A to target B whenever resolving A leads to a schema that
    transitively references B. An SCC of size >1, or a singleton SCC
    with a self-loop, marks all its members as cyclic.
    """
    # Build adjacency: for each ref target, find refs reachable from
    # *within* that target's resolved subtree.
    edges = build_ref_graph(root)
    target_refs: dict[str, set[str]] = {}
    for _, target in edges:
        target_refs.setdefault(target, set())

    for target in list(target_refs.keys()):
        subtree = resolve_pointer(root, target)
        if subtree is None:
            continue
        sub_edges: list = []
        _collect_refs(subtree, (), sub_edges)
        for _, t in sub_edges:
            target_refs[target].add(t)
            target_refs.setdefault(t, set())

    # Tarjan SCC.
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    cyclic: set[str] = set()

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlinks[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in target_refs.get(v, ()):
            if w not in indices:
                strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif w in on_stack:
                lowlinks[v] = min(lowlinks[v], indices[w])
        if lowlinks[v] == indices[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1 or v in target_refs.get(v, ()):
                cyclic.update(scc)

    for v in list(target_refs.keys()):
        if v not in indices:
            strongconnect(v)

    return cyclic


# ─── Walk context ───────────────────────────────────────────────────────


class WalkContext:
    """State threaded through the recursive normalization walk.

    ``root`` is the schema root for `$ref` resolution; ``visited_refs``
    is the per-traversal-path set used for depth-first cycle detection
    during inlining; ``inlined_node_count`` accumulates the rough size
    of all inlined sub-schemas to enforce ``SIZE_BUDGET``.

    Plain class rather than ``@dataclass`` — LiteLLM's dynamic callback
    importer leaves the host module out of ``sys.modules`` during
    callback load, which breaks the dataclass decorator's type
    introspection.
    """

    __slots__ = (
        "inline_depth",
        "inlined_node_count",
        "ref_warnings_emitted",
        "root",
        "telemetry",
        "visited_refs",
    )

    def __init__(self, root: Any, telemetry: dict) -> None:
        self.root = root
        self.telemetry = telemetry
        self.visited_refs: set[str] = set()
        self.inline_depth: int = 0
        self.inlined_node_count: int = 0
        self.ref_warnings_emitted: int = 0


# ─── Helpers ────────────────────────────────────────────────────────────


def _empty_dict(x: Any) -> bool:
    return isinstance(x, dict) and len(x) == 0


def _node_count(node: Any) -> int:
    """Approximate node count for size-budget tracking."""
    if isinstance(node, dict):
        return 1 + sum(_node_count(v) for v in node.values())
    if isinstance(node, list):
        return 1 + sum(_node_count(v) for v in node)
    return 1


def _merge_properties(base: dict, override: dict) -> dict:
    merged = dict(base)
    merged.update(override)
    return merged


def _merge_required(base: list, override: list) -> list:
    seen = set(base)
    out = list(base)
    for k in override:
        if k not in seen:
            out.append(k)
            seen.add(k)
    return out


# ─── Phase B: $ref inlining ─────────────────────────────────────────────


def _inline_ref(node: dict, ctx: WalkContext) -> Any:
    """Resolve a `$ref` node by inlining its target.

    Cycle detection: if the target is already on the current
    traversal path (``visited_refs``), the inline would not terminate
    — leave the ref in place. Depth cap: if ``inline_depth`` exceeds
    ``MAX_INLINE_DEPTH``, coarsen to ``{"type": "object"}``. Budget
    cap: if the accumulated inline size would cross ``SIZE_BUDGET``,
    also coarsen.
    """
    ref = node["$ref"]
    if ref in ctx.visited_refs:
        ctx.telemetry["cycles_preserved"] += 1
        return node  # cyclic — leave as-is for llama.cpp's own resolver

    target = resolve_pointer(ctx.root, ref)
    if target is None:
        ctx.telemetry["refs_unresolved"] += 1
        # Rate-limit per-ref WARN lines per schema. Aggregate counter
        # in telemetry still reflects all of them; this just trims log
        # spam when an MCP server is emitting hundreds of dangling refs.
        if ctx.ref_warnings_emitted < MAX_PER_SCHEMA_REF_WARNINGS:
            ctx.ref_warnings_emitted += 1
            if STRICT_UNRESOLVED_REFS:
                logger.warning(
                    "unresolvable $ref left in place (STRICT mode); "
                    "llama.cpp will reject the containing tool",
                    extra={"ref": ref, "strict": True},
                )
            else:
                logger.warning(
                    "unresolvable $ref replaced with permissive {} fallback",
                    extra={"ref": ref, "strict": False},
                )
        if STRICT_UNRESOLVED_REFS:
            return node
        # Default: replace with the permissive empty schema. A dangling
        # ref crashes llama.cpp's grammar converter and takes the whole
        # tool down; `{}` means "match anything" — the field loses its
        # specific type spec but the request goes through. The WARN
        # log surfaces the upstream bug so operators can file it
        # (commonly `zod-to-json-schema` emitting refs to envelopes
        # its own singleton-union collapse pass has removed; see the
        # 2026-05-28 paperclip investigation).
        return {}

    if ctx.inline_depth >= MAX_INLINE_DEPTH:
        ctx.telemetry["max_inline_depth_reached"] += 1
        return {"type": "object"}

    target_size = _node_count(target)
    if ctx.inlined_node_count + target_size > SIZE_BUDGET:
        ctx.telemetry["size_coarsenings"] += 1
        return {"type": "object"}

    ctx.inlined_node_count += target_size
    ctx.telemetry["refs_inlined"] += 1

    # Inline a deep copy and walk it so transitively-referenced refs
    # also get resolved.
    inlined = copy.deepcopy(target)
    ctx.visited_refs.add(ref)
    ctx.inline_depth += 1
    try:
        walked = _walk(inlined, ctx)
    finally:
        ctx.inline_depth -= 1
        ctx.visited_refs.discard(ref)
    return walked


# ─── Phase A: anyOf-beside-siblings distribution + not-strip ────────────


def _distribute_union_siblings(node: dict, union_key: str, telemetry: dict) -> dict:
    """Rewrite ``{anyOf: [...], properties: {...}, ...}`` into
    ``{anyOf: [{...siblings + branch_i...}]}``."""
    siblings = {k: v for k, v in node.items() if k != union_key}
    if not siblings:
        return node

    branches = node[union_key]
    new_branches = []
    for branch in branches:
        if not isinstance(branch, dict):
            new_branches.append(branch)
            continue
        merged = dict(siblings)
        for k, v in branch.items():
            if (
                k == "properties"
                and isinstance(merged.get("properties"), dict)
                and isinstance(v, dict)
            ):
                merged["properties"] = _merge_properties(merged["properties"], v)
            elif (
                k == "required" and isinstance(merged.get("required"), list) and isinstance(v, list)
            ):
                merged["required"] = _merge_required(merged["required"], v)
            else:
                merged[k] = v
        new_branches.append(merged)

    telemetry[_UNION_TELEMETRY_KEY[union_key]] += 1
    return {union_key: new_branches}


def _flatten_inner_union(branches: list, union_key: str) -> tuple[list, bool]:
    """Lift inner `{union_key: [...]}` branches into the outer list."""
    out: list = []
    changed = False
    for branch in branches:
        if (
            isinstance(branch, dict)
            and len(branch) == 1
            and union_key in branch
            and isinstance(branch[union_key], list)
        ):
            out.extend(branch[union_key])
            changed = True
        else:
            out.append(branch)
    return out, changed


# ─── Walker ─────────────────────────────────────────────────────────────


def _walk(node: Any, ctx: WalkContext) -> Any:
    """Recursively normalize. Returns a *new* node (no input mutation)."""
    if isinstance(node, list):
        return [_walk(item, ctx) for item in node]
    if not isinstance(node, dict):
        return node

    # Phase B: if this node is a $ref, inline before further processing.
    if "$ref" in node and isinstance(node["$ref"], str):
        return _inline_ref(node, ctx)

    # Recurse into children so they're already normalized when we
    # consider the current node's structural rewrites.
    new_node: dict = {}
    for k, v in node.items():
        # `$defs` / `definitions` are the resolution targets, NOT
        # use sites. Walking into them would resolve refs in vacuo
        # and double-count budget. Pass through verbatim.
        if k in ("$defs", "definitions"):
            new_node[k] = v
        else:
            new_node[k] = _walk(v, ctx)

    # Phase A — drop `not: {}` sentinel as a *sibling* keyword. A bare
    # `{"not": {}}` is preserved so the union-branch filter below can
    # recognize and remove it; collapsing it here would leave `{}` —
    # which in JSON Schema means "match anything", a semantic change.
    if "not" in new_node and _empty_dict(new_node["not"]) and len(new_node) > 1:
        del new_node["not"]
        ctx.telemetry["not_drops"] += 1

    # Coexistence guard: `anyOf` + `oneOf` at the same level encode
    # 'must match anyOf AND match oneOf', which can't be distributed
    # without combinatorial fan-out or semantic change.
    if all(uk in new_node and isinstance(new_node[uk], list) for uk in _UNION_KEYS):
        ctx.telemetry["union_coexistence_skipped"] += 1
        return new_node

    # For the first union keyword present:
    #   (a) flatten inner `{uk: [...]}` branches into the outer list,
    #   (b) drop `{"not": {}}` sentinel branches,
    #   (c) drop the entire union if all branches were filtered away,
    #   (d) distribute sibling keywords into the surviving branches,
    #   (e) collapse a single-branch union to its branch.
    for uk in _UNION_KEYS:
        branches = new_node.get(uk)
        if not isinstance(branches, list):
            continue

        flat, did_flatten = _flatten_inner_union(branches, uk)
        if did_flatten:
            new_node[uk] = flat
            branches = flat

        filtered = [
            b
            for b in branches
            if not (isinstance(b, dict) and len(b) == 1 and "not" in b and _empty_dict(b["not"]))
        ]
        if len(filtered) != len(branches):
            ctx.telemetry["not_drops"] += len(branches) - len(filtered)
            new_node[uk] = filtered
            branches = filtered

        if not branches:
            del new_node[uk]
            ctx.telemetry["empty_union_drops"] += 1
            break

        if any(k != uk for k in new_node):
            new_node = _distribute_union_siblings(new_node, uk, ctx.telemetry)
            branches = new_node[uk]

        if len(branches) == 1 and isinstance(branches[0], dict):
            new_node = branches[0]

        break  # coexistence ruled out above; only one union key here

    return new_node


# ─── Public API ─────────────────────────────────────────────────────────


def normalize_schema(schema: Any) -> tuple[Any, dict]:
    """Normalize a single JSON schema. Returns (new_schema, telemetry).

    The full pipeline runs: `$ref` inlining (with cycle detection +
    size budget) → `not: {}` stripping → union-coexistence guard →
    inner-union flatten → branch filter → sibling distribute →
    single-branch collapse.
    """
    telemetry = _empty_telemetry()
    root_copy = copy.deepcopy(schema)
    ctx = WalkContext(root_copy, telemetry)
    out = _walk(root_copy, ctx)
    # After normalization, `$defs` / `definitions` are no longer
    # referenced (all `$ref`s have been inlined). Strip them so the
    # downstream payload is smaller and llama.cpp doesn't try to
    # build rules for unreferenced sub-schemas.
    if isinstance(out, dict):
        out.pop("$defs", None)
        out.pop("definitions", None)
    # One trailing WARN if per-ref warnings were rate-limited — gives
    # operators the suppressed count without spamming individual lines.
    suppressed = telemetry["refs_unresolved"] - ctx.ref_warnings_emitted
    if suppressed > 0:
        logger.warning(
            "unresolvable $ref WARN lines rate-limited; %d more occurred "
            "(total refs_unresolved in telemetry)",
            suppressed,
            extra={"suppressed": suppressed, "refs_unresolved_total": telemetry["refs_unresolved"]},
        )
    return out, telemetry


def normalize_tools(tools: Any) -> tuple[Any, dict]:
    """Normalize every ``function.parameters`` entry in a tools list."""
    aggregate = _empty_telemetry()
    aggregate["tools_seen"] = 0
    aggregate["tools_modified"] = 0

    if not isinstance(tools, list):
        return tools, aggregate

    out_tools = []
    for tool in tools:
        if not (
            isinstance(tool, dict)
            and tool.get("type") == "function"
            and isinstance(tool.get("function"), dict)
            and "parameters" in tool["function"]
        ):
            out_tools.append(tool)
            continue

        aggregate["tools_seen"] += 1
        params = tool["function"]["parameters"]
        new_params, per_tool = normalize_schema(params)
        if any(per_tool[k] for k in _MODIFYING_KEYS):
            aggregate["tools_modified"] += 1
        for k in per_tool:
            if k in aggregate:
                aggregate[k] += per_tool[k]

        new_tool = dict(tool)
        new_tool["function"] = dict(tool["function"])
        new_tool["function"]["parameters"] = new_params
        out_tools.append(new_tool)

    return out_tools, aggregate
