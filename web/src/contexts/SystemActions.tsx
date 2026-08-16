import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { ActionStatusResponse, GatewayLifecycleJob } from "@/lib/api";
import { Toast } from "@youtab/ui/ui/components/toast";
import { useI18n } from "@/i18n";
import {
  SystemActionsContext,
  type SystemAction,
} from "./system-actions-context";

const ACTION_NAMES: Record<SystemAction, string> = {
  restart: "gateway-restart",
  update: "youtab-update",
};

export function SystemActionsProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [pendingAction, setPendingAction] = useState<SystemAction | null>(null);
  const [activeAction, setActiveAction] = useState<SystemAction | null>(null);
  const [actionStatus, setActionStatus] = useState<ActionStatusResponse | null>(
    null,
  );
  const [toast, setToast] = useState<ToastState | null>(null);
  // The gateway restart's verdict comes from its job, never from the POST that
  // started it. `null` while no restart is in flight.
  const [gatewayJob, setGatewayJob] = useState<GatewayLifecycleJob | null>(null);
  const [gatewayJobId, setGatewayJobId] = useState<string | null>(null);
  const { t } = useI18n();

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(timer);
  }, [toast]);

  // Log tail only. This drives the output panel and nothing else -- in
  // particular it no longer decides success, because a child's exit status is
  // only half of the question for a lifecycle operation.
  useEffect(() => {
    if (!activeAction) return;
    const name = ACTION_NAMES[activeAction];
    let cancelled = false;

    const poll = async () => {
      try {
        const resp = await api.getActionStatus(name);
        if (cancelled) return;
        setActionStatus(resp);
        // For the gateway restart, the job decides when we stop polling; the
        // child can be gone well before the gateway is back.
        if (!resp.running && activeAction !== "restart") {
          const ok = resp.exit_code === 0;
          setToast({
            type: ok ? "success" : "error",
            message: ok
              ? t.status.actionFinished
              : `${t.status.actionFailed} (exit ${resp.exit_code ?? "?"})`,
          });
          return;
        }
      } catch {
        // transient fetch error; keep polling
      }
      if (!cancelled) setTimeout(poll, 1500);
    };

    poll();
    return () => {
      cancelled = true;
    };
  }, [activeAction, t.status.actionFinished, t.status.actionFailed]);

  // The authoritative restart verdict. Polls until the backend reports a
  // terminal state, then reports exactly what the backend concluded -- a
  // failed child is never rendered as a success.
  useEffect(() => {
    if (!gatewayJobId) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const job = await api.getGatewayJob(gatewayJobId);
        if (cancelled) return;
        setGatewayJob(job);
        if (job.state !== "pending") {
          setToast({
            type: job.state === "succeeded" ? "success" : "error",
            message:
              job.state === "succeeded"
                ? t.status.actionFinished
                : `${t.status.actionFailed} (${job.reason ?? "unknown"}${
                    job.exit_code != null ? `, exit ${job.exit_code}` : ""
                  })`,
          });
          return;
        }
      } catch {
        // transient fetch error; keep polling
      }
      if (!cancelled) setTimeout(poll, 1500);
    };

    poll();
    return () => {
      cancelled = true;
    };
  }, [gatewayJobId, t.status.actionFinished, t.status.actionFailed]);

  const runAction = useCallback(
    async (action: SystemAction) => {
      setPendingAction(action);
      setActionStatus(null);
      try {
        if (action === "restart") {
          setGatewayJob(null);
          setGatewayJobId(null);
          // 202 Accepted: this only says the operation was admitted. The
          // result comes from polling the job it returned.
          const accepted = await api.restartGateway();
          setGatewayJobId(accepted.job_id);
          setGatewayJob(accepted);
          setActiveAction(action);
        } else {
          const resp = await api.updateYoutab();
          // Some installs cannot apply updates from inside the dashboard. The
          // endpoint returns a structured {ok:false, message, update_command}
          // envelope instead of spawning the action; surface that guidance
          // rather than polling a synthetic failed action.
          if (!resp.ok) {
            const cmd = resp.update_command ? `  ${resp.update_command}` : "";
            setToast({
              type: "success",
              message:
                (resp.message ??
                  "Updates don't apply from this dashboard.") +
                cmd,
            });
            return;
          }
          setActiveAction(action);
        }
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        setToast({
          type: "error",
          message: `${t.status.actionFailed}: ${detail}`,
        });
      } finally {
        setPendingAction(null);
      }
    },
    [t.status.actionFailed],
  );

  const dismissLog = useCallback(() => {
    setActiveAction(null);
    setActionStatus(null);
    setGatewayJob(null);
    setGatewayJobId(null);
  }, []);

  // A restart is "running" for as long as its *job* is pending. Deriving this
  // from the child's liveness would clear the spinner while the gateway was
  // still down, which is the moment the old UI declared victory.
  const isRunning =
    activeAction === "restart"
      ? gatewayJob === null || gatewayJob.state === "pending"
      : activeAction !== null && actionStatus?.running !== false;
  const isBusy = pendingAction !== null || isRunning;

  return (
    <SystemActionsContext.Provider
      value={{
        actionStatus,
        activeAction,
        dismissLog,
        gatewayJob,
        isBusy,
        isRunning,
        pendingAction,
        runAction,
      }}
    >
      {children}
      <Toast toast={toast} />
    </SystemActionsContext.Provider>
  );
}

interface ToastState {
  message: string;
  type: "success" | "error";
}
