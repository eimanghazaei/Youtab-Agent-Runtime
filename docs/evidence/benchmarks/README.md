# Benchmark evidence index (append-only)

Each benchmark generation is a **separate, immutable, dated record**. New generations are
**added** to this table; existing rows and directories are **never overwritten, amended,
squashed, renamed away, or deleted**. A faster later benchmark does not invalidate or replace
an earlier one.

| Evidence ID | Date | Environment | Source SHA | Production path | Tunnel | Model / provider | Kind | Status | Primary-report SHA-256 | Path |
|---|---|---|---|---|---|---|---|---|---|---|
| wave30h-local-tunnel-20260908 | 2026-09-08 | Windows → SSH tunnel → macOS/Ollama | `40b3d69aa` (+ uncommitted step-3 tree) | real `runner=cli` worker (over tunnel) | YES | `youtab-qwen35-9b-agent-64k:latest` | local/tunnel | historical baseline — NOT production/Linux/cloud | `ee5c5d43306d2117d64d5c95e9fc9cb76a6986ad4b2c138cfbf2dfe313ebc88b` | `wave-30h/2026-09-08-local-tunnel/` |
| _(pending)_ | — | Linux CI runner | _exact candidate SHA_ | real production path | NO | same model | Linux production-path | NOT STARTED (needs authorized push / Linux host) | — | `wave-30h/<date>-linux-production-path/` |
| _(pending)_ | — | Owner-approved cloud provider | _exact candidate SHA_ | real production path | NO | _Owner-approved model_ | cloud | NOT STARTED (needs Owner-approved provider) | — | `wave-30h/<date>-cloud-<provider>/` |

## Rules
- **Append-only.** Add a row; never edit or remove a historical row except to correct a typo in
  metadata (never to alter a measured result).
- **Additive directories.** Each generation gets its own dated directory with its own
  `MANIFEST.sha256`. Never write new results into an older generation's directory.
- **Honest labels.** Every record states its environment, whether it is production evidence,
  whether a tunnel was present, and its known limitations.
- **Traceability.** Every headline claim in a record must trace to a raw record in that record's
  `raw/` (see each record's `PROVENANCE.md`).
