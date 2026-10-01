# ADR-0003 — Extensible Tool Registry (no fixed tool-count ceiling)

**Status:** Accepted (2026-09-07, WAVE-30H R11)
**Relates to:** ADR-0002 (Simorgh admission/execution contract), `CANONICAL-SIGNATURE-GRANT.md`

## Context

Earlier waves tried to certify the runtime against a fixed model-facing tool
count ("38/0/0"). That number was never recoverable from evidence and, worse,
*a fixed ceiling is the wrong contract*: 38, 61 and 69 are observations or
snapshots, not architectural limits. The Owner decision (binding) is that the
runtime must support an **open, extensible** set of tools with **no global
maximum**, while guaranteeing that nothing authorized is ever silently hidden or
made unreachable.

The registry already had the right shape: `ToolRegistry.register()`
(`tools/registry.py`) is the single choke point through which every tool —
built-in, plugin, MCP, device-local — flows, and nothing downstream
(`get_definitions`, `dispatch`, `handle_function_call`, `tool_search`) is
hard-coded to specific tool names. A repo-wide check confirms **no `== 38/61/69`
or equivalent tool-count ceiling exists in core**. This ADR formalizes the
extensible contract on top of that seam.

## Decision

1. **No fixed tool-count ceiling.** No code path asserts a global tool count.
   Certification uses the *discoverability invariant* below, not a magic number.

2. **One extensible registration seam.** Every tool — including a new
   `computer_use`-class, plugin, MCP, or device-local tool — is registered by a
   single `register_spec(ToolSpec)` (or the underlying `register()`), with **zero
   edits to any core tool list or routing switch**. Dispatch is a pure
   `_tools[name].handler` lookup.

3. **`ToolSpec` metadata contract (additive).** `ToolSpec` and `ToolEntry` carry,
   beyond name/toolset/schema/handler: `version`, `schema_hash` (sha256-16 of the
   canonical schema, auto-computed), `capabilities` (categories), `platforms`,
   `side_effect_class` (`none|read|write|network|process|credential|memory_write`),
   `requires_approval`, `timeout_seconds`, and `provenance`
   (`builtin|plugin|mcp|device-local|dynamic`). These are audit/selection metadata
   and **never gate routing**; existing `register()` callers are unchanged (all
   default).

4. **Capability-preserving hybrid discovery.** Progressive schema loading
   (`tool_search`) is permitted ONLY because tool *existence and capability stay
   visible*: `describe_index()` returns a compact, truthful index of **every**
   registered tool with honest `available` (operational — `check_fn` passes) and
   `authorized` (within the Simorgh grant envelope) flags. An unavailable or
   unauthorized tool is still listed (existence visible) but is not advertised as
   callable. Same-run deterministic discovery → full-schema load → invocation is
   guaranteed.

5. **Simorgh grant = maximum authorized capability envelope.** Intelligent
   selection operates only within `allowed_toolsets` of the admitted grant
   (ADR-0002; enforced by `AuthorityBoundary.decide_tool`). The registry never
   widens authority; the grant never invents a tool.

6. **Per-run provenance manifest.** `tool_manifest(tool_names)` freezes the exact
   names + versions + schema hashes (+ toolset/side-effect/provenance) for a run,
   for audit and benchmark reproducibility. It is a **snapshot, never a ceiling**:
   adding a tool grows the manifest; nothing is capped.

7. **Fail closed on authorization/integrity; never silently hide.** Unknown
   ownership, collisions without opt-in, and bad integrity fail closed
   (`register()` rejects cross-toolset shadowing unless `override=True`; an
   invalid `side_effect_class` raises). An *otherwise-authorized* tool is never
   silently dropped — an unavailable provider is reported as `available=False`,
   not omitted from the index.

## Acceptance invariant (replaces "38/0/0")

For every registered, enabled (operational) and Simorgh-authorized tool:

* **Unexpected missing = 0** — it is discoverable and selectable.
* **Undocumented hidden = 0** — its existence is in the discovery index.
* **Unauthorized callable = 0** — nothing outside the grant envelope is advertised
  as authorized/callable.
* **Unavailable falsely advertised as callable = 0** — an unavailable tool is
  never in the model-facing definitions, though its existence stays visible.

## Proof

`tests/tools/test_extensible_registry.py` registers a synthetic `computer_use`
tool via one `register_spec` call and proves discover → select → invoke with no
core-routing edit; that registering 200 tools grows the manifest with no cap; and
the four-zero invariant, plus dynamic registration, removal, versioning,
schema-hash change, authorization denial, unavailable providers and name
collisions.

## Consequences

* Adding tools is a data operation, not a code-routing change.
* Certification is invariant-based and stable across tool-set growth.
* The per-run manifest gives reproducible, auditable provenance without ever
  implying a global maximum.
