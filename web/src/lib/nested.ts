export function getNestedValue(obj: Record<string, unknown>, path: string): unknown {
  const parts = path.split(".");
  let cur: unknown = obj;
  for (const p of parts) {
    if (cur == null || typeof cur !== "object") return undefined;
    if (p === "__proto__" || p === "prototype" || p === "constructor") return undefined;
    if (!Object.prototype.hasOwnProperty.call(cur, p)) return undefined;
    cur = (cur as Record<string, unknown>)[p];
  }
  return cur;
}

export function setNestedValue(obj: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> {
  const clone = structuredClone(obj);
  const parts = path.split(".");
  let cur: Record<string, unknown> = clone;
  for (let i = 0; i < parts.length - 1; i++) {
    const part = parts[i];
    if (part === "__proto__" || part === "prototype" || part === "constructor") {
      throw new Error("Unsafe config path");
    }
    if (
      !Object.prototype.hasOwnProperty.call(cur, part) ||
      cur[part] == null ||
      typeof cur[part] !== "object"
    ) {
      cur[part] = {};
    }
    cur = cur[part] as Record<string, unknown>;
  }
  const leaf = parts[parts.length - 1];
  if (leaf === "__proto__" || leaf === "prototype" || leaf === "constructor") {
    throw new Error("Unsafe config path");
  }
  cur[leaf] = value;
  return clone;
}
