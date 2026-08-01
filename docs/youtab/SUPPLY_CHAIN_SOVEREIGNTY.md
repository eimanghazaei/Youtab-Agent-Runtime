# Youtab Agent Runtime Supply-Chain Sovereignty

## Decision

Youtab owns a standalone private downstream. Upstream is an optional,
read-only source of candidate updates; it is never a build-time or runtime
availability dependency. Updates are mirrored first, quarantined, evaluated,
and admitted only after every Youtab gate passes. Nothing automatically merges
into the product branch.

## Source independence

- All upstream branches and tags are mirrored into dedicated Git namespaces.
- The `upstream` remote has a disabled push URL.
- A scheduled workflow copies exact refs into quarantined
  `upstream-mirror/*` branches and tags in the private Youtab origin.
- The mirror workflow never rebases or merges the Youtab product branch.
- A verified `git bundle --all` plus a ref manifest is the second, off-GitHub
  backup format.

The namespaces are intentionally separate from downstream branches:

```text
refs/youtab-upstream/heads/*
refs/youtab-upstream/tags/*
```

## Safe update flow

1. Mirror all upstream refs without changing the product worktree.
2. Record the exact upstream commit and ref delta.
3. Create an `intake/upstream-<date>-<sha>` candidate from an approved base.
4. Reapply registered Youtab patches; unresolved conflicts stop intake.
5. Regenerate locks, binary/image manifests, SBOM, checksums, and provenance.
6. Run feature-parity, security, performance, cache, streaming, branding, and
   deterministic regression gates.
7. Open a Draft PR. Human approval remains mandatory.
8. Build only from internal registries and mirrored assets.
9. Sign the image and provenance; admit the exact digest.
10. Deploy only after separate Owner authorization and rollback proof.

This gives Youtab updates without allowing upstream changes to overwrite
Youtab identity, public UI, Brain authority, patches, or production admission.

## Dependency independence

| Layer | Required Youtab control | Current implementation |
|---|---|---|
| Python | `uv.lock`, exported hash lock, internal index/wheelhouse, offline install | Lock/export/offline installer implemented; internal bytes pending infrastructure |
| npm | committed lockfiles with integrity, internal proxy/cache, `npm ci --offline` | Integrity gate and offline installer implemented; internal cache pending |
| Containers | digest pin, internal OCI copy, scan, sign, immutable admission | Digest manifest implemented for known images; Debian digest and internal copies pending |
| Binaries/assets | SHA-256 manifest, internal mirror, offline `COPY` | Known Docker assets registered; complete platform installer inventory pending |
| Runtime | no package/binary download or lazy install | Lazy dependency retrieval and self-update are denied by `YAR-PATCH-0001`; admitted image and egress proof remain pending |
| Evidence | source SBOM, runtime-image SBOM, checksums, signed provenance | Deterministic source evidence implemented; image SBOM/signing pending build infrastructure |
| Source backup | independent Git origin, exact upstream mirror, off-GitHub bundle | Private origin provisioned; complete quarantined mirror and durable verified off-GitHub bundle implemented |

## Public identity boundary

The runtime has no direct public browser ingress. `youtab-frontend` talks only
to `youtab-ai-os`, and `youtab-ai-os` controls the internal Runtime adapter.
Public artifacts must pass a case-insensitive branding gate that rejects
upstream product names. License, SBOM, copyright, and provenance retain the
truthful upstream identity required by law and supply-chain integrity.

Internal source code and legal provenance are not falsified or erased.
Provider/upstream identity is simply not exposed as the Youtab product name.

The separate Youtab frontend is not modified by upstream intake. End-to-end
branding admission still requires scanning the built `youtab-frontend` and
`youtab-ai-os` adapter artifacts via `--public-artifact`; source evidence from
this internal runtime alone is not proof of the public UI.

## Immutable runtime activation

Every admitted image or pod imports
`.youtab/supply-chain/runtime.env.template`. The Youtab policy takes precedence
over the upstream durable lazy-install target, blocks the built-in updater, and
requires a digest-pinned replacement image for upgrades. Package-manager
offline flags are defence in depth. Production network policy must separately
deny public package registries and installer hosts while allowing explicitly
authorized model/tool traffic.

## Release truth rule

`scripts/youtab/verify_supply_chain.py --mode source` proves only lock and
manifest integrity at source level. Production admission requires
`--mode release`, which fails closed unless it receives real files proving:

- internal OCI copy/signature;
- complete runtime image SBOM;
- signed provenance;
- verified off-GitHub Git bundle.

No document, environment variable name, or placeholder converts a missing
artifact into a pass.
