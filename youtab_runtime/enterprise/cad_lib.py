"""Library-backed (numpy) 1-D FEA execution, cross-checked vs the reference.

This is an *additive* companion to :mod:`youtab_runtime.enterprise.cad`. Where
``cad.fea_run`` is a pure-Python 1-D axial-bar solver (Gaussian elimination,
validated against the closed form ``F*L/(A*E)``), this module performs the same
physics with the locked ``numpy`` dependency:

  * :func:`fea_run_numpy` — discretises an axially loaded bar, assembles the
    tridiagonal global stiffness matrix with numpy, strikes the fixed DOF
    (node 0), and solves ``K u = f`` via :func:`numpy.linalg.solve`. A singular
    system (``numpy.linalg.LinAlgError``) or any non-physical / out-of-bounds
    input fails closed by raising :class:`cad.CadError`.
  * :func:`cross_check_against_reference` — runs BOTH ``cad.fea_run`` and
    :func:`fea_run_numpy` and asserts their tip displacements agree.

The numpy solver reuses :class:`cad.CadError` and the ``cad.FEA_SOLVER`` /
``cad.SOLVER_VERSION`` identity so a receipt can bind a single solver identity
across both backends. The result carries ``"backend": "numpy"`` and the pinned
``numpy_version`` for provenance.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

import numpy as np

from youtab_runtime.enterprise import cad

__all__ = [
    "FEA_NUMPY_BACKEND",
    "MIN_ELEMENTS",
    "MAX_ELEMENTS",
    "CROSS_CHECK_REL_TOL",
    "fea_run_numpy",
    "cross_check_against_reference",
]

FEA_NUMPY_BACKEND = "numpy"
MIN_ELEMENTS = 1
MAX_ELEMENTS = 5000

# Documented default tolerance for cross_check_against_reference: both backends
# solve the identical linear system for a tip load, for which linear elements
# are exact, so agreement is limited only by floating-point round-off. 1e-6 is a
# conservative relative tolerance well above that noise floor.
CROSS_CHECK_REL_TOL = 1e-6

_EPS = 1e-12
_ROUND_DECIMALS = 15


def _as_float(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise cad.CadError(f"{name} must be a number")
    f = float(v)
    if not math.isfinite(f):
        raise cad.CadError(f"{name} must be finite")
    return f


def _checksum(u: np.ndarray) -> str:
    """Deterministic sha256 of the canonically-rounded displacement vector."""
    rounded = [round(float(x), _ROUND_DECIMALS) for x in u.tolist()]
    payload = json.dumps(rounded, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fea_run_numpy(model: Mapping[str, Any]) -> dict:
    """numpy-backed bounded 1-D FEA of an axially loaded bar (fails closed).

    ``model`` keys: ``length`` (L), ``area`` (A), ``youngs_modulus`` (E),
    ``force`` (F, axial tip load), ``elements`` (N, ``MIN_ELEMENTS``..
    ``MAX_ELEMENTS``). Node 0 is fixed; the tip (last) node carries F. Assembles
    the tridiagonal global stiffness with numpy, strikes DOF 0, and solves
    ``K u = f`` via :func:`numpy.linalg.solve`. Returns tip displacement, max
    axial stress, a deterministic result checksum, the solver identity and the
    ``numpy`` backend/version. Invalid, non-finite, non-positive or singular
    models raise :class:`cad.CadError`.
    """
    if not isinstance(model, Mapping):
        raise cad.CadError("model must be a mapping")
    L = _as_float(model.get("length"), "length")
    A = _as_float(model.get("area"), "area")
    E = _as_float(model.get("youngs_modulus"), "youngs_modulus")
    F = _as_float(model.get("force"), "force")
    n_el = model.get("elements")
    if isinstance(n_el, bool) or not isinstance(n_el, int):
        raise cad.CadError("elements must be an int")
    if not (MIN_ELEMENTS <= n_el <= MAX_ELEMENTS):
        raise cad.CadError(
            f"elements must be in [{MIN_ELEMENTS}, {MAX_ELEMENTS}] (bounded)"
        )
    if min(L, A, E) <= _EPS:
        raise cad.CadError("length/area/youngs_modulus must be positive")

    le = L / n_el
    k_el = A * E / le
    if not math.isfinite(k_el) or k_el <= _EPS:
        raise cad.CadError("degenerate element stiffness — solver failed")
    n_nodes = n_el + 1

    # Assemble global stiffness (tridiagonal) with numpy.
    K = np.zeros((n_nodes, n_nodes), dtype=np.float64)
    ke = k_el * np.array([[1.0, -1.0], [-1.0, 1.0]], dtype=np.float64)
    for e in range(n_el):
        K[e : e + 2, e : e + 2] += ke

    # Strike the fixed DOF (node 0); free DOFs are nodes 1..n_el.
    Kff = K[1:, 1:]
    f = np.zeros(n_el, dtype=np.float64)
    f[-1] = F  # tip load on the last (free) node

    try:
        u_free = np.linalg.solve(Kff, f)
    except np.linalg.LinAlgError as exc:  # singular / non-invertible system
        raise cad.CadError(f"singular stiffness matrix — solver failed: {exc}")

    if not np.all(np.isfinite(u_free)):
        raise cad.CadError("non-finite displacement — solver failed")

    u = np.concatenate(([0.0], u_free))
    tip = float(u[-1])
    # Max axial stress = E * strain; strain is uniform = tip/L for a tip load.
    max_stress = E * (tip / L)

    return {
        "tip_displacement": tip,
        "max_axial_stress": max_stress,
        "elements": n_el,
        "converged": True,
        "result_checksum": _checksum(u),
        "solver": cad.FEA_SOLVER,
        "solver_version": cad.SOLVER_VERSION,
        "backend": FEA_NUMPY_BACKEND,
        "numpy_version": np.__version__,
    }


def cross_check_against_reference(
    model: Mapping[str, Any], *, rel_tol: float = CROSS_CHECK_REL_TOL
) -> dict:
    """Run both solvers and assert their tip displacements agree.

    Runs the reference oracle :func:`cad.fea_run` and the library-backed
    :func:`fea_run_numpy` on the same ``model`` and compares tip displacements.
    ``rel_tol`` defaults to :data:`CROSS_CHECK_REL_TOL` (``1e-6``): both backends
    solve the identical linear system for a tip load (linear elements are exact),
    so agreement is limited only by floating-point round-off, and this tolerance
    sits well above that noise floor. Raises :class:`cad.CadError` if the two
    backends diverge beyond ``rel_tol``.
    """
    if not (isinstance(rel_tol, (int, float)) and math.isfinite(rel_tol)) or rel_tol <= 0:
        raise cad.CadError("rel_tol must be a positive finite number")

    reference = cad.fea_run(model)  # fails closed on bad input
    numpy_result = fea_run_numpy(model)  # fails closed on bad input

    ref_tip = float(reference["tip_displacement"])
    num_tip = float(numpy_result["tip_displacement"])
    denom = abs(ref_tip) if abs(ref_tip) > _EPS else 1.0
    rel_err = abs(num_tip - ref_tip) / denom
    if rel_err > rel_tol:
        raise cad.CadError(
            "numpy FEA diverged from reference "
            f"(rel_err={rel_err:.3e} > rel_tol={rel_tol:.3e})"
        )

    return {
        "agreed": True,
        "relative_error": rel_err,
        "rel_tol": rel_tol,
        "reference_tip_displacement": ref_tip,
        "numpy_tip_displacement": num_tip,
        "reference": reference,
        "numpy": numpy_result,
    }
