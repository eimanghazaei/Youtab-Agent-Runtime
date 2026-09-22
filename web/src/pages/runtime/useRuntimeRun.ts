import { useCallback, useEffect, useRef, useState } from "react";

import { GatewayClient, type ConnectionState } from "@/lib/gatewayClient";
import {
  parseRuntimeErrorMessage,
  parseRuntimeEventText,
} from "@/pages/runtime/runtime-events";

/**
 * Real Agent Runtime run engine for the web `/runtime` surface.
 *
 * This drives the SAME authenticated same-origin JSON-RPC gateway
 * (`/api/ws`) the dashboard chat sidecar uses — `session.create`,
 * `prompt.submit`, streaming `message.*` events, and `session.interrupt`
 * for cancellation. No Electron IPC, no Node APIs: browser transport only.
 *
 * The workspace (`cwd`), model, and profile reported here are all
 * SERVER-DERIVED — read off the `session.create` response `info` block and
 * the gateway's `session.info` events. The client never mints workspace or
 * tenant identity; if the caller passes a `cwd`, the backend decides whether
 * to honour it (an unknown folder falls back to the gateway launch dir).
 */

export interface RuntimeRunInfo {
  /** Server-derived workspace directory for the active run. */
  cwd?: string;
  /** Server-resolved model label. */
  model?: string;
  provider?: string;
  branch?: string | null;
  profileName?: string;
}

export interface RuntimeRunOptions {
  /** Optional model override (per-session; never a global config write). */
  model?: string;
  provider?: string;
  /** Optional workspace hint — the backend validates and may fall back. */
  cwd?: string;
}

export type RuntimeRunPhase =
  | "idle"
  | "connecting"
  | "ready"
  | "streaming"
  | "cancelling"
  | "error";

interface CreateSessionResponse {
  session_id: string;
  info?: {
    model?: string;
    provider?: string;
    cwd?: string;
    branch?: string | null;
    profile_name?: string;
  };
}

export interface UseRuntimeRun {
  connectionState: ConnectionState;
  phase: RuntimeRunPhase;
  sessionId: string | null;
  info: RuntimeRunInfo;
  /** Streaming assistant output for the current turn. */
  output: string;
  error: string | null;
  /** Create a real Runtime run (idempotent while one is live). */
  start: (options?: RuntimeRunOptions) => Promise<void>;
  /** Submit a prompt to the live run; streams into `output`. */
  submit: (text: string) => Promise<void>;
  /** Cooperative cancellation that reaches the backend (session.interrupt). */
  cancel: () => Promise<void>;
}

export function useRuntimeRun(): UseRuntimeRun {
  const gwRef = useRef<GatewayClient | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const [connectionState, setConnectionState] =
    useState<ConnectionState>("idle");
  const [phase, setPhase] = useState<RuntimeRunPhase>("idle");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [info, setInfo] = useState<RuntimeRunInfo>({});
  const [output, setOutput] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const gw = new GatewayClient();
    gwRef.current = gw;

    const offState = gw.onState((s) => setConnectionState(s));

    const offStart = gw.on("message.start", () => {
      setOutput("");
      setPhase("streaming");
    });
    const offDelta = gw.on("message.delta", (ev) => {
      const chunk = parseRuntimeEventText(ev.payload);
      if (chunk) setOutput((prev) => prev + chunk);
    });
    const offComplete = gw.on("message.complete", (ev) => {
      const full = parseRuntimeEventText(ev.payload);
      if (full) setOutput(full);
      setPhase("ready");
    });
    const offInfo = gw.on("session.info", (ev) => {
      const p = (ev.payload ?? {}) as Record<string, unknown>;
      setInfo((prev) => ({
        ...prev,
        cwd: typeof p.cwd === "string" ? p.cwd : prev.cwd,
        model: typeof p.model === "string" ? p.model : prev.model,
        branch: typeof p.branch === "string" ? p.branch : prev.branch,
      }));
    });
    const offError = gw.on("error", (ev) => {
      setError(parseRuntimeErrorMessage(ev.payload));
      setPhase("error");
    });

    return () => {
      offState();
      offStart();
      offDelta();
      offComplete();
      offInfo();
      offError();
      gw.close();
      gwRef.current = null;
      sessionIdRef.current = null;
    };
  }, []);

  const start = useCallback(async (options?: RuntimeRunOptions) => {
    const gw = gwRef.current;
    if (!gw) return;
    if (sessionIdRef.current) return; // a run is already live
    setError(null);
    setPhase("connecting");
    try {
      await gw.connect();
      const params: Record<string, unknown> = {
        source: "web-runtime",
        close_on_disconnect: true,
      };
      if (options?.model) params.model = options.model;
      if (options?.provider) params.provider = options.provider;
      if (options?.cwd) params.cwd = options.cwd;
      const res = await gw.request<CreateSessionResponse>(
        "session.create",
        params,
      );
      sessionIdRef.current = res.session_id;
      setSessionId(res.session_id);
      setInfo({
        cwd: res.info?.cwd,
        model: res.info?.model,
        provider: res.info?.provider,
        branch: res.info?.branch ?? null,
        profileName: res.info?.profile_name,
      });
      setPhase("ready");
    } catch (e) {
      setError((e as Error).message);
      setPhase("error");
    }
  }, []);

  const submit = useCallback(async (text: string) => {
    const gw = gwRef.current;
    const sid = sessionIdRef.current;
    if (!gw || !sid || !text.trim()) return;
    setError(null);
    setOutput("");
    setPhase("streaming");
    try {
      await gw.request("prompt.submit", { session_id: sid, text });
    } catch (e) {
      setError((e as Error).message);
      setPhase("error");
    }
  }, []);

  const cancel = useCallback(async () => {
    const gw = gwRef.current;
    const sid = sessionIdRef.current;
    if (!gw || !sid) return;
    setPhase("cancelling");
    try {
      await gw.request("session.interrupt", { session_id: sid });
      setPhase("ready");
    } catch (e) {
      setError((e as Error).message);
      setPhase("error");
    }
  }, []);

  return {
    connectionState,
    phase,
    sessionId,
    info,
    output,
    error,
    start,
    submit,
    cancel,
  };
}
