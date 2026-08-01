# NeMo Relay Shared Metrics

Youtab includes NeMo Relay as a normal runtime dependency on platforms for
which Relay publishes a native wheel. The shared-metrics integration is built
into Youtab and does not require `youtab plugins enable
observability/nemo_relay`. Youtab remains importable without Relay on other
native targets. Those targets use an explicit reduced-capability no-op host:
Youtab execution remains available, while Relay scopes, middleware, plugins,
and subscribers are unavailable. The `youtab-agent-runtime[nemo-relay]` extra remains
as a no-op compatibility alias for existing installation commands.

Youtab requires NeMo Relay 0.6.0 or later within the 0.6 release line. That
release establishes the lossless provider-codec contract used for Anthropic
Messages, OpenAI Chat Completions, and OpenAI Responses requests.

## Runtime Dependency and Data Boundary

Youtab installs the platform-specific `nemo-relay` native wheel from the
bounded `>=0.6.0,<0.7` dependency range. The published package is built from
the [NVIDIA NeMo Relay repository](https://github.com/NVIDIA/NeMo-Relay).
Unsupported platforms use the explicit no-op runtime described above rather
than downloading a different implementation.

When Relay managed execution is active, the provider request and response pass
through that native module in the Youtab process so configured interceptors can
operate on the real call. This is separate from the shared-metrics data
contract. Shared-metrics mode installs no network exporter and its subscriber
accepts only the versioned, allowlisted projection described below. Enabling a
separately configured rich-observability or dynamic plugin can create a
different data path and requires its own policy review.

Collection remains off unless Youtab policy enables it:

```yaml
telemetry:
  shared_metrics:
    enabled: true
```

This choice is read from the profile's own `config.yaml`. A machine-managed
configuration overlay cannot enable or disable shared metrics on the profile's
behalf.

The existing `observability/nemo_relay` plugin remains separate. Enable that
plugin only for its opt-in rich observability exporters, adaptive execution,
or dynamic Relay plugins.

Youtab core owns one Relay host and one isolated Relay session scope per Youtab
session. Core lifecycle producers use
`youtab_agent_cli.observability.relay_runtime` to obtain the shared session handle or
run Relay scope, LLM, tool, and mark APIs in that session context. New product
marks do not require Youtab plugin registration. Shared-metrics marks must
still contain only fields approved by the versioned allowlist; the hard
dependency does not change the collection or privacy policy.

## Current Slices

The current vertical slices record logical model calls and top-level task runs:

```text
Youtab turn, API, and tool hooks
  -> Relay session, task, and LLM lifecycle
  -> Youtab shared-metrics subscriber
  -> SQLite counters
  -> immutable JSON delta package
```

Youtab sends an empty `LLMRequest` into the metrics-owned lifecycle. This does
not describe the separate managed-execution call through the native runtime
documented above. The terminal metrics event contains only bounded model
family, provider family, locality, call role, and outcome values. Prompts,
responses, exact model IDs, endpoints, errors, session IDs, task IDs, and
request IDs are not included in the metrics event or package.

Each task run is a Relay `Function` scope named `youtab.task_run`, parented to
the owning Youtab session. The start counter contains only bounded execution
surface and entrypoint values. The terminal counter contains bounded outcome,
end reason, termination status, duration, logical model-call count, terminal
tool-call count, and provider-retry count buckets. Retries are additional
provider attempts for the same Youtab API request ID; they do not inflate the
logical model-call count. Tool calls are deduplicated by their Youtab tool-call
ID after a terminal tool result is observed. The outer `AIAgent` execution
boundary closes the task for normal returns, early returns, exceptions, and
cancellations. Active task ownership follows the task ID if Youtab rotates its
conversation session during context compression.

Local state is written under:

```text
$YOUTAB_AGENT_HOME/telemetry/shared_metrics/metrics.sqlite3
$YOUTAB_AGENT_HOME/telemetry/shared_metrics/outbox/*.json
```

The database keeps transactional aggregate and package-outbox state. Package
files are immutable delta documents that conform to a closed JSON schema and
are written with atomic replacement. Fully packaged aggregate rows and
successfully exported package rows and files are retained locally for 30 days.
Pending package rows and counters with unexported deltas are never pruned.

Each package contains an `install_id` generated as a random UUID. Despite the
schema field name, its current scope is one `YOUTAB_AGENT_HOME`, so it is more
precisely a persistent pseudonymous profile identifier. It is not derived from
hardware, account, host, path, or credential data. It remains stable across
packages from that profile and can therefore link those local packages.
Deleting `$YOUTAB_AGENT_HOME/telemetry/shared_metrics` resets the identifier together
with all aggregates and package files.

This slice has no remote-delivery path. A future remote exporter must not reuse
the persistent local identifier by default. It requires a separate product and
privacy decision covering consent, identity scope, rotation or keyed
pseudonymization, reset behavior, retention, and deletion.

## Smoke Test

Run a real Youtab CLI turn against the deterministic local model server:

```bash
./.venv/bin/python scripts/smoke_nemo_relay_shared_metrics.py
```

The script uses the installed `nemo-relay` dependency by default. Pass
`--relay-python ../nemo-relay/python` only when testing a locally built Relay
binding.

The smoke verifies the model request reached the local server, model and task
counters were stored, one package was exported, and prompt, response, and
exact-model canaries are absent from the package.
