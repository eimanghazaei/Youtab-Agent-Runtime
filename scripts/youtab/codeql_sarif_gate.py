"""Fail closed on new CodeQL findings against an explicit repository baseline.

Code Scanning uploads are unavailable for this repository. The baseline records
open debt, not an assertion that the repository has no findings.

Two comparison modes, because one identity scheme does not fit both kinds of
finding:

*Exact-key* (the default, and what this gate always did): a finding is
identified by ``[ruleId, uri, primaryLocationLineHash, startColumnFingerprint]``.
That identity is stable for a defect that lives on one line -- the line hash
survives the file moving around it -- so an unseen key is genuinely a new
finding.

*Per-file count ceiling* (``GRAPH_SCOPED_RULES`` below): for rules whose defect
is a property of the whole module import graph, CodeQL reports one result per
*import statement participating in the cycle*, not one per cycle. Editing any
module in a cycle re-reports every member at a shifted line, so the line hash
changes and the exact-key comparison sees a brand-new finding for a defect that
did not change. Measured on this repository: a pull request whose entire
content was CodeQL remediation reported ``7690 open, 23 new, 22 absent`` -- a
near 1:1 new/absent churn, which is the signature of unstable identity rather
than regression. The gate was consequently red on every pull request AND on
``main``, including on the two pull requests opened to fix CodeQL findings, and
was removed from the required checks -- so the one analysis that looks at the
whole tree constrained nothing.

For those rules we therefore compare a COUNT per ``(ruleId, uri)`` and fail
only when a file's count goes up. Line churn inside a file is invisible;
"this module gained an import cycle" is still caught, and so is "a new file
introduced one". The 2,488 findings this covers are 32% of the Python
baseline, and all 1,809 ``py/cyclic-import`` ones are of the deferred-import
kind (``py/unsafe-cyclic-import`` is 0), i.e. none can fail at import time.

What this does NOT do: tighten the baseline when a finding is fixed. ``absent``
is reported loudly below but not enforced, because there is no way to make a
genuine fix pass the gate until the baseline is refreshed, and refreshing it
wholesale silently accepts real regressions. ``regenerate_codeql_baseline.py``
next to this file makes that refresh a reviewable one-command operation; once
it is in routine use, ``absent`` can be promoted to a failure and the ratchet
closes in both directions.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

# Rules whose result identity is not stable under unrelated edits, because the
# defect is a graph property reported at each participating import statement.
# See the module docstring. Keep this set as small as the evidence justifies --
# every rule added here trades exact-location tracking for churn immunity.
GRAPH_SCOPED_RULES = frozenset({
    "py/cyclic-import",
    "py/import-and-import-from",
})


def rule_of(key: str) -> str:
    """The ruleId embedded in a ``finding_key``."""
    return json.loads(key)[0]


def file_of(key: str) -> str:
    """The artifact URI embedded in a ``finding_key``."""
    return json.loads(key)[1]


def split_by_identity(counts: Counter[str]) -> tuple[Counter[str], Counter[tuple[str, str]]]:
    """Partition findings into exact-key and per-file-count buckets."""
    exact: Counter[str] = Counter()
    scoped: Counter[tuple[str, str]] = Counter()
    for key, count in counts.items():
        rule = rule_of(key)
        if rule in GRAPH_SCOPED_RULES:
            scoped[(rule, file_of(key))] += count
        else:
            exact[key] += count
    return exact, scoped


def finding_key(result: dict) -> str:
    location = result["locations"][0]["physicalLocation"]
    uri = location["artifactLocation"]["uri"]
    fingerprints = result["partialFingerprints"]
    line_hash = fingerprints["primaryLocationLineHash"]
    column = fingerprints["primaryLocationStartColumnFingerprint"]
    rule = result["ruleId"]
    if not all(isinstance(value, str) and value for value in (rule, uri, line_hash, column)):
        raise ValueError("incomplete finding identity")
    return json.dumps([rule, uri, line_hash, column], separators=(",", ":"))


def inspect(directory: Path, baseline_path: Path | None = None,
            changed_files_path: Path | None = None) -> int:
    paths = sorted(directory.glob("*.sarif"))
    if not paths:
        print(f"CodeQL SARIF missing in {directory}", file=sys.stderr)
        return 2

    current: Counter[str] = Counter()
    details: dict[str, tuple[str, str, int, str]] = {}
    run_metadata: list[dict] = []
    try:
        for path in paths:
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("version") != "2.1.0":
                raise ValueError(f"{path}: unsupported SARIF version")
            runs = document["runs"]
            if not isinstance(runs, list) or not runs:
                raise ValueError(f"{path}: no analysis runs")
            for run in runs:
                if ((run.get("tool") or {}).get("driver") or {}).get("name") != "CodeQL":
                    raise ValueError(f"{path}: analysis is not from CodeQL")
                run_metadata.append(run)
                results = run["results"]
                if not isinstance(results, list):
                    raise ValueError(f"{path}: invalid results")
                for result in results:
                    if any(item.get("status") == "accepted" for item in result.get("suppressions", [])):
                        continue
                    key = finding_key(result)
                    location = result["locations"][0]["physicalLocation"]
                    details[key] = (
                        result["ruleId"],
                        location["artifactLocation"]["uri"],
                        int(location.get("region", {}).get("startLine") or 0),
                        str((result.get("message") or {}).get("text", "")),
                    )
                    current[key] += 1

        if baseline_path is None:
            baseline: Counter[str] = Counter()
        else:
            document = json.loads(baseline_path.read_text(encoding="utf-8"))
            if document.get("schema") != 1 or not isinstance(document.get("findings"), dict):
                raise ValueError(f"{baseline_path}: invalid baseline schema")
            if not isinstance(document.get("source_run"), str) or not document["source_run"]:
                raise ValueError(f"{baseline_path}: missing provenance")
            if any(type(count) is not int or count < 1 for count in document["findings"].values()):
                raise ValueError(f"{baseline_path}: invalid finding count")
            baseline = Counter(document["findings"])
            open_total = document.get("open_total")
            open_security = document.get("open_security")
            open_quality = document.get("open_quality")
            if not all(type(count) is int and count >= 0 for count in
                       (open_total, open_security, open_quality)):
                raise ValueError(f"{baseline_path}: missing finding totals")
            if sum(baseline.values()) != open_total or open_security + open_quality != open_total:
                raise ValueError(f"{baseline_path}: inconsistent finding totals")
            language = document.get("language")
            expected_queries = {
                "python": "codeql/python-queries",
                "javascript-typescript": "codeql/javascript-queries",
            }.get(language)
            artifact_count = document.get("artifact_count")
            query_count = document.get("query_count")
            extracted_count = document.get("extracted_count")
            baseline_extracted_paths = document.get("extracted_paths")
            required_rules = document.get("required_rules")
            if not expected_queries or not all(type(count) is int and count > 0 for count in
                                               (artifact_count, query_count, extracted_count)):
                raise ValueError(f"{baseline_path}: missing analysis coverage metadata")
            if (not isinstance(baseline_extracted_paths, list) or
                    len(baseline_extracted_paths) != extracted_count or
                    len(set(baseline_extracted_paths)) != extracted_count):
                raise ValueError(f"{baseline_path}: invalid extracted-source inventory")
            if not isinstance(required_rules, list) or not required_rules or any(
                not isinstance(rule, str) or not rule for rule in required_rules
            ):
                raise ValueError(f"{baseline_path}: missing required query identities")
            if len(run_metadata) != 1:
                raise ValueError("expected exactly one CodeQL analysis run per language")
            run = run_metadata[0]
            if not any(item.get("executionSuccessful") is True for item in run.get("invocations", [])):
                raise ValueError("CodeQL analysis invocation was not successful")
            if (run.get("automationDetails") or {}).get("id") != f"/language:{language}/":
                raise ValueError("CodeQL analysis language does not match baseline")
            queries = (run.get("properties") or {}).get("codeqlConfigSummary", {}).get("queries")
            if queries != [{"type": "builtinSuite", "uses": "security-and-quality"}]:
                raise ValueError("CodeQL security-and-quality suite was not used")
            if len(run.get("artifacts", [])) < artifact_count * 0.9:
                raise ValueError("CodeQL analyzed artifact coverage dropped unexpectedly")
            query_sets = [item for item in run["tool"].get("extensions", [])
                          if item.get("name") == expected_queries]
            if len(query_sets) != 1 or len(query_sets[0].get("rules", [])) < query_count:
                raise ValueError("CodeQL query coverage dropped unexpectedly")
            actual_rules = {rule.get("id") for rule in query_sets[0]["rules"]}
            if not set(required_rules).issubset(actual_rules):
                raise ValueError("CodeQL required query identities are missing")
            diagnostic = ("py" if language == "python" else "js") + "/diagnostics/successfully-extracted-files"
            extracted = {
                item["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
                for invocation in run["invocations"]
                for item in invocation.get("toolExecutionNotifications", [])
                if (item.get("descriptor") or {}).get("id") == diagnostic and item.get("locations")
            }
            if len(extracted) < extracted_count * 0.9:
                raise ValueError("CodeQL source extraction coverage dropped unexpectedly")
            if changed_files_path is not None:
                changed = {name.decode("utf-8").replace("\\", "/") for name in
                           changed_files_path.read_bytes().split(b"\0") if name}
                js_extensions = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
                                 ".html", ".yaml", ".yml")
                missing = sorted(
                    name for name in changed
                    if Path(name).exists() and (
                        name in baseline_extracted_paths or
                        (language == "python" and name.endswith(".py")) or
                        (language != "python" and (
                            name.endswith(js_extensions) or
                            Path(name).name in ("package.json", "manifest.json") or
                            Path(name).name.startswith("tsconfig") and name.endswith(".json")
                        ))
                    ) and name not in extracted
                )
                if missing:
                    raise ValueError(f"CodeQL did not extract changed source files: {missing[:10]}")
            if baseline and not current:
                raise ValueError("CodeQL reported zero findings against a nonempty baseline")
            # The all-zero case above is the obvious degraded run. The costly
            # case is PARTIAL degradation: the syntactic queries produce
            # results while the interprocedural dataflow ones silently produce
            # none, so the baseline looks populated and passes every check
            # here. That is what the committed baseline actually is -- it
            # records 0 for `py/clear-text-logging-sensitive-data`,
            # `py/path-injection`, `py/log-injection`, `py/partial-ssrf` and
            # nine other taint-tracking queries, all of which are in its own
            # `required_rules`, while an identical query configuration on the
            # same tree reports 1272 of them.
            #
            # Compared key-by-key that reads as 1272 NEW findings, and the
            # obvious next move -- refresh the baseline -- would silently
            # accept 1272 security findings, three of them SSRF at severity
            # 9.1. So detect it and refuse to judge instead: a rule the suite
            # was required to run, with nothing in the baseline and a lot in
            # this run, means the baseline is not a valid comparison basis.
            suspect = Counter()
            for key, count in current.items():
                rule = rule_of(key)
                if rule in set(required_rules or ()) and not any(
                    rule_of(k) == rule for k in baseline
                ):
                    suspect[rule] += count
            if len(suspect) >= 3 and sum(suspect.values()) > 100:
                worst = ", ".join(
                    f"{rule} ({count})" for rule, count in suspect.most_common(5)
                )
                raise ValueError(
                    f"baseline under-records {len(suspect)} required queries that this run "
                    f"reports {sum(suspect.values())} findings for ({worst}). The baseline was "
                    f"captured from a run whose dataflow analysis did not produce results, so it "
                    f"is not a valid comparison basis and these are NOT new findings. Do not "
                    f"refresh the baseline to clear this: that accepts every one of them as "
                    f"reviewed debt. Triage them first."
                )
    except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        print(f"CodeQL SARIF or baseline could not be verified: {exc}", file=sys.stderr)
        return 2

    current_exact, current_scoped = split_by_identity(current)
    baseline_exact, baseline_scoped = split_by_identity(baseline)

    # Exact-key rules: any key not seen before is a new finding.
    new = current_exact - baseline_exact
    absent = baseline_exact - current_exact

    # Graph-scoped rules: only a per-file INCREASE is a regression. Counter
    # subtraction drops non-positive values, so this is exactly "went up".
    scoped_regressions = current_scoped - baseline_scoped
    scoped_absent = baseline_scoped - current_scoped

    for key in list(new.elements())[:100]:
        rule, file, line, message = details[key]
        print(f"{file}:{line}: {rule}: {message[:300]}", file=sys.stderr)
    for (rule, file), delta in sorted(scoped_regressions.items()):
        was = baseline_scoped[(rule, file)]
        print(
            f"{file}: {rule}: {was + delta} findings, up {delta} from {was} in the baseline",
            file=sys.stderr,
        )
    failures = sum(new.values()) + sum(scoped_regressions.values())
    stale = sum(absent.values()) + sum(scoped_absent.values())
    summary = (
        f"CodeQL SARIF: {sum(current.values())} open, {sum(new.values())} new, "
        f"{sum(scoped_regressions.values())} graph-scoped increases, "
        f"{stale} absent from current SARIF against "
        f"{baseline_path if baseline_path else 'empty baseline'}"
    )
    print(summary)
    if sum(new.values()) > 100:
        print(f"... {sum(new.values()) - 100} more new findings", file=sys.stderr)
    if stale:
        # Not a failure -- see the module docstring. Loud, because a baseline
        # carrying findings that no longer exist will silently re-accept them
        # if the same defect returns at the same line hash.
        print(
            f"NOTE: {stale} baseline findings are absent from this run. The baseline is "
            f"stale by that much; refresh it with "
            f"scripts/youtab/regenerate_codeql_baseline.py so a fixed finding cannot be "
            f"silently re-accepted later."
        )
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as handle:
            handle.write(f"### CodeQL existing-debt regression check\n\n{summary}\n\n")
            handle.write("A passing check means no new findings against the reviewed baseline; it does not mean zero open findings.\n")
            if stale:
                handle.write(f"\n{stale} baseline findings are absent from this run — the baseline is stale and should be refreshed.\n")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3, 4):
        print("usage: codeql_sarif_gate.py SARIF_DIRECTORY [BASELINE_JSON] [CHANGED_FILES_NUL]", file=sys.stderr)
        raise SystemExit(2)
    baseline_file = Path(sys.argv[2]) if len(sys.argv) == 3 else None
    if len(sys.argv) == 4:
        baseline_file = Path(sys.argv[2])
    changed_files = Path(sys.argv[3]) if len(sys.argv) == 4 else None
    raise SystemExit(inspect(Path(sys.argv[1]), baseline_file, changed_files))
