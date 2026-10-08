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

A third thing matters as much as either comparison mode: both events must
measure the same tree the same way. CodeQL's diff-informed analysis is ON by
default for `pull_request` and clips DATAFLOW results to the diff, so the same
tree reported 7701 findings on a pull request and 8971 on a push to main.
`codeql.yml` pins `CODEQL_ACTION_DIFF_INFORMED_QUERIES: "false"` to stop that;
`tests/youtab_runtime/test_codeql_gate_wiring.py` fails if it is removed, and
the mismatch check in `inspect` is the runtime backstop.

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
import re
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


def graph_identities(message: str) -> list[str]:
    """The stable identity of each component of a graph-scoped message.

    ``py/cyclic-import`` reports ``Import of module [pkg.mod](1) begins an
    import cycle.`` and ``py/import-and-import-from`` reports ``Module
    'pkg.mod' is imported with both 'import' and 'import from'.`` -- in both
    the module name is the defect's identity, and it does not move when an
    unrelated line in the file does. The ``(1)`` is a SARIF relatedLocations
    index, so it is stripped: it is presentation, not identity.

    Verified stable: across a diff-informed pull-request run and a full push
    run of the same tree, `youtab_agent_cli/update_cmd.py` produced the
    identical eight module names with identical multiplicities while the line
    numbers moved.
    """
    return [
        stripped
        for line in message.split("\n")
        if (stripped := re.sub(r"\((\d+)\)", "", line).strip())
    ]


def finding_keys(result: dict) -> list[str]:
    """A finding's identities, by rule class. One result can yield several.

    Localized defects are keyed on ``primaryLocationLineHash``, which survives
    the file moving around them, and yield exactly one key.

    Graph-scoped rules cannot use that hash: the defect is a property of the
    module import graph but is reported once per participating import
    statement, so editing any module in a cycle re-reports every member at a
    shifted line and the hash changes for a defect that did not. They are
    keyed on the module name from the message instead.

    Those rules also need ONE KEY PER MESSAGE COMPONENT, not one per result.
    CodeQL coalesces diagnostics that land on the same location into a single
    result whose message is newline-joined -- 105 keys in the committed Python
    baseline are of that shape, e.g. one result on
    `agent/agent_runtime_helpers.py` carrying both `Import of module
    [providers] …` and `Import of module [youtab_agent_cli.providers] …`.
    Keyed on the joined string, fixing one of the two would change the
    identity from `A\nB` to `B`, and the survivor would be reported as NEW --
    reintroducing exactly the phantom churn this scheme exists to remove. Per
    component, that same fix is one `absent` and zero `new`.
    """
    location = result["locations"][0]["physicalLocation"]
    uri = location["artifactLocation"]["uri"]
    rule = result["ruleId"]
    if not isinstance(rule, str) or not rule or not isinstance(uri, str) or not uri:
        raise ValueError("incomplete finding identity")

    if rule in GRAPH_SCOPED_RULES:
        components = graph_identities(str((result.get("message") or {}).get("text", "")))
        if not components:
            raise ValueError(f"{rule}: graph-scoped finding carries no message to key on")
        return [json.dumps([rule, uri, c], separators=(",", ":")) for c in components]

    fingerprints = result["partialFingerprints"]
    line_hash = fingerprints["primaryLocationLineHash"]
    column = fingerprints["primaryLocationStartColumnFingerprint"]
    if not all(isinstance(value, str) and value for value in (line_hash, column)):
        raise ValueError("incomplete finding identity")
    return [json.dumps([rule, uri, line_hash, column], separators=(",", ":"))]


def finding_key(result: dict) -> str:
    """The single identity of a result that has exactly one.

    Kept for callers that build one key per result. Raises on a coalesced
    graph-scoped result rather than silently picking one component, because
    picking one is the bug `finding_keys` exists to avoid.
    """
    keys = finding_keys(result)
    if len(keys) != 1:
        raise ValueError(
            f"{result.get('ruleId')}: result carries {len(keys)} identities; "
            "use finding_keys()"
        )
    return keys[0]


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
                    location = result["locations"][0]["physicalLocation"]
                    for key in finding_keys(result):
                        details[key] = (
                            result["ruleId"],
                            location["artifactLocation"]["uri"],
                            int(location.get("region", {}).get("startLine") or 0),
                            str((result.get("message") or {}).get("text", "")),
                        )
                        current[key] += 1

        open_security = 0
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
            # EVERY invocation, not any. `any` accepted a run whose statuses
            # were [true, false] -- a partially failed analysis, whose missing
            # results then read as `absent` rather than as a coverage
            # problem. An empty list is also a failure: a run that records no
            # invocation at all has not demonstrated that it ran.
            invocations = run.get("invocations") or []
            if not invocations or not all(
                item.get("executionSuccessful") is True for item in invocations
            ):
                statuses = [item.get("executionSuccessful") for item in invocations]
                raise ValueError(
                    f"CodeQL analysis did not succeed in every invocation "
                    f"(executionSuccessful={statuses or 'none recorded'})"
                )
            if (run.get("automationDetails") or {}).get("id") != f"/language:{language}/":
                raise ValueError("CodeQL analysis language does not match baseline")
            queries = (run.get("properties") or {}).get("codeqlConfigSummary", {}).get("queries")
            if queries != [{"type": "builtinSuite", "uses": "security-and-quality"}]:
                raise ValueError("CodeQL security-and-quality suite was not used")
            # `artifacts` is NOT a coverage metric, and enforcing on it was a
            # false positive waiting for a CodeQL upgrade. It arrived:
            #
            #   baseline (bundle 2.2x)  artifacts 6735, extracted 2932
            #   this run (bundle 2.27.1) artifacts 2932, extracted 2932
            #
            # Zero source files were lost -- the extraction inventories are
            # identical set-for-set, and the run reported the same 110
            # findings -- but `artifacts` fell 56% and failed the 90% floor,
            # so `CodeQL analyze (javascript-typescript)` went red on a tree
            # with no regression in it. In 2.27.1 the index holds exactly the
            # extracted set; before, it also carried ~3803 entries that were
            # referenced but never extracted.
            #
            # That membership rule is a SARIF serialisation detail of the
            # CodeQL release, not a property of the analysis, so it cannot
            # carry a fail-closed gate across a version bump. The enforcing
            # coverage check is the extraction inventory below, which is the
            # defined quantity -- the files CodeQL reports it SUCCESSFULLY
            # EXTRACTED, via `*/diagnostics/successfully-extracted-files` --
            # and it is strictly the stronger of the two: the changed-files
            # check beneath it also requires every edited source file to
            # appear in that set, which no count of `artifacts` would catch.
            #
            # Kept as a reported signal rather than deleted, because a real
            # collapse here is still worth seeing in the log next to the
            # number that does gate -- and ONE case stays fatal: an index that
            # is entirely EMPTY against a baseline that recorded entries. That
            # is not a release listing a different set, it is a SARIF carrying
            # no artifact index at all, which no complete run produces. 2.27.1
            # still lists 2932 of them; the degenerate case is a malformed or
            # truncated artifact and must not be allowed to clear a baseline.
            artifact_now = len(run.get("artifacts", []))
            if artifact_count > 0 and artifact_now == 0:
                raise ValueError(
                    "CodeQL SARIF carries an empty `artifacts` index against a baseline "
                    f"recording {artifact_count}. A complete run always lists the files it "
                    "analyzed, so this is a truncated or malformed artifact rather than a "
                    "CodeQL version difference."
                )
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
            # THE enforcing coverage check. Same proportional shape the
            # `artifacts` check used to have, on a metric that means
            # something: a 10% drop in successfully-extracted source files is
            # a partial analysis however healthy the invocation claims to be.
            if len(extracted) < extracted_count * 0.9:
                raise ValueError(
                    f"CodeQL source extraction coverage dropped unexpectedly: "
                    f"{len(extracted)} files extracted against {extracted_count} in the "
                    f"baseline ({len(set(baseline_extracted_paths) - extracted)} baselined "
                    f"files absent). Below 90% this is a partial analysis, not a tree that "
                    f"shrank -- re-run before touching the baseline."
                )
            if artifact_now < extracted_count * 0.9:
                # Reported, never fatal -- see the note above the assignment.
                print(
                    f"NOTE: the SARIF `artifacts` index lists {artifact_now} entries "
                    f"against {artifact_count} in the baseline, while source extraction is "
                    f"intact at {len(extracted)}/{extracted_count}. The index's membership "
                    f"rule changes between CodeQL releases, so this is expected after a "
                    f"bundle bump and is not enforced."
                )
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
            # The all-zero case above is the obvious mismatch. The costly case
            # is PARTIAL: the syntactic queries are fully represented while
            # the interprocedural dataflow ones are not, so the baseline looks
            # populated and satisfies every check above.
            #
            # The mechanism is CodeQL's diff-informed analysis, not a broken
            # run. On `pull_request` the action defaults to restricting
            # DATAFLOW results to the lines the pull request touched, so the
            # same tree measures 7701 findings on a pull request and 8971 on a
            # push to main. A baseline captured under one event is not a valid
            # comparison basis for the other -- the committed one came from a
            # `pull_request` run and recorded 0 for
            # `py/clear-text-logging-sensitive-data`, `py/path-injection`,
            # `py/partial-ssrf` and ten other taint-tracking queries, all of
            # them in its own `required_rules`, while every push to main
            # measured 1272 of them.
            #
            # codeql.yml now pins `CODEQL_ACTION_DIFF_INFORMED_QUERIES:
            # "false"` so both events measure the same thing, which is what
            # makes this gate coherent at all. This check stays as the
            # backstop: if that setting is ever dropped, or a future baseline
            # is captured under the wrong event, the mismatch reappears, and
            # key-by-key it reads as 1272 NEW findings. The obvious next move
            # -- refresh the baseline -- would then accept 1272 security
            # findings as reviewed debt. So refuse to judge instead.
            # Both rule-coverage sets are built ONCE. Doing the baseline
            # membership test inline per current finding is quadratic -- 8971
            # keys reparsed for each of 8971 findings -- and that cost is paid
            # on every CodeQL job before any comparison happens.
            required_set = set(required_rules or ())
            baseline_rule_totals: Counter[str] = Counter()
            for key, count in baseline.items():
                baseline_rule_totals[rule_of(key)] += count
            current_rule_totals: Counter[str] = Counter()
            for key, count in current.items():
                current_rule_totals[rule_of(key)] += count

            # PER-RULE DEFICITS, not "the rule vanished entirely".
            #
            # Measuring only rules whose count reached ZERO left the largest
            # hole this check has had. A degraded analysis that retains even
            # ONE result per query has no fully-missing rule at all, so
            # nothing was suspect and every lost finding filed as
            # non-enforcing `absent`. Measured on the real baselines, a run
            # keeping one result per rule loses:
            #
            #     python                 9009 of 9076   (99.3%)
            #     javascript-typescript    87 of  110   (79.1%)
            #
            # and the gate returned 0 for both. Essentially the whole
            # analysis could evaporate and the security gate would pass.
            #
            # Comparing per-rule counts subsumes the old test -- a vanished
            # rule is just a deficit equal to its whole count -- and catches
            # partial loss, which is what a real degradation looks like.
            deficits_in_current: Counter[str] = Counter()      # baseline had more
            unrecorded_in_baseline: Counter[str] = Counter()   # baseline has NONE
            for rule in required_set:
                was = baseline_rule_totals.get(rule, 0)
                now = current_rule_totals.get(rule, 0)
                if was > now:
                    deficits_in_current[rule] = was - now
                elif now > was and was == 0:
                    # ONLY when the baseline recorded the query at ZERO.
                    #
                    # This direction used to take ANY positive delta as
                    # evidence that the baseline was captured under
                    # diff-informed analysis, and that made it fire on
                    # genuine regressions. Adding 13 `js/log-injection`
                    # findings to the 110-finding javascript baseline is 13
                    # >= max(10, 11), so a real new-finding regression came
                    # out of this branch as exit 2 "baseline under-records"
                    # -- a message that names rebuilding the baseline as a
                    # legitimate response. Rebuilding it would have accepted
                    # all 13 as reviewed debt. The gate would have talked the
                    # reader into laundering the exact thing it exists to
                    # catch.
                    #
                    # A count delta cannot distinguish the two cases, so this
                    # branch no longer tries. It now requires the structural
                    # fingerprint of diff-informed clipping instead: the
                    # baseline holding a required query at ZERO while this
                    # run reports it in volume. That is what the original bug
                    # actually looked like -- 13 taint-tracking queries, all
                    # in `required_rules`, all recorded as 0, against 1272
                    # measured on main. A query at zero is not a baseline
                    # that under-counts; it is a baseline that never saw the
                    # query run.
                    #
                    # Everything else -- more findings on a rule the baseline
                    # already populates -- is an ordinary new finding and
                    # falls through to the `new` comparison below, which
                    # prints each location and exits 1. Less precise in the
                    # ambiguous case, and that is the right trade: exit 1
                    # with locations is a safe diagnosis, exit 2 with
                    # "consider regenerating" is not.
                    unrecorded_in_baseline[rule] = now

            # The volume threshold is PROPORTIONAL, not absolute. An absolute
            # floor silently disables this check for whichever language has
            # the smaller result set: calibrated on Python's 1272 missing
            # dataflow findings, a `> 100` floor never fires for
            # javascript-typescript, whose entire baseline is 110 findings
            # with ~53 across 13 dataflow rules. A JS scan that lost all of
            # its dataflow output would therefore have scored a green gate --
            # the exact hole this check exists to close, reopened by the
            # constant.
            #
            # VOLUME ONLY -- there is deliberately no condition on the number
            # of rules. A `len(suspect) >= 3` floor was here to keep
            # remediating one whole rule from raising an alarm, and it opened
            # a hole big enough to drive the original bug through: losing just
            # `py/clear-text-logging-sensitive-data` (734) and
            # `py/path-injection` (279) is 1013 findings, over the
            # proportional floor of 908, but `len(suspect) == 2` so the check
            # never fired and all 1013 filed as non-enforcing `absent`. Two
            # queries are exactly how many a partial failure needs to take.
            #
            # The floor is 10% of the side the missing findings come from,
            # with an absolute floor of 10 so a tiny baseline cannot trip on
            # single digits.
            #
            # The cost is that a deliberate remediation large enough to cross
            # that floor now stops the gate too. That is the correct outcome,
            # not a false positive: remediating 10% of the baseline is exactly
            # when the baseline should be rebuilt, and the message below names
            # remediation as one of the two causes so the reader is not sent
            # hunting for a scanner fault that is not there.
            def coverage_breach(suspect: Counter[str], total: int) -> bool:
                return sum(suspect.values()) >= max(10, -(-total * 10 // 100))

            for label, quantity, suspect, total, explanation, remedy in (
                ("the baseline records NOTHING for",
                 "{n} required queries that this run reports {k} times",
                 unrecorded_in_baseline,
                 sum(current_rule_totals.values()),
                 "the baseline was captured under CodeQL's diff-informed analysis (the "
                 "pull_request default, which clips dataflow results to the diff) and is "
                 "being compared against a full analysis -- a required query sitting at "
                 "zero in the baseline while this run reports it in volume is that "
                 "signature, not a count that drifted",
                 "or the CodeQL release in use ADDED these queries after the baseline was "
                 "captured, so the baseline predates them -- the one case where rebuilding "
                 "it from a complete run is the right move"),
                ("THIS RUN under-reports",
                 "{n} required queries by {k} findings",
                 deficits_in_current,
                 sum(baseline_rule_totals.values()),
                 "this run lost the output of queries the baseline records, which is what a "
                 "partially failed analysis or a re-enabled diff-informed run looks like",
                 "or those queries really were remediated in full -- the one case where "
                 "rebuilding the baseline from a complete run is the right move"),
            ):
                if coverage_breach(suspect, total):
                    worst = ", ".join(
                        f"{rule} ({count})" for rule, count in suspect.most_common(5)
                    )
                    raise ValueError(
                        f"{label} "
                        f"{quantity.format(n=len(suspect), k=sum(suspect.values()))} "
                        f"({worst}). Two causes look "
                        f"identical from here and they need opposite responses. Either "
                        f"{explanation} -- check that codeql.yml still sets "
                        f"CODEQL_ACTION_DIFF_INFORMED_QUERIES=false and that every "
                        f"invocation completed -- {remedy}. Read the findings before you "
                        f"decide: a refresh accepts every one of them as reviewed debt."
                    )
    except (OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        print(f"CodeQL SARIF or baseline could not be verified: {exc}", file=sys.stderr)
        return 2

    # One comparison mode. `finding_keys` already gives graph-scoped rules an
    # identity that survives line churn (the module name from the message), so
    # they no longer need a per-file count ceiling -- and unlike that ceiling,
    # this still catches a REPLACEMENT: fixing one cycle in a file and
    # introducing a different one leaves the count unchanged but changes the
    # key, so it surfaces as new rather than passing silently.
    new = current - baseline
    absent = baseline - current

    for key in list(new.elements())[:100]:
        rule, file, line, message = details[key]
        print(f"{file}:{line}: {rule}: {message[:300]}", file=sys.stderr)
    failures = sum(new.values())
    stale = sum(absent.values())
    summary = (
        f"CodeQL SARIF: {sum(current.values())} open, {sum(new.values())} new, "
        f"{stale} absent from current SARIF against "
        f"{baseline_path if baseline_path else 'empty baseline'}"
    )
    print(summary)
    # Mandatory disclosure, printed on PASS as well as on failure.
    #
    # The baseline this gate passes against records 1373 open security
    # findings, 1272 of which entered it in one refresh because the previous
    # baseline had been captured from a run whose dataflow analysis produced
    # nothing. They are accepted debt, not reviewed debt, and a green check
    # here says only "no NEW findings" -- it does not say the tree is clean,
    # and nobody reading a green tick should have to go and discover that.
    if open_security:
        print(
            f"OPEN SECURITY DEBT: {open_security} security findings are recorded in this "
            f"baseline as accepted. Green means no new findings were added, NOT that the "
            f"tree is clean. This number must only ever go down."
        )
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
            if open_security:
                handle.write(
                    f"\n**{open_security} security findings are recorded in this baseline as "
                    f"accepted debt.** Green means no new findings were added, not that the "
                    f"tree is clean. This number must only ever go down.\n"
                )
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
