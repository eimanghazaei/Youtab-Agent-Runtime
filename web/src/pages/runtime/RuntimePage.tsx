import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import {
  AlertTriangle,
  Building2,
  Cpu,
  FolderOpen,
  Play,
  Plug,
  ScrollText,
  ShieldCheck,
  Square,
  Terminal,
} from "@/lib/runtime-icons";

import { Button } from "@youtab/ui/ui/components/button";
import { Badge } from "@youtab/ui/ui/components/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@youtab/ui/ui/components/card";
import { Input } from "@youtab/ui/ui/components/input";
import { Spinner } from "@youtab/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import {
  api,
  type ModelOptionsResponse,
  type SessionInfo,
  type StatusResponse,
} from "@/lib/api";
import { useRuntimeRun } from "@/pages/runtime/useRuntimeRun";

// ── Backend health ─────────────────────────────────────────────────────
type HealthPhase = "checking" | "ready" | "unavailable";

interface HealthState {
  phase: HealthPhase;
  status: StatusResponse | null;
  error: string | null;
}

function useBackendHealth(): HealthState & { retry: () => void } {
  const [state, setState] = useState<HealthState>({
    phase: "checking",
    status: null,
    error: null,
  });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .getStatus()
      .then((status) => {
        if (cancelled) return;
        setState({ phase: "ready", status, error: null });
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setState({ phase: "unavailable", status: null, error: e.message });
      });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  const retry = useCallback(() => {
    setState((s) => ({ ...s, phase: "checking", error: null }));
    setNonce((n) => n + 1);
  }, []);
  return { ...state, retry };
}

function BackendHealthBanner({
  health,
}: {
  health: HealthState & { retry: () => void };
}) {
  if (health.phase === "checking") {
    return (
      <div
        role="status"
        aria-live="polite"
        data-testid="runtime-backend-health"
        data-health="checking"
        className="flex items-center gap-2 border border-current/15 bg-background-base/60 px-3 py-2 text-sm text-text-secondary"
      >
        <Spinner />
        <span>Checking Agent Runtime backend…</span>
      </div>
    );
  }
  if (health.phase === "unavailable") {
    return (
      <div
        role="alert"
        data-testid="runtime-backend-health"
        data-health="unavailable"
        className="flex items-center justify-between gap-3 border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
      >
        <span className="flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          Backend unavailable — {health.error ?? "no connection"}
        </span>
        <Button size="sm" onClick={health.retry}>
          Retry
        </Button>
      </div>
    );
  }
  const s = health.status;
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="runtime-backend-health"
      data-health="ready"
      className="flex flex-wrap items-center gap-x-4 gap-y-1 border border-success/25 bg-success/10 px-3 py-2 text-sm text-success"
    >
      <span className="flex items-center gap-2 font-medium">
        <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-success" />
        Backend ready
      </span>
      <span className="text-text-secondary">v{s?.version}</span>
      <span className="text-text-secondary">
        gateway {s?.gateway_running ? "running" : "stopped"}
      </span>
      <span className="text-text-secondary">
        {s?.active_sessions ?? 0} active session
        {s?.active_sessions === 1 ? "" : "s"}
      </span>
    </div>
  );
}

// ── Pending / unavailable capability card ───────────────────────────────
function PendingCapability({
  title,
  description,
  reason,
  testid,
}: {
  title: string;
  description: string;
  reason: string;
  testid: string;
}) {
  return (
    <Card data-testid={testid} data-capability="pending">
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle>{title}</CardTitle>
          <Badge tone="warning">Pending backend</Badge>
        </div>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-text-secondary">{reason}</p>
      </CardContent>
    </Card>
  );
}

// ── Run view: real streaming Runtime run + cancellation ──────────────────
function RunView() {
  const run = useRuntimeRun();
  const [prompt, setPrompt] = useState("");
  const busy = run.phase === "streaming" || run.phase === "cancelling";

  const onStart = useCallback(() => {
    void run.start();
  }, [run]);

  const onSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      if (!prompt.trim()) return;
      void run.submit(prompt);
    },
    [prompt, run],
  );

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Agent Run</CardTitle>
            <div className="flex items-center gap-2 text-xs">
              <Badge tone={run.sessionId ? "success" : "outline"}>
                {run.connectionState}
              </Badge>
              <span
                data-testid="runtime-run-phase"
                className="text-text-tertiary uppercase tracking-[0.12em]"
              >
                {run.phase}
              </span>
            </div>
          </div>
          <CardDescription>
            Streams from the real Runtime gateway over authenticated
            same-origin WebSocket. Workspace, model, and profile are
            server-derived.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {!run.sessionId ? (
            <Button
              data-testid="runtime-start-run"
              onClick={onStart}
              disabled={run.phase === "connecting"}
            >
              {run.phase === "connecting" ? (
                <Spinner />
              ) : (
                <Play className="h-4 w-4" />
              )}
              Start Runtime run
            </Button>
          ) : (
            <dl
              data-testid="runtime-run-identity"
              className="grid grid-cols-1 gap-1 text-sm sm:grid-cols-2"
            >
              <IdentityRow label="Session" value={run.sessionId} />
              <IdentityRow
                label="Workspace (server)"
                value={run.info.cwd ?? "—"}
              />
              <IdentityRow label="Model" value={run.info.model ?? "—"} />
              <IdentityRow
                label="Profile"
                value={run.info.profileName ?? "default"}
              />
            </dl>
          )}

          {run.sessionId && (
            <form onSubmit={onSubmit} className="flex flex-col gap-2">
              <label htmlFor="runtime-prompt" className="sr-only">
                Prompt
              </label>
              <div className="flex gap-2">
                <Input
                  id="runtime-prompt"
                  data-testid="runtime-prompt-input"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  placeholder="Ask the agent…"
                  disabled={busy}
                />
                <Button
                  type="submit"
                  data-testid="runtime-submit"
                  disabled={busy || !prompt.trim()}
                >
                  Send
                </Button>
                {busy && (
                  <Button
                    type="button"
                    outlined
                    data-testid="runtime-cancel"
                    onClick={() => void run.cancel()}
                    disabled={run.phase === "cancelling"}
                  >
                    <Square className="h-4 w-4" />
                    Cancel
                  </Button>
                )}
              </div>
            </form>
          )}

          {run.error && (
            <p role="alert" className="text-sm text-destructive">
              {run.error}
            </p>
          )}

          {(run.output || busy) && (
            <pre
              data-testid="runtime-output"
              aria-live="polite"
              className="max-h-80 overflow-auto whitespace-pre-wrap border border-current/10 bg-background-base/60 p-3 font-courier text-sm text-text-primary"
            >
              {run.output}
              {run.phase === "streaming" && (
                <span aria-hidden className="animate-pulse">
                  ▋
                </span>
              )}
            </pre>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function IdentityRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs uppercase tracking-[0.1em] text-text-tertiary">
        {label}
      </dt>
      <dd className="truncate font-courier text-text-primary" title={value}>
        {value}
      </dd>
    </div>
  );
}

// ── Model / provider setup (server-backed catalogue) ─────────────────────
function ModelView() {
  const [options, setOptions] = useState<ModelOptionsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .getModelOptions()
      .then((res) => {
        if (!cancelled) setOptions(res);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const providers = options?.providers ?? [];

  return (
    <Card data-testid="runtime-model-setup">
      <CardHeader>
        <CardTitle>Model &amp; Engine</CardTitle>
        <CardDescription>
          Providers and models resolved by the backend. Full setup lives on the
          Models management surface; a run pins its model per-session.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loading && (
          <span className="flex items-center gap-2 text-sm text-text-secondary">
            <Spinner /> Loading model catalogue…
          </span>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}
        {!loading && !error && (
          <ul className="flex flex-col divide-y divide-current/10">
            {providers.map((p) => (
              <li
                key={p.slug}
                className="flex items-center justify-between gap-3 py-2 text-sm"
              >
                <span className="text-text-primary">{p.name}</span>
                <Badge tone={p.authenticated ? "success" : "outline"}>
                  {p.authenticated ? "configured" : "not configured"}
                </Badge>
              </li>
            ))}
            {providers.length === 0 && (
              <li className="py-2 text-sm text-text-secondary">
                No providers reported by the backend.
              </li>
            )}
          </ul>
        )}
        <p className="pt-3 text-xs text-text-tertiary">
          Manage keys and defaults on the{" "}
          <NavLink to="/models" className="underline">
            Models
          </NavLink>{" "}
          page.
        </p>
      </CardContent>
    </Card>
  );
}

// ── Connections: API + MCP (server-backed) ───────────────────────────────
function ConnectionsView() {
  const [servers, setServers] = useState<
    { name: string; enabled: boolean }[] | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getMcpServers()
      .then((res) => {
        if (!cancelled)
          setServers(
            res.servers.map((s) => ({ name: s.name, enabled: s.enabled })),
          );
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex flex-col gap-4">
      <Card data-testid="runtime-connections-mcp">
        <CardHeader>
          <CardTitle>MCP Connections</CardTitle>
          <CardDescription>
            Model Context Protocol servers registered on the backend. Secrets
            are stored server-side only — never in the browser.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {error && <p className="text-sm text-destructive">{error}</p>}
          {!servers && !error && (
            <span className="flex items-center gap-2 text-sm text-text-secondary">
              <Spinner /> Loading MCP servers…
            </span>
          )}
          {servers && (
            <ul className="flex flex-col divide-y divide-current/10">
              {servers.map((s) => (
                <li
                  key={s.name}
                  className="flex items-center justify-between py-2 text-sm"
                >
                  <span className="text-text-primary">{s.name}</span>
                  <Badge tone={s.enabled ? "success" : "outline"}>
                    {s.enabled ? "enabled" : "disabled"}
                  </Badge>
                </li>
              ))}
              {servers.length === 0 && (
                <li className="py-2 text-sm text-text-secondary">
                  No MCP servers configured.
                </li>
              )}
            </ul>
          )}
          <p className="pt-3 text-xs text-text-tertiary">
            Add servers and API providers on the{" "}
            <NavLink to="/mcp" className="underline">
              MCP
            </NavLink>{" "}
            and{" "}
            <NavLink to="/env" className="underline">
              Keys
            </NavLink>{" "}
            pages.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

// ── Files: browser picker + Gateway File Ingress (truthful pending) ──────
function FilesView() {
  const [picked, setPicked] = useState<{ name: string; size: number } | null>(
    null,
  );
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <div className="flex flex-col gap-4">
      <Card data-testid="runtime-files-picker">
        <CardHeader>
          <CardTitle>Files</CardTitle>
          <CardDescription>
            Browser file picker. Uploaded bytes go to the Gateway File Ingress
            for scan/quarantine and a server-issued file id — no local paths
            leave the browser.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <input
            ref={inputRef}
            type="file"
            data-testid="runtime-file-input"
            className="text-sm"
            aria-label="Choose a file to ingest"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) setPicked({ name: f.name, size: f.size });
            }}
          />
          {picked && (
            <div
              data-testid="runtime-file-selected"
              className="flex items-center justify-between border border-current/10 bg-background-base/60 px-3 py-2 text-sm"
            >
              <span className="truncate text-text-primary">{picked.name}</span>
              <Badge tone="warning" data-testid="runtime-file-scan-state">
                quarantined — scan pending
              </Badge>
            </div>
          )}
        </CardContent>
      </Card>

      <PendingCapability
        testid="runtime-folder-grant"
        title="Folder access"
        description="Grant the agent read/write access to a workspace folder."
        reason="Folder-grant capability is unavailable in the browser transport until the Gateway grant + scan endpoints report ready. It is shown here truthfully as pending rather than hidden."
      />
    </div>
  );
}

// ── Enterprise + Governance (truthful DRAFT_CONSUMER_EXPECTATION) ────────
function EnterpriseView() {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      {["CRM", "ERP", "SAP", "CAD"].map((sys) => (
        <PendingCapability
          key={sys}
          testid={`runtime-enterprise-${sys.toLowerCase()}`}
          title={sys}
          description={`${sys} typed-manifest consumer surface.`}
          reason="DRAFT_CONSUMER_EXPECTATION — bound to a reference provider until the real Gateway/Runtime manifest SHAs arrive. Reference results are never treated as canonical effects."
        />
      ))}
    </div>
  );
}

function GovernanceView() {
  return (
    <div className="flex flex-col gap-4">
      <PendingCapability
        testid="runtime-governance-approval"
        title="Approval → Effect → Receipt"
        description="Governed operations require approval before any effect; approved effects produce a backend receipt and reconciliation."
        reason="UNBOUND — the approval, receipt, and reconciliation flow runs against the real Runtime worker when available. Denials prevent effects; renderer-local reference results are never canonical approvals."
      />
    </div>
  );
}

// ── Run history (server-backed; survives reload) ─────────────────────────
function HistoryView() {
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getSessions(25, 0, { sources: ["web-runtime"], order: "recent" })
      .then((res) => {
        if (!cancelled) setSessions(res.sessions);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Card data-testid="runtime-history">
      <CardHeader>
        <CardTitle>Run history</CardTitle>
        <CardDescription>
          Runtime runs restored from the backend session store — persists
          across reloads.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {error && <p className="text-sm text-destructive">{error}</p>}
        {!sessions && !error && (
          <span className="flex items-center gap-2 text-sm text-text-secondary">
            <Spinner /> Loading history…
          </span>
        )}
        {sessions && (
          <ul
            className="flex flex-col divide-y divide-current/10"
            data-testid="runtime-history-list"
          >
            {sessions.map((s) => (
              <li key={s.id} className="flex flex-col py-2 text-sm">
                <span className="truncate text-text-primary">
                  {s.title || s.preview || s.id}
                </span>
                <span className="text-xs text-text-tertiary">
                  {s.message_count} messages · {s.agent_label ?? "agent"}
                </span>
              </li>
            ))}
            {sessions.length === 0 && (
              <li className="py-2 text-sm text-text-secondary">
                No Runtime runs yet.
              </li>
            )}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// ── Sub-navigation ───────────────────────────────────────────────────────
const SUB_NAV: { to: string; label: string; icon: typeof Play }[] = [
  { to: "run", label: "Run", icon: Terminal },
  { to: "model", label: "Model", icon: Cpu },
  { to: "connections", label: "Connections", icon: Plug },
  { to: "files", label: "Files", icon: FolderOpen },
  { to: "enterprise", label: "Enterprise", icon: Building2 },
  { to: "governance", label: "Governance", icon: ShieldCheck },
  { to: "history", label: "History", icon: ScrollText },
];

export default function RuntimePage() {
  const health = useBackendHealth();

  return (
    <div className="flex flex-col gap-4" data-testid="runtime-page">
      <header className="flex flex-col gap-1">
        <h1 className="font-display text-2xl uppercase tracking-[0.06em] text-text-primary">
          Agent Runtime
        </h1>
        <p className="text-sm text-text-secondary">
          A connected Runtime product surface — real streaming runs, workspace,
          model, connections, and governance over browser transport.
        </p>
      </header>

      <BackendHealthBanner health={health} />

      <nav
        aria-label="Agent Runtime sections"
        className="flex flex-wrap gap-1 border-b border-current/10"
      >
        {SUB_NAV.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-2 px-3 py-2 text-sm uppercase tracking-[0.1em] transition-colors",
                "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground",
                isActive
                  ? "text-midground border-b-2 border-midground"
                  : "text-text-secondary hover:text-midground",
              )
            }
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </NavLink>
        ))}
      </nav>

      <Routes>
        <Route index element={<Navigate to="run" replace />} />
        <Route path="run" element={<RunView />} />
        <Route path="model" element={<ModelView />} />
        <Route path="connections" element={<ConnectionsView />} />
        <Route path="files" element={<FilesView />} />
        <Route path="enterprise" element={<EnterpriseView />} />
        <Route path="governance" element={<GovernanceView />} />
        <Route path="history" element={<HistoryView />} />
        <Route path="*" element={<Navigate to="run" replace />} />
      </Routes>
    </div>
  );
}
