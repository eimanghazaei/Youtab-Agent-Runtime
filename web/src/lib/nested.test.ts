import { describe, expect, it } from "vitest";

import { getNestedValue, setNestedValue } from "./nested";

describe("setNestedValue", () => {
  it("creates a nested own property without mutating the source", () => {
    const source = { safe: { prior: true } };
    expect(setNestedValue(source, "safe.next", 7)).toEqual({ safe: { prior: true, next: 7 } });
    expect(source).toEqual({ safe: { prior: true } });
  });

  it.each(["__proto__.polluted", "constructor.prototype.polluted", "safe.__proto__.polluted"])(
    "rejects prototype pollution through %s",
    (path) => {
      expect(() => setNestedValue({}, path, true)).toThrow("Unsafe config path");
      expect(Object.prototype).not.toHaveProperty("polluted");
    },
  );

  it("does not read inherited or reserved properties", () => {
    expect(getNestedValue({}, "__proto__.toString")).toBeUndefined();
    expect(getNestedValue({ safe: { value: 3 } }, "safe.value")).toBe(3);
  });
});
