"""Lane-2 CAD: real geometry/DFM/FEA computation validated independently."""

from __future__ import annotations

import math

import pytest

from youtab_runtime.enterprise import cad


# --------------------------------------------------------------------------- #
# Geometry — validated against hand-computed values                            #
# --------------------------------------------------------------------------- #
def test_polygon_area_is_real():
    # 2x2 square: area 4, perimeter 8, min edge 2.
    out = cad.inspect_geometry(
        {"kind": "polygon", "vertices": [[0, 0], [2, 0], [2, 2], [0, 2]]}
    )
    assert out["area"] == pytest.approx(4.0)
    assert out["perimeter"] == pytest.approx(8.0)
    assert out["min_feature"] == pytest.approx(2.0)
    assert out["solver"] == cad.GEOMETRY_SOLVER


def test_triangle_area_is_real():
    # right triangle legs 3,4 -> area 6, hypot 5, perimeter 12.
    out = cad.inspect_geometry(
        {"kind": "polygon", "vertices": [[0, 0], [3, 0], [0, 4]]}
    )
    assert out["area"] == pytest.approx(6.0)
    assert out["perimeter"] == pytest.approx(12.0)


def test_box_volume_is_real():
    out = cad.inspect_geometry(
        {"kind": "box", "dimensions": {"width": 3, "height": 4, "depth": 5}}
    )
    assert out["volume"] == pytest.approx(60.0)
    assert out["surface_area"] == pytest.approx(94.0)
    assert out["min_feature"] == pytest.approx(3.0)


def test_degenerate_polygon_fails_closed():
    with pytest.raises(cad.CadError):
        cad.inspect_geometry(
            {"kind": "polygon", "vertices": [[0, 0], [1, 1], [2, 2]]}
        )


def test_too_few_vertices_fails_closed():
    with pytest.raises(cad.CadError):
        cad.inspect_geometry({"kind": "polygon", "vertices": [[0, 0], [1, 0]]})


def test_nonpositive_box_fails_closed():
    with pytest.raises(cad.CadError):
        cad.inspect_geometry(
            {"kind": "box", "dimensions": {"width": 0, "height": 4, "depth": 5}}
        )


def test_unsupported_geometry_fails_closed():
    with pytest.raises(cad.CadError):
        cad.inspect_geometry({"kind": "torus"})


# --------------------------------------------------------------------------- #
# DFM — a real rule executed against geometry                                  #
# --------------------------------------------------------------------------- #
def test_dfm_pass():
    r = cad.dfm_check(
        {"kind": "box", "dimensions": {"width": 3, "height": 4, "depth": 5}},
        min_feature_size=2.0,
    )
    assert r["measured_min_feature"] == pytest.approx(3.0)
    assert r["passed"] is True


def test_dfm_fail():
    r = cad.dfm_check(
        {"kind": "box", "dimensions": {"width": 1, "height": 4, "depth": 5}},
        min_feature_size=2.0,
    )
    assert r["measured_min_feature"] == pytest.approx(1.0)
    assert r["passed"] is False


def test_dfm_bad_threshold_fails_closed():
    with pytest.raises(cad.CadError):
        cad.dfm_check(
            {"kind": "box", "dimensions": {"width": 3, "height": 4, "depth": 5}},
            min_feature_size=0,
        )


# --------------------------------------------------------------------------- #
# FEA — bounded solver validated vs closed form                                #
# --------------------------------------------------------------------------- #
def _model(elements):
    return {
        "length": 2.0,
        "area": 0.01,
        "youngs_modulus": 200e9,
        "force": 1000.0,
        "elements": elements,
    }


def test_fea_matches_analytical():
    out = cad.fea_run(_model(8))
    # delta = F*L/(A*E) = 1000*2/(0.01*200e9) = 1e-6
    assert out["tip_displacement"] == pytest.approx(1e-6, rel=1e-9)
    assert out["analytical_tip_displacement"] == pytest.approx(1e-6, rel=1e-9)
    # sigma = F/A = 1000/0.01 = 1e5
    assert out["max_axial_stress"] == pytest.approx(1e5, rel=1e-6)
    assert out["converged"] is True
    assert out["solver"] == cad.FEA_SOLVER


def test_fea_is_mesh_independent_for_tip_load():
    a = cad.fea_run(_model(1))["tip_displacement"]
    b = cad.fea_run(_model(16))["tip_displacement"]
    assert math.isclose(a, b, rel_tol=1e-9)


def test_fea_zero_elements_fails_closed():
    with pytest.raises(cad.CadError):
        cad.fea_run(_model(0))


def test_fea_nonpositive_modulus_fails_closed():
    m = _model(4)
    m["youngs_modulus"] = 0
    with pytest.raises(cad.CadError):
        cad.fea_run(m)


def test_fea_unbounded_elements_fails_closed():
    with pytest.raises(cad.CadError):
        cad.fea_run(_model(5000))


def test_fea_non_int_elements_fails_closed():
    m = _model(4)
    m["elements"] = 4.5
    with pytest.raises(cad.CadError):
        cad.fea_run(m)


if __name__ == "__main__":  # pragma: no cover - standalone smoke run
    import traceback

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v) and not k.startswith("test_dfm_bad")]
    # include all test_ fns
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\nenterprise_cad standalone: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
