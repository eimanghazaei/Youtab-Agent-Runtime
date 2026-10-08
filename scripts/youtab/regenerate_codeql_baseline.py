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

from codeql_sarif_gate import finding_keys  # noqa: E402  (same directory)

# One source of truth for the per-language facts, shared with the gate so a
# baseline cannot be built against a different query pack or extraction
# diagnostic than the gate will later look for.
from codeql_sarif_gate import (  # noqa: E402  (same directory)
    LANGUAGES,
    SEVERITY_BANDS,
    count_by_band,
)


def is_security(rule: dict) -> bool:
    """Whether a SARIF rule descriptor is a security rule.

    Calibrated against the committed baseline rather than guessed: over that
    baseline's own findings, ``security-severity is not None`` and the
    ``security`` tag each yield exactly its recorded ``open_security`` of 105,
    so either reproduces how that number was originally produced. Both are
    accepted because CodeQL does not always set both.

    Deliberately NOT using the ``external/cwe/*`` tags: plenty of pure quality
    queries carry a CWE tag (``py/empty-except`` among them), and counting
    those scores the same baseline at 3655 rather than 105 -- which would
    quietly redefine ``open_security`` into a different metric while looking
    like a finding explosion.
    """
    properties = rule.get("properties") or {}
    if properties.get("security-severity") is not None:
        return True
    return "security" in (properties.get("tags") or [])


def build(sarif_dir: Path, language: str, source_run: str, source_head: str) -> dict:
    paths = sorted(sarif_dir.glob("*.sarif"))
    if not paths:
        raise SystemExit(f"no *.sarif found in {sarif_dir}")
    spec = LANGUAGES.get(language)
    if not spec:
        raise SystemExit(f"unsupported language {language!r}; expected one of {sorted(LANGUAGES)}")
    expected_queries = spec["queries"]

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

    # Everything below mirrors a check `codeql_sarif_gate.inspect` already
    # makes. A baseline built from an artifact the gate would reject is worse
    # than no baseline: it passes review as "regenerated", then fails the
    # advertised round-trip on the next run with no obvious cause.

    # The workflow uploads the SARIF under `if: always()`, so a FAILED CodeQL
    # run still leaves a downloadable artifact. Building a baseline from a
    # partial analysis would bake its missing results in as accepted debt.
    # EVERY invocation, not any: a run with statuses [true, false] is a
    # partially failed analysis, and `any` accepted it.
    invocations = run.get("invocations") or []
    if not invocations or not all(
        item.get("executionSuccessful") is True for item in invocations
    ):
        raise SystemExit(
            "SARIF records no successful invocation -- this is the artifact of a FAILED "
            "or partial CodeQL run (the workflow uploads on always()). Re-run the "
            "analysis and download the artifact from a green run; a baseline built from "
            "this would record the missing results as accepted debt."
        )

    # The gate requires the baseline to have come from `security-and-quality`.
    # A SARIF produced with the default suite has fewer queries, so a baseline
    # built from one silently drops whole rule classes to zero.
    queries = (run.get("properties") or {}).get("codeqlConfigSummary", {}).get("queries")
    if queries != [{"type": "builtinSuite", "uses": "security-and-quality"}]:
        raise SystemExit(
            f"SARIF was produced with query suite {queries!r}, not the "
            "security-and-quality suite the gate requires. A baseline built from it "
            "would zero out every rule the narrower suite does not run."
        )

    # `--source-head` is recorded as the baseline's provenance and nothing
    # downstream re-derives it, so a mistyped SHA or the wrong run's artifact
    # yields a baseline that is accepted while describing a different tree.
    revisions = {
        item.get("revisionId")
        for item in run.get("versionControlProvenance") or []
        if item.get("revisionId")
    }
    if revisions and source_head not in revisions:
        raise SystemExit(
            f"--source-head {source_head} does not match the SARIF's own provenance "
            f"({', '.join(sorted(revisions))}). Either the wrong run's artifact was "
            "downloaded or the SHA was mistyped; the baseline would claim to describe a "
            "tree it was not built from."
        )

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
        # One result can carry several identities: CodeQL coalesces
        # graph diagnostics that share a location into one newline-joined
        # message. See `finding_keys`.
        for key in finding_keys(result):
            findings[key] += 1

    open_total = sum(findings.values())
    open_security = sum(
        count for key, count in findings.items()
        if severities.get(json.loads(key)[0], False)
    )

    diagnostic = spec["diagnostic"]
    extracted = sorted({
        item["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for invocation in run["invocations"]
        for item in invocation.get("toolExecutionNotifications", [])
        if (item.get("descriptor") or {}).get("id") == diagnostic and item.get("locations")
    })

    baseline = {
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
        # The ratchet. Counted off the RESULTS so it stays comparable across a
        # refresh, which is what lets `main()` below refuse to raise it. The
        # gate rejects a baseline that has no budget, so this control cannot be
        # dropped by regenerating with an older copy of this script.
        "severity_budget": count_by_band(run),
    }

    # The coverage metadata is DERIVED from the artifact, so a partial SARIF
    # that still marks its invocation successful yields zeros here -- no
    # `artifacts`, no rule descriptors, no extraction notifications -- and
    # `build()` would return normally and let `main()` overwrite the reviewed
    # baseline with it.
    #
    # The gate requires every one of these counts to be positive and
    # `required_rules` to be non-empty, so such a baseline is rejected the
    # moment it is used: "missing analysis coverage metadata". The regeneration
    # workflow would have turned one degraded artifact into a committed
    # baseline that fails EVERY subsequent CodeQL job, with the cause three
    # steps removed from the symptom. Refuse here, where the artifact is still
    # in hand and the fix is obvious.
    coverage = {
        "artifact_count": baseline["artifact_count"],
        "query_count": baseline["query_count"],
        "extracted_count": baseline["extracted_count"],
    }
    empty = sorted(name for name, value in coverage.items() if value <= 0)
    if empty:
        raise SystemExit(
            f"SARIF reports no {', '.join(empty)} -- the analysis ran but produced no "
            f"coverage metadata, which is a partial artifact even though its invocation "
            f"is marked successful. The gate requires all of these to be positive and "
            f"would reject the resulting baseline on every later run. Re-download from a "
            f"complete run."
        )
    if len(set(baseline["extracted_paths"])) != baseline["extracted_count"]:
        raise SystemExit(
            "SARIF extraction inventory contains duplicates -- the gate requires "
            "extracted_paths to be unique and the same length as extracted_count."
        )
    if not baseline["required_rules"]:
        raise SystemExit(
            "SARIF carries no rule identities, so required_rules would be empty and the "
            "gate would reject the baseline. Re-download from a complete run."
        )
    # A legitimately CLEAN analysis is allowed to produce an empty baseline,
    # and for a small language that is the strongest gate there is: with
    # nothing accepted, every future finding is new. `rust` reports 0 findings
    # today.
    #
    # The earlier blanket refusal reasoned that an empty baseline "would make
    # the gate's own 'zero findings against a nonempty baseline' guard
    # unreachable and accept anything afterwards". The first half is true and
    # the second does not follow: with an empty baseline `new = current -
    # baseline` is the whole current set, so a later finding fails the gate at
    # exit 1. That guard exists to catch a run that reports nothing against a
    # baseline that has entries -- it is simply inapplicable here, not
    # bypassed.
    #
    # What actually distinguishes "clean" from "partial artifact" is the
    # coverage metadata checked immediately above -- a successful invocation,
    # the expected query pack, and positive artifact / query / extraction
    # counts. A truncated run fails those. So zero findings is accepted only
    # together with evidence that the analysis really ran.
    if not baseline["findings"]:
        print(
            f"NOTE: {language} reports zero findings. The baseline will be empty, which "
            f"means NOTHING is accepted for this language -- every future finding fails "
            f"the gate. That is intended for a clean language; if this run was supposed "
            f"to have findings, re-download from a complete run instead of committing "
            f"this."
        )

    return baseline


def enforce_ratchet(old: dict | None, new: dict, accepted_reason: str | None) -> None:
    """Refuse to write a baseline that raises any severity budget.

    This is the half of the ratchet that cannot live in the gate. The gate
    compares a run against a committed budget; nothing there stops someone
    committing a HIGHER budget. And raising it is the easy mistake, not a
    malicious one: a refresh is the documented response to a legitimate
    cleanup, the diff is a megabyte of JSON, and a new `py/full-ssrf` arriving
    in the same refresh moves one integer inside it.

    Quality bands are not ratcheted -- only the security ones, plus the
    `security` total. Quality findings churn with ordinary refactoring and
    budgeting them would make the escape hatch routine, which is how a control
    stops being read.
    """
    if old is None:
        return
    before = old.get("severity_budget") or {}
    after = new.get("severity_budget") or {}
    if not isinstance(before, dict):
        return
    ratcheted = [name for name, _floor in SEVERITY_BANDS] + ["security"]
    raised = [
        (name, int(before[name]), int(after.get(name, 0)))
        for name in ratcheted
        if isinstance(before.get(name), int)
        and int(after.get(name, 0)) > int(before[name])
    ]
    if not raised:
        return
    detail = "; ".join(f"{name} {was} -> {now}" for name, was, now in raised)
    if not accepted_reason:
        raise SystemExit(
            f"REFUSING to write this baseline: it RAISES a severity budget ({detail}).\n"
            f"\n"
            f"A refresh is meant to remove findings that were fixed. Raising a budget "
            f"means this artifact contains security findings the previous baseline did "
            f"not, and writing it would record them as reviewed debt -- inside a diff "
            f"too large to notice them in.\n"
            f"\n"
            f"Fix the findings, or, if they are genuinely accepted, re-run with\n"
            f"  --accept-severity-regression \"<why, in a sentence a reviewer will read>\"\n"
            f"which records the reason IN the baseline so it shows up in review."
        )
    if len(accepted_reason.split()) < 5:
        raise SystemExit(
            f"--accept-severity-regression needs a real reason, not "
            f"{accepted_reason!r}. It is written into the baseline and read in review."
        )
    new["severity_budget_raised"] = {
        "detail": detail,
        "reason": accepted_reason,
    }
    print(f"WARNING: severity budget RAISED ({detail}) -- reason recorded in the baseline.")


def summarize(old: dict | None, new: dict) -> None:
    if old is None:
        print(f"new baseline: {new['open_total']} findings "
              f"({new['open_security']} security / {new['open_quality']} quality)")
        return
    before, after = Counter(old.get("findings", {})), Counter(new["findings"])
    removed, added = before - after, after - before
    print(f"open_total {old.get('open_total')} -> {new['open_total']} "
          f"({new['open_security']} security / {new['open_quality']} quality)")
    was, now = old.get("severity_budget") or {}, new.get("severity_budget") or {}
    moved = [
        f"{name} {was.get(name, 0)} -> {now.get(name, 0)}"
        for name, _floor in SEVERITY_BANDS
        if was.get(name, 0) != now.get(name, 0)
    ]
    print("  severity budget: " + ("; ".join(moved) if moved else "unchanged"))
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
    parser.add_argument("language", choices=sorted(LANGUAGES))
    parser.add_argument("--source-run", required=True,
                        help="URL of the workflow run the SARIF came from (recorded as provenance)")
    parser.add_argument("--source-head", required=True,
                        help="commit SHA the SARIF was produced from")
    parser.add_argument("--out", type=Path, default=None,
                        help="output path (default: scripts/youtab/codeql_baselines/<language>.json)")
    parser.add_argument("--stdout", action="store_true",
                        help="print the baseline instead of writing it")
    parser.add_argument("--accept-severity-regression", default=None, metavar="REASON",
                        help="deliberately RAISE a severity budget, recording REASON in "
                             "the baseline. Refused without this flag.")
    args = parser.parse_args()

    baseline = build(args.sarif_dir, args.language, args.source_run, args.source_head)

    if args.stdout:
        rendered = json.dumps(baseline, indent=2, sort_keys=False) + "\n"
        sys.stdout.write(rendered)
        return 0

    out = args.out or (Path(__file__).resolve().parent / "codeql_baselines" / f"{args.language}.json")
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    # Before anything is written, and before the human-readable summary, so a
    # refused refresh cannot leave a half-written baseline or a summary that
    # reads like success.
    enforce_ratchet(old, baseline, args.accept_severity_regression)
    rendered = json.dumps(baseline, indent=2, sort_keys=False) + "\n"
    summarize(old, baseline)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    print(f"\nwrote {out}")
    print("Review the diff before committing: a refresh removes fixed findings, "
          "but it would just as happily record a regression as accepted debt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
