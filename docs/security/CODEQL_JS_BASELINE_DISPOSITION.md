# JavaScript CodeQL baseline disposition

Source: CodeQL `security-and-quality` SARIF from [run 35930698674](https://github.com/eimanghazaei/Youtab-Agent-Runtime/actions/runs/35930698674), product SHA `fe96df6a8448d1a08861b0d3c092e6c74048f720`. The exact scan reported four unsuppressed findings (three security tagged, one quality). The baseline records them as open; it does not say the repository has zero findings. New findings beyond these identities remain CI failures.

| Finding | Disposition and boundary |
| --- | --- |
| `js/http-to-file-access` in `website/scripts/prebuild.mjs`, fingerprint `d4f1a398438857df:1/26` | Accepted finding for the website build cache path. The fetch URL is a constant HTTPS origin, redirects are disabled, response time and bytes are bounded, and the destination is a fixed path derived from the script's own location with an atomic staged write. The remote JSON is validated before caching; extraction rejects unsafe install identifiers and source URLs, and the UI revalidates copyable commands. The CodeQL flow tracks response content into a file write; it does not show attacker control of the file path. Seven focused catalog tests pass. This finding does not qualify the enterprise Runtime installer. |
| `js/user-controlled-bypass` in `apps/desktop/scripts/diag-overlay-sweep.mjs`, fingerprint `7ecce93aca336d4e:1/4` | Retained for diagnostic-only CDP overlay measurement script. It is not bundled into the desktop app or invoked by its runtime. The reported conditional handles an absent render counter in an operator-run diagnostic session. |
| `js/user-controlled-bypass` in `apps/desktop/scripts/diag-overlay-full.mjs`, fingerprint `3d9d8840c79279d0:1/4` | Same diagnostic-only boundary as the sweep script; no production execution path. |
| `js/useless-assignment-to-property` in `apps/desktop/src/app/starmap/color.ts`, fingerprint `adb2bb3cc1295780:1/0` | The first canvas `fillStyle` assignment is an intentional fallback: the browser ignores an invalid CSS color assignment and retains the prior value. Removing it would change the fallback color. |

This disposition is scoped to the exact fingerprints above. A changed flow or newly reported identity must fail the no-new-findings gate and receive another review. Other Python CodeQL and dependency findings are outside this JavaScript disposition.
