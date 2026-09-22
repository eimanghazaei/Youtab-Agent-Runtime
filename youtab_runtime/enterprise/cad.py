"""Real bounded CAD computation: geometry, DFM rules, and a 1-D FEA solver.

Review v1.0 item 7 requires the CAD reference boundary to perform *actual*
computation rather than return canned values. This module, pure-stdlib and
deterministic, does exactly that:

  * :func:`inspect_geometry` — parses/constructs real geometry (2-D polygon or a
    3-D axis-aligned box) and computes a genuine property (shoelace area +
    perimeter, or volume + surface area). Degenerate/invalid geometry fails
    closed.
  * :func:`dfm_check` — executes a real design-for-manufacturability rule
    (minimum feature size) against the geometry and returns the measured value.
  * :func:`fea_run` — runs a bounded, deterministic 1-D linear-elastic finite
    element analysis of an axially loaded bar: it discretises the bar, assembles
    the global stiffness matrix, solves ``K u = f`` by Gaussian elimination with
    partial pivoting, and reports the tip displacement and max stress. The
    result is validated against the independently computed closed-form solution
    ``delta = F*L / (A*E)``; a singular system or non-physical input fails
    closed.

Every function returns a ``solver``/``solver_version`` identity and enough
provenance for the connector to bind an input digest, solver identity and result
digest into the Runtime receipt.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

__all__ = [
    "CadError",
    "GEOMETRY_SOLVER",
    "DFM_SOLVER",
    "FEA_SOLVER",
    "SOLVER_VERSION",
    "inspect_geometry",
    "dfm_check",
    "fea_run",
]


class CadError(ValueError):
    """A fail-closed CAD computation error (invalid geometry / solver failure)."""


GEOMETRY_SOLVER = "youtab.cad.geometry"
DFM_SOLVER = "youtab.cad.dfm"
FEA_SOLVER = "youtab.cad.fea.bar1d"
SOLVER_VERSION = "1.0.0"

_EPS = 1e-12


def _as_float(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CadError(f"{name} must be a number")
    f = float(v)
    if not math.isfinite(f):
        raise CadError(f"{name} must be finite")
    return f


# --------------------------------------------------------------------------- #
# Geometry                                                                     #
# --------------------------------------------------------------------------- #
def _polygon_area_perimeter(
    vertices: Sequence[Sequence[float]],
) -> tuple[float, float, float]:
    if len(vertices) < 3:
        raise CadError("polygon needs at least 3 vertices")
    pts = []
    for i, v in enumerate(vertices):
        if len(v) != 2:
            raise CadError(f"vertex {i} must be [x, y]")
        pts.append((_as_float(v[0], "x"), _as_float(v[1], "y")))
    area2 = 0.0
    perim = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
        perim += math.hypot(x2 - x1, y2 - y1)
    area = abs(area2) / 2.0
    if area <= _EPS:
        raise CadError("degenerate polygon (zero area)")
    # Minimum edge length is a real, reusable feature measure for DFM.
    min_edge = min(
        math.hypot(pts[(i + 1) % n][0] - pts[i][0], pts[(i + 1) % n][1] - pts[i][1])
        for i in range(n)
    )
    return area, perim, min_edge


def inspect_geometry(geometry: Mapping[str, Any]) -> dict:
    """Compute a real geometric property; raise :class:`CadError` if invalid."""
    if not isinstance(geometry, Mapping):
        raise CadError("geometry must be a mapping")
    kind = geometry.get("kind")
    if kind == "polygon":
        area, perim, min_edge = _polygon_area_perimeter(geometry.get("vertices", []))
        return {
            "kind": "polygon",
            "area": round(area, 9),
            "perimeter": round(perim, 9),
            "min_feature": round(min_edge, 9),
            "solver": GEOMETRY_SOLVER,
            "solver_version": SOLVER_VERSION,
        }
    if kind == "box":
        dims = geometry.get("dimensions", {})
        w = _as_float(dims.get("width"), "width")
        h = _as_float(dims.get("height"), "height")
        d = _as_float(dims.get("depth"), "depth")
        if min(w, h, d) <= _EPS:
            raise CadError("box dimensions must be positive")
        volume = w * h * d
        surface = 2.0 * (w * h + h * d + w * d)
        return {
            "kind": "box",
            "volume": round(volume, 9),
            "surface_area": round(surface, 9),
            "min_feature": round(min(w, h, d), 9),
            "solver": GEOMETRY_SOLVER,
            "solver_version": SOLVER_VERSION,
        }
    raise CadError(f"unsupported geometry kind {kind!r}")


# --------------------------------------------------------------------------- #
# DFM                                                                          #
# --------------------------------------------------------------------------- #
def dfm_check(geometry: Mapping[str, Any], *, min_feature_size: Any) -> dict:
    """Execute a minimum-feature-size DFM rule against real geometry."""
    threshold = _as_float(min_feature_size, "min_feature_size")
    if threshold <= 0:
        raise CadError("min_feature_size must be positive")
    inspected = inspect_geometry(geometry)  # fails closed on bad geometry
    measured = inspected["min_feature"]
    passed = measured >= threshold
    return {
        "rule": "min_feature_size",
        "threshold": round(threshold, 9),
        "measured_min_feature": measured,
        "passed": bool(passed),
        "solver": DFM_SOLVER,
        "solver_version": SOLVER_VERSION,
    }


# --------------------------------------------------------------------------- #
# FEA — 1-D linear-elastic axially loaded bar                                  #
# --------------------------------------------------------------------------- #
def _solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; raise on a singular system."""
    n = len(rhs)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-15:
            raise CadError("singular stiffness matrix — solver failed")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, n):
            factor = a[r][col] / a[col][col]
            for c in range(col, n + 1):
                a[r][c] -= factor * a[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = a[r][n] - sum(a[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / a[r][r]
    return x


def fea_run(model: Mapping[str, Any]) -> dict:
    """Bounded deterministic 1-D FEA of an axial bar, validated vs closed form.

    ``model`` keys: ``length`` (L), ``area`` (A), ``youngs_modulus`` (E),
    ``force`` (F, axial tip load), ``elements`` (N, 1..1000). Node 0 is fixed;
    the tip node carries F. Returns tip displacement + max axial stress with the
    solver identity, and asserts the FE tip displacement matches the analytical
    ``F*L/(A*E)`` within tolerance (linear elements are exact for a tip load).
    """
    if not isinstance(model, Mapping):
        raise CadError("model must be a mapping")
    L = _as_float(model.get("length"), "length")
    A = _as_float(model.get("area"), "area")
    E = _as_float(model.get("youngs_modulus"), "youngs_modulus")
    F = _as_float(model.get("force"), "force")
    n_el = model.get("elements")
    if isinstance(n_el, bool) or not isinstance(n_el, int):
        raise CadError("elements must be an int")
    if not (1 <= n_el <= 1000):
        raise CadError("elements must be in [1, 1000] (bounded)")
    if min(L, A, E) <= _EPS:
        raise CadError("length/area/youngs_modulus must be positive")

    le = L / n_el
    k_el = A * E / le
    n_nodes = n_el + 1
    # Assemble global stiffness (tridiagonal), then strike the fixed DOF (node 0).
    K = [[0.0] * n_nodes for _ in range(n_nodes)]
    for e in range(n_el):
        for (i, j, val) in (
            (e, e, k_el), (e, e + 1, -k_el),
            (e + 1, e, -k_el), (e + 1, e + 1, k_el),
        ):
            K[i][j] += val
    free = list(range(1, n_nodes))
    Kff = [[K[i][j] for j in free] for i in free]
    f = [0.0] * len(free)
    f[-1] = F  # tip load on the last (free) node
    u_free = _solve_linear(Kff, f)
    u = [0.0] + u_free
    tip = u[-1]

    analytical = F * L / (A * E)
    denom = abs(analytical) if abs(analytical) > _EPS else 1.0
    rel_err = abs(tip - analytical) / denom
    if rel_err > 1e-6:
        raise CadError(
            f"FEA result failed validation vs analytical (rel_err={rel_err:.3e})"
        )
    # Max axial stress = E * strain; strain uniform = tip/L for a tip load.
    max_stress = E * (tip / L)
    return {
        "tip_displacement": tip,
        "analytical_tip_displacement": analytical,
        "relative_error": rel_err,
        "max_axial_stress": max_stress,
        "elements": n_el,
        "converged": True,
        "solver": FEA_SOLVER,
        "solver_version": SOLVER_VERSION,
    }
