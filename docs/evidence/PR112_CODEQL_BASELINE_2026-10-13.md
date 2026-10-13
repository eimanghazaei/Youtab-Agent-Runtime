# PR #112 Python CodeQL baseline review, 2026-10-13

The baseline was refreshed from [CodeQL run 38419285897](https://github.com/eimanghazaei/Youtab-Agent-Runtime/actions/runs/38419285897), which analyzed PR head `32fda7051dc958102e6eacd8a2c01bc157098bd6` with the `security-and-quality` suite. The previous baseline was from run 35925065102. The PR base at `ced18415a8c06bd19409eebec988bd5e0f31f433` already failed the same gate with 1,468 new Python findings (run 36157829644).

The downloaded `python.sarif` contains 7,879 findings: 111 security and 7,768 quality. Relative to the old baseline, 241 were new and 51 absent. None of the 241 is located on a line added by PR #112. Of the new findings, 235 are quality findings in inherited code. All 6 security findings are in negative-case tests: five intentionally create permissive file modes, and one checks that a sanitized URL preserves the host string. They do not grant an unsafe permission or accept a URL in production code.

The baseline preserves every open finding and raises extraction coverage from 3,925 to 4,119 source paths and artifact coverage from 6,666 to 6,861. It does not suppress a rule or weaken `scripts/youtab/codeql_sarif_gate.py`. The local gate command against the downloaded SARIF returned `7,879 open, 0 new, 0 absent`. Future code changes still fail on new findings.

Dependency remediation in this PR separately upgrades the six packages identified by the current all-extras audit. `uv export --frozen --all-extras` followed by `scripts/youtab/cve_audit.py --no-deps --disable-pip` returned `PASS (0 triaged/suppressed, 0 blocking)`.

Rollback: revert the dependency and baseline commits together if the new package versions fail integration verification; retain the previous SARIF artifact and this review record for comparison.
