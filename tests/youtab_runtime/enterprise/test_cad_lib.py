"""Library-backed (numpy) 1-D FEA: matches analytical + cross-checks reference."""

from __future__ import annotations

import math

import pytest

from youtab_runtime.enterprise import cad, cad_lib


def _model(elements):
    return {
        "length": 2.0,
        "area": 0.01,
        "youngs_modulus": 200e9,
        "force": 1000.0,
        "elements": elements,
    }


# --------------------------------------------------------------------------- #
# numpy result vs closed-form analytical                                       #
# --------------------------------------------------------------------------- #
def test_numpy_matches_analytical():
    out = cad_lib.fea_run_numpy(_model(8))
    # delta = F*L/(A*E) = 1000*2/(0.01*200e9) = 1e-6
    assert out["tip_displacement"] == pytest.approx(1e-6, rel=1e-9)
    # sigma = F/A = 1000/0.01 = 1e5
    assert out["max_axial_stress"] == pytest.approx(1e5, rel=1e-6)
    assert out["converged"] is True
    assert out["solver"] == cad.FEA_SOLVER
    assert out["backend"] == "numpy"
    assert out["numpy_version"]


def test_numpy_cross_checks_reference():
    r = cad_lib.cross_check_against_reference(_model(8))
    assert r["agreed"] is True
    assert r["relative_error"] <= cad_lib.CROSS_CHECK_REL_TOL
    assert r["numpy_tip_displacement"] == pytest.approx(
        r["reference_tip_displacement"], rel=1e-9
    )


def test_numpy_mesh_independent_for_tip_load():
    a = cad_lib.fea_run_numpy(_model(1))["tip_displacement"]
    b = cad_lib.fea_run_numpy(_model(32))["tip_displacement"]
    assert math.isclose(a, b, rel_tol=1e-9)


def test_checksum_deterministic_across_runs():
    c1 = cad_lib.fea_run_numpy(_model(8))["result_checksum"]
    c2 = cad_lib.fea_run_numpy(_model(8))["result_checksum"]
    assert c1 == c2
    assert len(c1) == 64  # sha256 hex


def test_zero_elements_fails_closed():
    with pytest.raises(cad.CadError):
        cad_lib.fea_run_numpy(_model(0))


def test_negative_elements_fails_closed():
    with pytest.raises(cad.CadError):
        cad_lib.fea_run_numpy(_model(-3))


def test_unbounded_elements_fails_closed():
    with pytest.raises(cad.CadError):
        cad_lib.fea_run_numpy(_model(cad_lib.MAX_ELEMENTS + 1))


def test_non_int_elements_fails_closed():
    m = _model(4)
    m["elements"] = 4.5
    with pytest.raises(cad.CadError):
        cad_lib.fea_run_numpy(m)


def test_nonpositive_modulus_fails_closed():
    m = _model(4)
    m["youngs_modulus"] = 0
    with pytest.raises(cad.CadError):
        cad_lib.fea_run_numpy(m)


def test_non_finite_length_fails_closed():
    m = _model(4)
    m["length"] = float("inf")
    with pytest.raises(cad.CadError):
        cad_lib.fea_run_numpy(m)


def test_singular_degenerate_fails_closed():
    # Zero area drives element stiffness to zero -> non-physical / singular
    # system; must fail closed rather than emit a spurious result.
    m = _model(4)
    m["area"] = 0.0
    with pytest.raises(cad.CadError):
        cad_lib.fea_run_numpy(m)


def test_cross_check_bad_tol_fails_closed():
    with pytest.raises(cad.CadError):
        cad_lib.cross_check_against_reference(_model(8), rel_tol=0)


if __name__ == "__main__":  # pragma: no cover - standalone smoke run
    import traceback

    fns = [
        v
        for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\ncad_lib standalone: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
