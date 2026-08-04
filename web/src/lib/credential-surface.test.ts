import { describe, expect, it } from "vitest";

import {
  CREDENTIAL_SURFACE_PATHS,
  filterCredentialSurfaceNav,
  filterCredentialSurfaceRoutes,
} from "./credential-surface";

const NAV = [
  { path: "/sessions", label: "Sessions" },
  { path: "/models", label: "Models" },
  { path: "/env", label: "Keys" },
  { path: "/docs", label: "Documentation" },
];

const ROUTES = {
  "/sessions": "SessionsPage",
  "/models": "ModelsPage",
  "/env": "EnvPage",
  "/docs": "DocsPage",
};

describe("filterCredentialSurfaceNav", () => {
  it("removes Models and Keys when the deployment does not offer them", () => {
    expect(filterCredentialSurfaceNav(NAV, false).map((n) => n.path)).toEqual([
      "/sessions",
      "/docs",
    ]);
  });

  it("keeps them when the deployment does", () => {
    expect(filterCredentialSurfaceNav(NAV, true).map((n) => n.path)).toEqual([
      "/sessions",
      "/models",
      "/env",
      "/docs",
    ]);
  });

  // Without this, a filter that dropped everything would satisfy the first
  // case and read as correct.
  it("leaves unrelated entries alone", () => {
    const untouched = filterCredentialSurfaceNav(
      [{ path: "/sessions", label: "Sessions" }],
      false,
    );
    expect(untouched).toHaveLength(1);
  });

  it("does not mutate the input", () => {
    const input = [...NAV];
    filterCredentialSurfaceNav(input, false);
    expect(input).toHaveLength(NAV.length);
  });
});

describe("filterCredentialSurfaceRoutes", () => {
  it("removes the routes so a deep link cannot reach a refused page", () => {
    expect(Object.keys(filterCredentialSurfaceRoutes(ROUTES, false))).toEqual([
      "/sessions",
      "/docs",
    ]);
  });

  it("keeps them when the deployment offers the surface", () => {
    expect(Object.keys(filterCredentialSurfaceRoutes(ROUTES, true))).toEqual(
      Object.keys(ROUTES),
    );
  });

  it("does not mutate the input", () => {
    const input = { ...ROUTES };
    filterCredentialSurfaceRoutes(input, false);
    expect(Object.keys(input)).toEqual(Object.keys(ROUTES));
  });
});

describe("the two filters agree", () => {
  // The nav and the route table are filtered by separate functions. If they
  // ever consulted different lists, the sidebar could hide an entry whose page
  // stayed reachable, or the reverse. Both read this constant; this fails if
  // one of them stops.
  it("hide exactly the same paths", () => {
    const navHidden = NAV.filter(
      (n) => !filterCredentialSurfaceNav(NAV, false).some((k) => k.path === n.path),
    ).map((n) => n.path);
    const routesHidden = Object.keys(ROUTES).filter(
      (p) => !(p in filterCredentialSurfaceRoutes(ROUTES, false)),
    );
    expect(navHidden).toEqual(routesHidden);
    expect(navHidden).toEqual([...CREDENTIAL_SURFACE_PATHS]);
  });
});
