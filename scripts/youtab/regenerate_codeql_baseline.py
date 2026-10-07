"""Rebuild a CodeQL baseline from a SARIF run, with provenance.

Why this exists: the baseline is a 978 KB generated artifact that
``codeql_sarif_gate.py`` validates strictly -- schema, provenance, finding
totals, query identities, extracted-source inventory. Before this script the
only way to refresh it was by hand, so in practice it was never refreshed:
findings that had been FIXED stayed listed, and a baseline carrying a finding
that no longer exists will silently re-accept that defect if it returns at the
same line hash. The gate reports how many such entries it sees ("absent"), and
this script is how you clear them.

Usage, from the repository root::

    # 1. Download the SARIF artifact from the CodeQL workflow run:
    #      gh run download <run-id> -n codeql-sarif-python-<sha> -D sarif/
    # 2. Rebuild the baseline from it:
    python scripts/youtab/regenerate_codeql_baseline.py \\
        sarif/ python \\
        --source-run https://github.com/<owner>/<repo>/actions/runs/<run-id> \\
        --source-head <sha>

By default it writes ``scripts/youtab/codeql_baselines/<language>.json`` and
prints what changed against the baseline already on disk. Pass ``--stdout`` to
inspect the result without writing.

**Review the diff before committing.** A refresh legitimately removes fixed
findings; it would just as happily record a regression as accepted debt. The
printed added/removed rule summary is there so that distinction is visible in
review rather than buried in a 978 KB diff.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from codeql_sarif_gate import finding_key  # noqa: E402  (same directory)

EXPECTED_QUERIES = {
    "python": "codeql/python-queries",
    "javascript-typescript": "codeql/javascript-queries",
}


def is_security(rule: dict) -> bool:
    """Whether a SARIF rule descriptor is a security rule.

    CodeQL tags security queries two ways and does not always set both, so we
    accept either: a ``security-severity`` property, or a ``security`` tag.
    """
    properties = rule.get("properties") or {}
    if properties.get("security-severity") is not None:
        return True
    return any(
        isinstance(tag, str) and tag.startswith("external/cwe")
        or tag == "security"
        for tag in properties.get("tags") or []
    )


def build(sarif_dir: Path, language: str, source_run: str, source_head: str) -> dict:
    paths = sorted(sarif_dir.glob("*.sarif"))
    if not paths:
        raise SystemExit(f"no *.sarif found in {sarif_dir}")
    expected_queries = EXPECTED_QUERIES.get(language)
    if not expected_queries:
        raise SystemExit(f"unsupported language {language!r}; expected one of {sorted(EXPECTED_QUERIES)}")

    findings: Counter[str] = Counter()
    severities: dict[str, bool] = {}
    runs: list[dict] = []
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("version") != "2.1.0":
            raise SystemExit(f"{path}: unsupported SARIF version")
        for run in document["runs"]:
            if ((run.get("tool") or {}).get("driver") or {}).get("name") != "CodeQL":
                raise SystemExit(f"{path}: analysis is not from CodeQL")
            runs.append(run)

    if len(runs) != 1:
        raise SystemExit(
            f"expected exactly one CodeQL run for {language}, found {len(runs)} -- "
            "the gate enforces the same, so pass one language's SARIF at a time"
        )
    run = runs[0]

    if (run.get("automationDetails") or {}).get("id") != f"/language:{language}/":
        raise SystemExit(f"SARIF is not a /language:{language}/ analysis")

    query_sets = [item for item in run["tool"].get("extensions", [])
                  if item.get("name") == expected_queries]
    if len(query_sets) != 1:
        raise SystemExit(f"SARIF does not carry exactly one {expected_queries} extension")
    rules = query_sets[0].get("rules") or []
    for rule in rules:
        if isinstance(rule.get("id"), str):
            severities[rule["id"]] = is_security(rule)

    for result in run["results"]:
        if any(item.get("status") == "accepted" for item in result.get("suppressions", [])):
            continue
        findings[finding_key(result)] += 1

    open_total = sum(findings.values())
    open_security = sum(
        count for key, count in findings.items()
        if severities.get(json.loads(key)[0], False)
    )

    diagnostic = ("py" if language == "python" else "js") + "/diagnostics/successfully-extracted-files"
    extracted = sorted({
        item["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for invocation in run["invocations"]
        for item in invocation.get("toolExecutionNotifications", [])
        if (item.get("descriptor") or {}).get("id") == diagnostic and item.get("locations")
    })

    return {
        "schema": 1,
        "language": language,
        "source_run": source_run,
        "source_head": source_head,
        "open_total": open_total,
        "open_security": open_security,
        "open_quality": open_total - open_security,
        "findings": dict(sorted(findings.items())),
        "artifact_count": len(run.get("artifacts", [])),
        "query_count": len(rules),
        "extracted_count": len(extracted),
        "extracted_paths": extracted,
        "required_rules": sorted(severities),
    }


def summarize(old: dict | None, new: dict) -> None:
    if old is None:
        print(f"new baseline: {new['open_total']} findings "
              f"({new['open_security']} security / {new['open_quality']} quality)")
        return
    before, after = Counter(old.get("findings", {})), Counter(new["findings"])
    removed, added = before - after, after - before
    print(f"open_total {old.get('open_total')} -> {new['open_total']} "
          f"({new['open_security']} security / {new['open_quality']} quality)")
    print(f"  removed (fixed or no longer reported): {sum(removed.values())}")
    print(f"  added   (NEW -- review each one)     : {sum(added.values())}")
    for label, delta in (("removed", removed), ("added", added)):
        by_rule = Counter(json.loads(key)[0] for key in delta.elements())
        for rule, count in by_rule.most_common(15):
            print(f"    {label:7s} {count:5d}  {rule}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sarif_dir", type=Path, help="directory holding the downloaded *.sarif")
    parser.add_argument("language", choices=sorted(EXPECTED_QUERIES))
    parser.add_argument("--source-run", required=True,
                        help="URL of the workflow run the SARIF came from (recorded as provenance)")
    parser.add_argument("--source-head", required=True,
                        help="commit SHA the SARIF was produced from")
    parser.add_argument("--out", type=Path, default=None,
                        help="output path (default: scripts/youtab/codeql_baselines/<language>.json)")
    parser.add_argument("--stdout", action="store_true",
                        help="print the baseline instead of writing it")
    args = parser.parse_args()

    baseline = build(args.sarif_dir, args.language, args.source_run, args.source_head)
    rendered = json.dumps(baseline, indent=2, sort_keys=False) + "\n"

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    out = args.out or (Path(__file__).resolve().parent / "codeql_baselines" / f"{args.language}.json")
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    summarize(old, baseline)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    print(f"\nwrote {out}")
    print("Review the diff before committing: a refresh removes fixed findings, "
          "but it would just as happily record a regression as accepted debt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
