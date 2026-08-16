import { createContext } from "react";
import type { ActionStatusResponse, GatewayLifecycleJob } from "@/lib/api";

export const SystemActionsContext = createContext<SystemActionsState | null>(
  null,
);

export type SystemAction = "restart" | "update";

export interface SystemActionsState {
  actionStatus: ActionStatusResponse | null;
  activeAction: SystemAction | null;
  dismissLog: () => void;
  /** Authoritative restart outcome; `null` when no restart is in flight and
   *  `state: "pending"` until the backend has actually observed the gateway. */
  gatewayJob: GatewayLifecycleJob | null;
  isBusy: boolean;
  isRunning: boolean;
  pendingAction: SystemAction | null;
  runAction: (action: SystemAction) => Promise<void>;
}
