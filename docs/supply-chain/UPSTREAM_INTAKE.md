# Upstream intake and product isolation

External source updates never flow directly into a product branch, release, runtime, VPS or production environment.

1. Materialize the candidate on disk outside the product Git history.
2. Record the source identifier, file manifest and SHA-256 checksums.
3. Place the candidate in quarantine and compare it with the current Youtab source.
4. Apply selected changes as Youtab-owned patches; do not import external refs, branches, tags, workflows or product identity.
5. Run unit, integration, E2E, smoke, OWASP, SAST, secret, dependency and branding gates.
6. Open a Youtab draft pull request with evidence.
7. Require human approval for merge and release. Deployment is a separate authorization.

The only repository location permitted to state third-party legal provenance is `THIRD_PARTY_NOTICES.md` (and license material when legally required). Product files, package identifiers, CLI, UI, paths, environment variables, docs, assets and workflows use Youtab identity.
