# Windows pilot Runtime artifact channel

This is a temporary public static delivery channel for approved Windows pilot
packages. It is **not live** until the `api.youtab.io` systemd Nginx vhost and
document root are read back, configured, and independently qualified. It does
not provide customer authorization; its high-entropy path only limits casual
discovery. The authenticated, IP-protected channel is deferred to Milestone 2.

The release operator chooses one fixed HTTPS base of the form
`https://api.youtab.io/pilot-runtime-<at-least-32-lowercase-hex>/releases`.
Keep the same value in the Nginx route, release command, and Setup build. Never
put a GitHub credential or customer secret in the installer or package.

From a clean checkout at the exact approved Runtime commit, with `$releaseBase`
set to that fixed base and `$stagingRoot` set to an offline directory outside
the checkout and outside the Nginx document root, prepare the static files
locally and build Setup:

```powershell
$sha = (git rev-parse HEAD).Trim()
$env:YOUTAB_AGENT_BUILD_PIN_COMMIT = $sha
$env:YOUTAB_AGENT_RELEASE_BASE_URL = $releaseBase
npm --prefix apps/bootstrap-installer run tauri:build
python scripts/release.py --package-runtime --artifact-output-dir $stagingRoot --artifact-version 0.19.1 --release-sequence 1 --artifact-base-url $releaseBase
```

The version and sequence above are examples; the approved release process must
set them explicitly. Qualify the built Setup and the staged package before
publishing. The command refuses dirty source, an output directory
inside the checkout, and an existing immutable SHA directory. It creates
`<stagingRoot>/<sha>/manifest.json`, `youtab-runtime-<sha>.zip`, and
`install-<sha>.ps1`. After qualification and separate publication authorization,
copy the immutable SHA directory to the verified Nginx document root first,
verify its hashes there, then atomically promote `latest.json` last. The next
sequence must increase. Never run the preparation command directly against a
live document root because it advances `latest.json` before release qualification.
The document root must be writable only by the authorized release process;
Nginx must deny dotfiles and must not expose temporary staging files. Do not
overwrite a published SHA directory or repoint latest to older content.

The ZIP and standalone script are checked against the manifest SHA256 values.
Setup executes its built-in script and installs the exact manifest SHA. It
stages the ZIP beside the current install, then verifies/builds at the final
path before retiring the backup. A legacy Git install without a release
sequence has no automatic ordering in this channel and requires a separately
qualified migration. A new Setup is needed when an old Setup's script protocol
cannot read a later manifest.

Publishing to or changing the actual Nginx host, switching traffic, and
handing an installer to a customer require separate authorization and live
qualification. This runbook does not assert that the route or clean-PC flow
currently passes.
