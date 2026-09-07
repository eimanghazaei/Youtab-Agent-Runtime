"""Print R8 latency statistics from collected stage traces (WAVE-30H R8).

Reads spans from EITHER a JSONL trace directory (the experiment sink) OR the
principal-bound run_journal ``timing`` category, aggregates them into
p50/p95/p99 + cold-vs-warm + process-cold-vs-warm, and prints the
bottleneck-ordered table.

Examples::

    # from the default per-pid JSONL traces under the agent home
    python -m youtab_runtime.stage_trace_cli --traces-dir ~/.youtab-agent-runtime/runtime/stage_traces

    # from the durable journal, for one principal (optionally one run)
    python -m youtab_runtime.stage_trace_cli --journal --tenant acme --user alice
    python -m youtab_runtime.stage_trace_cli --journal --tenant acme --user alice --run-id run-123

    # emit machine-readable JSON instead of the table
    python -m youtab_runtime.stage_trace_cli --traces-dir ./traces --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Iterable, Mapping

from youtab_runtime import stage_trace_report as rep


def _load(args: argparse.Namespace) -> Iterable[Mapping[str, Any]]:
    if args.journal:
        if not (args.tenant and args.user):
            raise SystemExit("--journal requires --tenant and --user")
        return list(
            rep.read_journal(
                args.tenant, args.user, run_id=args.run_id, db_path=args.db_path
            )
        )
    if args.traces_dir:
        return list(rep.read_dir(args.traces_dir))
    if args.traces_file:
        return list(rep.read_jsonl(args.traces_file))
    raise SystemExit("provide --traces-dir, --traces-file, or --journal")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="youtab-stage-trace", description=__doc__)
    src = p.add_argument_group("source")
    src.add_argument("--traces-dir", help="directory of trace-*.jsonl files")
    src.add_argument("--traces-file", help="a single trace .jsonl file")
    src.add_argument("--journal", action="store_true",
                     help="read from the run_journal timing category")
    src.add_argument("--tenant", help="journal principal tenant")
    src.add_argument("--user", help="journal principal user")
    src.add_argument("--run-id", help="limit journal read to one run")
    src.add_argument("--db-path", help="explicit run_journal.db path")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="emit stats as JSON instead of the text table")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = _load(args)
    stats = rep.aggregate(records)
    if args.as_json:
        json.dump({k: v.to_dict() for k, v in stats.items()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        total = sum(s.count for s in stats.values())
        print(f"# R8 stage-latency report — {total} spans across {len(stats)} stages")
        print(rep.format_report(stats))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
