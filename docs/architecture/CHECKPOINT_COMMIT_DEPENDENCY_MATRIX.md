# Checkpoint Commit Dependency Matrix (item 3)

Question: do later Memory commits depend on
`a990839b feat(runtime): hash-chained task checkpoint capsule`?

## Mechanical findings (candidate `34f57ab7`)

| Check | Result |
|---|---|
| Importers of `youtab_runtime.continuity` anywhere in tree | ONLY `tests/youtab_runtime/test_task_checkpoint.py` |
| `youtab_runtime/__init__.py` exports/imports continuity | NO |
| `youtab_runtime/memory/__init__.py` imports continuity | NO |
| Any `youtab_runtime/memory/*.py` imports continuity | NO |
| Direction of dependency | `continuity/checkpoint.py` imports `youtab_runtime.memory.scope` (continuity → memory, ONE-WAY) |

## Commit → continuity file touch map

| Commit | Touches `continuity/` | Memory code depends on it? |
|---|---|---|
| `41a89413` scope/claim/router | no | — |
| `930a11d8` MemoryBus | no | — |
| `a990839b` **checkpoint capsule** | YES (adds package) | NO — leaf, nothing in Memory imports it |
| `68bf3501` budget | no | no |
| `c7333822` checklist | no | no |
| `01451353` review fixes | YES (docstrings in continuity) | NO |
| `34f57ab7` docs | no | no |

## Conclusion

`youtab_runtime.continuity` (the `TaskCheckpoint`/`CheckpointChain` implementation) is a
**dependency leaf**: it consumes `memory.scope` but **no Memory module, package export, or
Memory commit depends on it**. 

**Master Integrator action if the Durable Execution owner does NOT adopt the interface:**
exclude the implementation cleanly by dropping/reverting the continuity code and its test —
specifically `youtab_runtime/continuity/__init__.py`, `youtab_runtime/continuity/checkpoint.py`,
`tests/youtab_runtime/test_task_checkpoint.py` (introduced in `a990839b`, docstring-touched in
`01451353`). Keep only the interface/docs
(`RUNTIME_DURABLE_EXECUTION_INTERFACE_REQUEST.md`, ownership matrix, ADR-0006 references).
The rest of the Memory branch compiles, imports and tests unchanged, because nothing imports it.

Classification of `a990839b`: **IMPLEMENTED_NOT_INTEGRATED — safely excludable.**
