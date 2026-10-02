# Windows pilot UI and native model qualification

This candidate follows the first published Runtime source
`5b4ff500fcff5f34f67cd37432f77680c0cbe1aa` (version 0.19.1, sequence 1).
It is not a claim of clean-PC readiness.

## Changes to qualify

1. Installer light/dark text and progress accents are green; the active stage
   uses the existing Simorgh working SVG with reduced-motion support.
2. Native Youtab recommendations use the current profile's admitted Gateway
   catalog. Unsupported Anthropic Messages models remain rejected. Hosted
   Portal recommendations retain their separate tier behavior.
3. Approval displays the description and complete command in a prominent card.
   The request remains session-bound; Run, Reject, shortcuts and permanent
   approval confirmation retain their existing authority checks.
4. Clarification questions have a larger bordered card and larger controls.
5. CLI hero art depicts the Owner-provided Persepolis double-bull capital as
   terminal Braille, without adding a runtime image dependency.
6. The home wordmark uses the bundled Racing Sans One font. Pinned and Sessions
   headings use pin and desktop icons. Keyboard shortcuts remain available in
   Settings while their titlebar icon is hidden.
7. Approval mode and terminal controls are visible by default. A one-time
   migration updates old pilot visibility preferences; subsequent user choices
   to hide either control persist.
8. About uses the Simorgh logo. Dutch and Persian are available in the existing
   language picker. Persian text reads RTL while shell/menu geometry stays LTR.
   Common controls, navigation, Appearance, About, approval and questions are
   translated; specialist untranslated strings use the existing English fallback.

## Local checks

- Desktop: `npm --prefix apps/desktop run typecheck` and `npm --prefix apps/desktop run build`.
- Installer: `npm --prefix apps/bootstrap-installer run build`.
- Focused Desktop UI tests cover onboarding, approval, clarification, languages,
  runtime readiness and statusbar visibility/persistence.
- Python tests run through `scripts/run_tests.sh --file-retries 0` with isolated
  homes, including native recommendation and profile integration regressions.
- OWASP smoke, SAST, secrets, dependency, router policy, container isolation,
  branding with OCR, compileall, required Ruff and offline lock checks.
- Browser component smoke covers English/Dutch/Persian, actual font loading,
  full-command visibility, no automatic approval and explicit rejection.
- Built installer browser smoke covers progress, success, failure, SVG tint
  and reduced-motion behavior using synthetic native events.

The broad Windows test run is not green. Interrupted local full-suite runs and
worker startup timeouts must be reported separately from focused passing tests.
Require exact-head remote CI before merge; do not waive required protection.
Browser mocks do not prove native inference, native install or real update.

## Real PC update acceptance

After exact-head review and Owner merge/publication authorization:

1. Build from the approved new main SHA and private, verified release base.
2. Publish a new immutable SHA directory and an approved version/sequence above
   the live sequence. Verify downloads before atomically promoting `latest.json`.
   Never overwrite the original release directory or reuse provisional metadata.
3. On the existing PC, preserve the working configuration and chats. Record
   the installed SHA/version/sequence, then use About → Check now → Update now.
4. Verify the update is discovered, installation finishes, the app restarts,
   and installed metadata identifies the new SHA/sequence.
5. Confirm chat/config preservation, native Youtab sign-in followed by a real
   Gateway DeepSeek response, restart persistence, approval denial and each UI
   change. Confirm the working direct-API path still functions.
6. Separately exercise repair and controlled rollback using isolated synthetic
   test data. Do not deliberately damage the Owner's working PC installation.

The existing Setup executable does not self-update. Runtime/Desktop update
acceptance and the new installer UI are distinct checks: inspect the rebuilt
Setup separately and qualify its pinned source and release base. Do not claim
that an older staged Setup has acquired the new installer visuals.

Mask private release prefixes, credentials, OAuth callback codes/state and
customer data in logs, screenshots and reports. Record actual observations,
including failures; never substitute unit mocks for PC acceptance.
