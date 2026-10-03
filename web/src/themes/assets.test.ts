import { describe, expect, it } from "vitest";

import { assetVars } from "./assets";

describe("assetVars CSS image wrapping", () => {
  it("escapes backslashes, quotes, and control characters in bare assets", () => {
    const value = 'https://example.test/a\\" ); color:red; /*\nimage.png';
    const vars = assetVars({ bg: value, custom: { mark: value } });
    const expected = 'url("https://example.test/a\\\\\\" ); color:red; /*\\a image.png")';

    expect(vars["--theme-asset-bg"]).toBe(expected);
    expect(vars["--theme-asset-custom-mark"]).toBe(expected);
    expect(vars["--theme-asset-bg-raw"]).toBe(value);
  });

  it("preserves bare data URLs and supported CSS expressions", () => {
    const vars = assetVars({
      hero: "data:image/svg+xml;base64,PHN2Zy8+",
      logo: "url('/logo.png')",
      bg: "linear-gradient(red, blue)",
      crest: "none",
    });

    expect(vars["--theme-asset-hero"]).toBe('url("data:image/svg+xml;base64,PHN2Zy8+")');
    expect(vars["--theme-asset-logo"]).toBe("url('/logo.png')");
    expect(vars["--theme-asset-bg"]).toBe("linear-gradient(red, blue)");
    expect(vars["--theme-asset-crest"]).toBe("none");
  });
});
