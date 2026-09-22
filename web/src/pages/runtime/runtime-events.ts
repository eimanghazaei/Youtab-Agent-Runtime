/**
 * Pure helpers for interpreting Runtime gateway event payloads.
 *
 * Kept transport-free (no React, no WebSocket) so the streaming-text
 * extraction and error-message resolution can be unit-tested directly,
 * matching the web workspace's pure-logic test convention.
 */

/** Extract streamed assistant text from a `message.*` event payload. */
export function parseRuntimeEventText(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "";
  const p = payload as Record<string, unknown>;
  const raw = p.text ?? p.delta ?? p.content ?? "";
  return typeof raw === "string" ? raw : "";
}

/** Resolve a human-readable message from an `error` event payload. */
export function parseRuntimeErrorMessage(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "runtime error";
  const p = payload as Record<string, unknown>;
  return typeof p.message === "string" && p.message ? p.message : "runtime error";
}
