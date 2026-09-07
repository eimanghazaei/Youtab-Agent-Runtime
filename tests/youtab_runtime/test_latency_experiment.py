"""R8 controlled-experiment harness: one-variable-at-a-time attribution.

Uses the deterministic synthetic model (no network) to prove the harness +
aggregation attribute a latency delta to the SINGLE varied axis — the property
the real Owner-authorized diagnostic run relies on.
"""

from __future__ import annotations

import pytest

from youtab_runtime import latency_experiment as lx
from youtab_runtime import stage_trace_cli as cli


BASELINE = {"num_ctx": 8192, "tool_count": 38, "prompt_tokens": 500, "process_cold": False}


def test_matrix_varies_exactly_one_axis_per_cell():
    cells = lx.one_variable_matrix(
        BASELINE,
        {"prompt_tokens": [2000], "process_cold": [True]},
    )
    base = cells[0].config
    for cell in cells[1:]:
        differing = [k for k in base if cell.config[k] != base[k]]
        assert differing == [cell.axis], f"{cell.name} changed {differing}"


def test_unknown_axis_rejected():
    with pytest.raises(ValueError):
        lx.one_variable_matrix(BASELINE, {"not_an_axis": [1]})


def test_process_cold_dominates_wall_latency():
    cells = lx.one_variable_matrix(BASELINE, {"process_cold": [True]})
    results = lx.run_experiment(cells, lx.synthetic_call_fn, repetitions=10)
    base = next(r for r in results if r.cell.axis == "baseline")
    cold = next(r for r in results if r.cell.name == "process_cold=True")
    # cold start adds the large fixed load cost -> model.call p50 jumps
    assert cold.stage_p50("model.call") > base.stage_p50("model.call") + 3000
    # attributed to the model.init (load) leg, and into the process-cold split
    assert cold.stats["model.init"].proc_cold_count > 0
    assert base.stats["model.init"].proc_warm_count > 0


def test_prompt_tokens_scales_prefill_not_generation():
    cells = lx.one_variable_matrix(BASELINE, {"prompt_tokens": [4000]})
    results = lx.run_experiment(cells, lx.synthetic_call_fn, repetitions=8)
    base = next(r for r in results if r.cell.axis == "baseline")
    big = next(r for r in results if r.cell.name == "prompt_tokens=4000")
    # prefill (prompt.tokenize) grows with tokens ...
    assert big.stage_p50("prompt.tokenize") > base.stage_p50("prompt.tokenize")
    # ... while generation is unchanged (the confound the harness must isolate)
    assert big.stage_p50("model.generate") == base.stage_p50("model.generate")


def test_num_ctx_effect_is_small_relative_to_cold():
    cells = lx.one_variable_matrix(BASELINE, {"num_ctx": [65536], "process_cold": [True]})
    results = lx.run_experiment(cells, lx.synthetic_call_fn, repetitions=6)
    base = next(r for r in results if r.cell.axis == "baseline")
    nctx = next(r for r in results if r.cell.name == "num_ctx=65536")
    cold = next(r for r in results if r.cell.name == "process_cold=True")
    nctx_delta = nctx.stage_p50("model.call") - base.stage_p50("model.call")
    cold_delta = cold.stage_p50("model.call") - base.stage_p50("model.call")
    # the harness lets us SEE that ctx sizing is a minor knob vs cold start
    assert cold_delta > 10 * nctx_delta


def test_call_error_counted_not_crashed():
    def boom(_config):
        raise RuntimeError("provider down")

    cells = lx.one_variable_matrix(BASELINE, {})
    results = lx.run_experiment(cells, boom, repetitions=5)
    assert results[0].errors == 5 and results[0].stats == {}


def test_format_experiment_shows_delta():
    cells = lx.one_variable_matrix(BASELINE, {"process_cold": [True]})
    results = lx.run_experiment(cells, lx.synthetic_call_fn, repetitions=4)
    text = lx.format_experiment(results, stage="model.call")
    assert "process_cold=True" in text and "Δ vs base" in text


# --- CLI ---------------------------------------------------------------------


def test_cli_reads_jsonl_dir_and_prints(tmp_path, capsys):
    from youtab_runtime import stage_trace as st

    d = tmp_path / "traces"
    d.mkdir()
    st.set_sink(st._JsonlSink(path=d / "trace-1.jsonl"))
    try:
        st.record(st.Stage.MODEL_CALL, duration_ns=13_300_000_000, process_cold=True)
        st.record(st.Stage.MODEL_CALL, duration_ns=900_000_000, process_cold=False)
    finally:
        st.set_sink(None)
    rc = cli.main(["--traces-dir", str(d)])
    out = capsys.readouterr().out
    assert rc == 0 and "model.call" in out


def test_cli_json_output(tmp_path, capsys):
    from youtab_runtime import stage_trace as st

    f = tmp_path / "t.jsonl"
    st.set_sink(st._JsonlSink(path=f))
    try:
        st.record(st.Stage.TOOL_CALL, duration_ns=1_000_000, tool_name="x")
    finally:
        st.set_sink(None)
    rc = cli.main(["--traces-file", str(f), "--json"])
    out = capsys.readouterr().out
    assert rc == 0 and '"tool.call"' in out


def test_cli_journal_requires_principal():
    with pytest.raises(SystemExit):
        cli.main(["--journal"])
