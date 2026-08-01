# Upstream Provenance Record

## Immutable source

| Field | Value |
|---|---|
| Upstream project | `NousResearch/hermes-agent` |
| Upstream URL | `https://github.com/NousResearch/hermes-agent` |
| Release tag | `v2026.7.30` |
| Package version | `0.19.1` |
| Annotated tag object | `d25e2dbdbc40b49808c0a0e9cfed21cc90cffab3` |
| Peeled commit | `cc4cab2f592e60a197e796506de9168f74baf3ea` |
| Git tree | `fcdc6093750ed0a3a556e20927799d7245ba65e4` |
| Upstream remote name | `upstream` |
| Retrieval date | `2026-08-01` |

The peeled commit, not the mutable `main` branch or the annotated tag object,
is the downstream baseline identity.

## Integrity evidence

| Artifact | SHA-256 |
|---|---|
| `LICENSE` | `821556e6336796450ab852d375117b48a4887e71d255794fd6318d99982a5ab6` |
| `pyproject.toml` | `cb2789b2b080587f78d9155418e8ff4b32b1a7af3dd84d3dca8444e9bc9b0bac` |
| `uv.lock` | `ba099f6825c2ba2d85893d2a48fce9c31b46b68399c58894bd9e5064a77bc12b` |

The checkout contained 8,071 tracked files at the pinned commit. The initial
inventory counted 3,660 Python files, 2,000 TypeScript/JavaScript files, and
3,188 paths classified as test-related by the bootstrap inventory command.
These counts describe the pinned baseline and are evidence, not future CI
invariants.

## License obligations

The pinned upstream is licensed under the MIT License, copyright Nous
Research. The original `LICENSE` file remains unmodified. Youtab public
branding may change, but legal copyright, license, SBOM, and provenance records
must remain truthful and must not be removed.

## Downstream remote policy

- `upstream` is read-only by policy for Youtab work.
- A future private Youtab repository will be configured as `origin`.
- Downstream branches are based on immutable upstream commits.
- Every core divergence must be registered in `PATCH_REGISTRY.md` with tests,
  rollback, and an upstream-sync decision.
- Fetching a newer upstream version never authorizes merging or deploying it.
