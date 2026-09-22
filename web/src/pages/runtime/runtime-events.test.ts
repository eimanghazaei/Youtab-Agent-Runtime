import { describe, expect, it } from "vitest";

import {
  parseRuntimeErrorMessage,
  parseRuntimeEventText,
} from "@/pages/runtime/runtime-events";

describe("parseRuntimeEventText", () => {
  it("reads the `text` field of a delta payload", () => {
    expect(parseRuntimeEventText({ text: "hello" })).toBe("hello");
  });

  it("falls back to `delta` then `content`", () => {
    expect(parseRuntimeEventText({ delta: "d" })).toBe("d");
    expect(parseRuntimeEventText({ content: "c" })).toBe("c");
  });

  it("returns empty string for missing / non-string / non-object payloads", () => {
    expect(parseRuntimeEventText(null)).toBe("");
    expect(parseRuntimeEventText(undefined)).toBe("");
    expect(parseRuntimeEventText("nope")).toBe("");
    expect(parseRuntimeEventText({ text: 42 })).toBe("");
    expect(parseRuntimeEventText({})).toBe("");
  });
});

describe("parseRuntimeErrorMessage", () => {
  it("returns the payload message when present", () => {
    expect(parseRuntimeErrorMessage({ message: "boom" })).toBe("boom");
  });

  it("falls back to a generic message", () => {
    expect(parseRuntimeErrorMessage(null)).toBe("runtime error");
    expect(parseRuntimeErrorMessage({})).toBe("runtime error");
    expect(parseRuntimeErrorMessage({ message: "" })).toBe("runtime error");
    expect(parseRuntimeErrorMessage({ message: 5 })).toBe("runtime error");
  });
});
