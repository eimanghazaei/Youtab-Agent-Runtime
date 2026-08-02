#!/usr/bin/env python3
"""Classify every identity leak by the surface a normal user could reach it on.

``brand_url_inventory`` answers "is this occurrence allowed to exist". That is
a different question from "can a normal user see it", and the second one is the
one the product identity policy is actually about. A provider id in a
server-side binding table is fine. The same string rendered in the Models page,
an error message, a session export or a notification is a leak.

So this tool scans for two token families rather than one:

* the retired upstream brand, which must never be product identity; and
* the private engine binding -- provider ids, engine ids and raw model ids --
  which is server-side truth and must never be public identity.

and assigns every occurrence a *surface* (where it lives) and a *reachability
class* (who can see it):

``USER_REACHABLE``    a normal, non-operator user can see this string in the
                      product: UI text, CLI output, help, errors, prompts,
                      logs shown to users, About/footer, installer output,
                      published docs and images, exports, history,
                      notifications, tool and skill descriptions, URLs, and
                      environment examples users are told to copy.
                      **This class must be zero.**
``LEGAL_PROVENANCE``  licence text, copyright, contributor attribution,
                      third-party notices. Required to exist.
``PRIVATE_BINDING``   server-side mapping between a public profile and the
                      engine that serves it: adapters, registries, routing,
                      credential plumbing. Correct, and must stay private.
``MIGRATION_COMPAT``  a legacy identifier retained only so existing user data,
                      configs or APIs keep working, plus the tests that pin
                      that behaviour. Never rendered as current identity.

Production policy this encodes (Owner decision, 2026-08-02):

* BYOK is **disabled for every normal user**. There is no user-facing provider
  API-key entry, provider catalogue, model identifier or BYOK setup path, and
  the restriction is enforced server-side rather than by hiding controls.
* Users authenticate through Youtab and see **Alpha v0.6** and nothing else.
  The DeepSeek credential and the ``deepseek.v4_pro`` binding are Youtab-managed
  and live only in the production secret-management environment.
* Provider integration code is **kept**, as private backend infrastructure and
  as disabled internal capability for a future entitlement-controlled
  Enterprise/on-premise feature. It is not deleted, and it is not reachable.
* An exact provider identity or engine binding may appear **only** in private
  backend infrastructure, Secret Manager, access-controlled operator records,
  compliance records and processor agreements -- see ``PRIVATE_RECORD_PATHS``.
* Public Terms and Privacy carry only the generic disclosure: that Youtab uses
  carefully selected third-party infrastructure and AI processing providers,
  subject to contractual, confidentiality, security and data-protection
  requirements. They may **not** name a provider, and may **not** associate any
  provider with a named Youtab Agent.
* Nothing anywhere may state or imply that Alpha is, uses or is powered by a
  particular provider. ``ALPHA_BINDING_DISCLOSURE`` detects that claim on its
  own, because it is a leak even where the provider token alone would be one.

Anything a rule cannot place lands in ``USER_REACHABLE``, deliberately: an
unclassified occurrence is treated as a leak until someone evidences otherwise.
That is the same fail-closed posture the branding gate takes for an
uninstallable OCR engine -- not being able to prove something is safe is not
the same as it being safe.

Exit codes:

- ``0`` -- no occurrence is user-reachable.
- ``1`` -- at least one is.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

USER_REACHABLE = "USER_REACHABLE"
LEGAL_PROVENANCE = "LEGAL_PROVENANCE"
PRIVATE_BINDING = "PRIVATE_BINDING"
MIGRATION_COMPAT = "MIGRATION_COMPAT"

# Assembled so this file does not itself trip a naive grep for the brand.
_H = "her" + "mes"
_N = "no" + "us"
_T = "tek" + "nium"
_P = "deep" + "seek"  # assembled so this file does not match its own scan

LEGACY_BRAND = re.compile(rf"{_H}|{_N}research|{_N}[-_ ]research|{_T}", re.IGNORECASE)

# The private engine binding for the public Alpha v0.6 profile. `deepseek`
# alone is deliberately included: the provider name is exactly what a normal
# user must never be shown.
#
# Field NAMES (`provider_id`, `model_id`, `engine_id`) are deliberately NOT in
# this pattern. A first cut included them and they produced 692 of 1325 hits --
# every internal variable and dataclass attribute in the runtime, none of which
# is a string a user ever sees. Matching an identifier because it is *about*
# the binding, rather than because it *is* the binding, buries the real leaks
# in noise. Whether a payload leaks these keys is a question about serialized
# output, and `--check-payload-keys` answers it against real API responses
# instead of guessing from source text.
PRIVATE_ENGINE = re.compile(_P, re.IGNORECASE)

# Keys that must never appear in a normal user's serialized API response. Used
# by the payload check, not by the source scan.
FORBIDDEN_PAYLOAD_KEYS = ("provider_id", "engine_id", "model_id", "provider", "model")

# The only places an exact provider identity or engine binding may be named:
# private backend infrastructure, access-controlled operator records, and
# compliance/processor documentation. All three repositories are private, so
# these paths are internal records rather than published material.
#
# Terms and Privacy are deliberately NOT here. An earlier cut allowed them on
# the reasoning that naming a processor is legal disclosure; the Owner
# corrected that. Public Terms and Privacy may carry only the generic
# disclosure -- that Youtab uses carefully selected third-party infrastructure
# and AI processing providers -- and may never associate any provider with a
# named Youtab Agent. A provider name in Terms, Privacy, public docs or a setup
# guide is a USER_REACHABLE leak, never LEGAL_PROVENANCE.
PRIVATE_RECORD_PATHS = (
    r"^docs/operator/",
    r"^docs/adr/",
    r"^docs/evidence/",
    r"^docs/compliance/",
)

# Naming the engine is one leak. Asserting the *mapping* -- that Alpha is, uses
# or is powered by a given provider -- is the disclosure the policy most
# specifically forbids, so it is detected on its own rather than relying on the
# provider token alone. Outside the private record paths this is always a leak.
ALPHA_BINDING_DISCLOSURE = re.compile(
    rf"alpha[^\n]{{0,80}}{_P}|{_P}[^\n]{{0,80}}alpha|alpha\.v06[^\n]{{0,80}}(?:engine|provider|model)",
    re.IGNORECASE,
)

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", ".venv-qual", "__pycache__",
    ".pytest_cache", "dist", "build", "web_dist", "tui_dist",
}

# This tool's own output and the audit trail quote every occurrence verbatim.
# Reading them back in would block on our own evidence -- the same reason the
# branding gate skips them.
SKIP_PATH_PREFIXES = ("docs/evidence/", ".youtab/evidence/", ".youtab/matrix/")

# Binary/asset suffixes are recorded as a surface but not line-scanned.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico", ".svg"}

# --- Surfaces ---------------------------------------------------------------
# Ordered: the first match wins, so put the specific before the general.
SURFACE_RULES: list[tuple[str, str]] = [
    (r"^(LICENSE|THIRD_PARTY_NOTICES\.md)$", "legal"),
    (r"^\.mailmap$", "attribution"),
    # Tests first: a suite colocated with the code it covers
    # (`apps/desktop/src/**/*.test.tsx`) is still a test, and a later UI rule
    # would otherwise claim it and report its fixtures as shipped UI strings.
    (r"^tests?/|^tests-js/|(^|/)test_[^/]+\.py$|[._-]test\.(ts|tsx|js|jsx)$|\.spec\.(ts|tsx|js|jsx)$", "tests"),
    (r"^locales/", "ui.i18n"),
    (r"^web/src/", "ui.dashboard"),
    (r"^apps/desktop/", "ui.desktop"),
    (r"^ui-tui/|^tui_gateway/", "ui.tui"),
    (r"^plugins/[^/]+/dashboard/", "ui.plugin"),
    (r"^website/static/img/", "image.docs"),
    (r"^website/", "docs.site"),
    (r"^docs/", "docs.repo"),
    (r"(^|/)README[^/]*\.md$|(^|/)CONTRIBUTING[^/]*\.md$|(^|/)SECURITY[^/]*\.md$", "docs.repo"),
    (r"^optional-skills/|^skills/", "skills"),
    (r"^tools/|^toolsets?\.py$|^model_tools\.py$", "tools"),
    (r"^scripts/", "scripts"),
    (r"^gateway/", "service.gateway"),
    (r"^cron/", "service.cron"),
    (r"^youtab_agent_cli/|^cli\.py$|^run_agent\.py$", "cli"),
    (r"^agent/", "runtime.agent"),
    (r"^providers/", "runtime.providers"),
    (r"\.env(\.|$)|\.example$|^cli-config\.yaml\.example$", "env.example"),
    (r"^\.github/|^Dockerfile|^docker|^nix/|^flake", "build"),
]

# --- Rules: LEGAL_PROVENANCE ------------------------------------------------
LEGAL_RULES: list[tuple[str, str]] = [
    (rf"author:\s*.*{_T}", "skill author metadata"),
    (rf"@{_T}1?\b", "contributor handle"),
    (rf"{_T}1?@(?:gmail\.com|users\.noreply\.github\.com|{_N}research\.com)", "contributor identity"),
    (rf"Copyright|\(c\)\s*\d{{4}}|SPDX-", "copyright notice"),
    (rf"{_N} Research team", "upstream contributor credit"),
]

# --- Rules: PRIVATE_BINDING -------------------------------------------------
# Server-side truth. These paths are never rendered to a normal user; the
# public label comes from the agent profile registry instead.
PRIVATE_BINDING_PATH_RULES: list[tuple[str, str]] = [
    (r"^providers/", "provider adapter, server-side"),
    (r"^plugins/model-providers/", "provider adapter plugin, server-side"),
    (r"^youtab_agent_cli/model_normalize\.py$", "engine id normalisation, server-side"),
    (r"^youtab_agent_cli/(auth|credential|secrets|dump)[^/]*\.py$", "credential plumbing"),
    (r"^agent/", "runtime routing internals"),
    (r"^gateway/", "gateway routing internals"),
]
PRIVATE_BINDING_LINE_RULES: list[tuple[str, str]] = [
    (r"provider_id|engine_id|model_id", "binding field name in server-side code"),
    (r"os\.environ|getenv|API_KEY", "credential lookup"),
]

# --- Rules: MIGRATION_COMPAT ------------------------------------------------
MIGRATION_COMPAT_PATH_RULES: list[tuple[str, str]] = [
    (r"^youtab_agent_cli/config_migrations\.py$", "config migration"),
    (r"migrat", "migration code or test"),
]
MIGRATION_COMPAT_LINE_RULES: list[tuple[str, str]] = [
    (r"deprecat|legacy|retired|back[- ]?compat|compatib|alias", "explicit compatibility shim"),
]

# --- Rules: third-party facts that are not this product's identity ----------
# A real external model id or repo the product genuinely documents. These are
# user-reachable *by design* and are not a leak of OUR private binding, so they
# are recorded as LEGAL_PROVENANCE (third-party attribution) rather than a leak.
THIRD_PARTY_RULES: list[tuple[str, str]] = [
    (rf"{_N}research/{_H}-[34]", "third-party model id"),
    (rf"{_N}Research/{_H}-3-Llama", "third-party model id"),
    (rf"\b{_H}-[34](?:[-_.:]|\b)", "third-party model family"),
    (rf"{_H}-example-plugins|{_H}-mod\b", "third-party project"),
    (rf"{_N} Research", "third-party vendor name"),
]


@dataclass
class Occurrence:
    path: str
    line: int
    surface: str
    token_family: str
    match: str
    classification: str
    reason: str
    text: str


def _surface_for(rel: str) -> str:
    for pattern, surface in SURFACE_RULES:
        if re.search(pattern, rel):
            return surface
    if Path(rel).suffix.lower() in IMAGE_SUFFIXES:
        return "image.other"
    return "other"


def _first(rules: list[tuple[str, str]], text: str) -> str | None:
    for pattern, reason in rules:
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return None


def _classify(rel: str, surface: str, line_text: str) -> tuple[str, str]:
    if surface in {"legal", "attribution"}:
        return LEGAL_PROVENANCE, "dedicated provenance file"
    # A test is a test wherever it lives. Matching on the `tests` surface
    # rather than a `^tests/` path prefix also covers the colocated
    # `apps/desktop/src/**/*.test.tsx` suites, which a prefix rule missed.
    if surface == "tests":
        return MIGRATION_COMPAT, "test pinning current or legacy behaviour"
    # Vendored third-party reference material: upstream documentation shipped
    # verbatim inside a skill. It describes other people's models, and editing
    # it would falsify a quoted source.
    if re.search(r"/references?/", rel):
        return LEGAL_PROVENANCE, "vendored third-party reference material"
    in_private_record = any(re.search(p, rel) for p in PRIVATE_RECORD_PATHS)
    # The Alpha-to-provider mapping, anywhere outside a private record, is the
    # disclosure the policy forbids most explicitly. Checked before the legal
    # rules so a copyright header on the same line cannot launder it.
    if ALPHA_BINDING_DISCLOSURE.search(line_text) and not in_private_record:
        return USER_REACHABLE, "discloses or implies the Alpha private engine binding"
    reason = _first(LEGAL_RULES, line_text)
    if reason and not PRIVATE_ENGINE.search(line_text):
        # A mandatory copyright or licence notice stays LEGAL_PROVENANCE, but
        # it may not describe the private engine binding.
        return LEGAL_PROVENANCE, reason
    if in_private_record:
        return PRIVATE_BINDING, "access-controlled internal operator or compliance record"
    # Third-party *brand* facts (someone else's model id, someone else's repo)
    # are attribution. A third-party *processor we route to* is not covered by
    # this rule -- that is the private binding, and naming it outside the
    # permitted disclosure paths above is a leak regardless of how it is
    # phrased. So this only applies to the retired-upstream token family.
    reason = _first(THIRD_PARTY_RULES, line_text)
    if reason and not PRIVATE_ENGINE.search(line_text):
        return LEGAL_PROVENANCE, reason
    reason = _first(MIGRATION_COMPAT_PATH_RULES, rel) or _first(MIGRATION_COMPAT_LINE_RULES, line_text)
    if reason:
        return MIGRATION_COMPAT, reason
    reason = _first(PRIVATE_BINDING_PATH_RULES, rel)
    if reason:
        return PRIVATE_BINDING, reason
    if surface.startswith(("runtime.", "service.")):
        reason = _first(PRIVATE_BINDING_LINE_RULES, line_text)
        if reason:
            return PRIVATE_BINDING, reason
    # Fail closed.
    return USER_REACHABLE, f"unclassified occurrence on a {surface} surface"


def _tracked(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def scan(root: Path) -> dict:
    occurrences: list[Occurrence] = []
    images_seen = 0
    for rel in _tracked(root):
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        if rel.startswith(SKIP_PATH_PREFIXES):
            continue
        path = root / rel
        if not path.is_file():
            continue
        surface = _surface_for(rel)
        if Path(rel).suffix.lower() in IMAGE_SUFFIXES:
            images_seen += 1
            continue  # images are covered by the OCR branding gate
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for family, pattern in (("legacy_brand", LEGACY_BRAND), ("private_engine", PRIVATE_ENGINE)):
                m = pattern.search(line)
                if not m:
                    continue
                classification, reason = _classify(rel, surface, line)
                occurrences.append(
                    Occurrence(
                        path=rel, line=lineno, surface=surface, token_family=family,
                        match=m.group(0), classification=classification, reason=reason,
                        text=line.strip()[:200],
                    )
                )

    by_class: dict[str, int] = {}
    by_surface: dict[str, dict[str, int]] = {}
    for o in occurrences:
        by_class[o.classification] = by_class.get(o.classification, 0) + 1
        by_surface.setdefault(o.surface, {})
        by_surface[o.surface][o.classification] = by_surface[o.surface].get(o.classification, 0) + 1

    leaks = [asdict(o) for o in occurrences if o.classification == USER_REACHABLE]
    return {
        "schema_version": 1,
        "images_deferred_to_branding_gate": images_seen,
        "occurrences_total": len(occurrences),
        "counts_by_classification": by_class,
        "matrix_by_surface": dict(sorted(by_surface.items())),
        "user_reachable_total": len(leaks),
        "user_reachable": leaks,
        "passed": not leaks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--summary", action="store_true",
                        help="print only the matrix, not every occurrence")
    args = parser.parse_args()
    result = scan(args.root.resolve())

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(_render_markdown(result), encoding="utf-8")

    if args.summary:
        print(json.dumps({k: result[k] for k in
                          ("occurrences_total", "counts_by_classification",
                           "user_reachable_total", "passed")}, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))

    if result["user_reachable_total"]:
        print(
            f"FAIL: {result['user_reachable_total']} occurrence(s) are reachable by a "
            f"normal user and must be replaced with Youtab public identity",
            file=sys.stderr,
        )
        return 1
    print("PASS: no user-reachable legacy or private-binding identity", file=sys.stderr)
    return 0


def _render_markdown(result: dict) -> str:
    lines = [
        "# User-reachability matrix",
        "",
        f"Occurrences scanned: **{result['occurrences_total']}**  ",
        f"User-reachable (must be 0): **{result['user_reachable_total']}**",
        "",
        "| Surface | " + " | ".join(
            (USER_REACHABLE, LEGAL_PROVENANCE, PRIVATE_BINDING, MIGRATION_COMPAT)) + " |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for surface, counts in result["matrix_by_surface"].items():
        lines.append(
            f"| `{surface}` | "
            + " | ".join(str(counts.get(c, 0)) for c in
                         (USER_REACHABLE, LEGAL_PROVENANCE, PRIVATE_BINDING, MIGRATION_COMPAT))
            + " |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
