"""Fast unit tests for super_lotto_simulator.py (the full pipeline is covered by --self-test)."""

from __future__ import annotations

import math

import numpy as np
import pytest
import super_lotto_simulator as sim


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("J$25,000,000", 25_000_000),
        ("$25.5M", 25_500_000),
        ("JMD 120 million", 120_000_000),
        ("J$1.2 billion", 1_200_000_000),
        (1_000_000, 1_000_000),
    ],
)
def test_clean_jackpot_parses_common_formats(raw, expected):
    assert sim.clean_jackpot(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["n/a", None, "", float("nan")])
def test_clean_jackpot_unparseable_is_nan(raw):
    assert math.isnan(sim.clean_jackpot(raw))


def test_bh_adjust_matches_hand_calculation():
    p = np.array([0.01, 0.04, 0.03, 0.20])
    # sorted: 0.01, 0.03, 0.04, 0.20 -> *4/rank = 0.04, 0.06, 0.0533, 0.20 -> monotone from the top
    np.testing.assert_allclose(sim.bh_adjust(p), [0.04, 0.05333333, 0.05333333, 0.20])


def test_normalize_inclusion_sums_to_k_and_respects_cap():
    p = sim.normalize_inclusion(np.array([100.0, 1, 1, 1, 1, 1, 1, 1]), k=5, cap=0.9)
    assert p.sum() == pytest.approx(5)
    assert p.max() <= 0.9 + 1e-12


def test_set_probabilities_uniform_equals_one_over_n_choose_k():
    n = 12
    combos = np.array([[0, 1, 2, 3, 4], [3, 5, 7, 9, 11]])
    probs = sim.set_probabilities(combos, np.ones(n))
    np.testing.assert_allclose(probs, 1 / math.comb(n, 5))


def test_sample_tickets_are_valid_and_respect_constraint():
    rng = np.random.default_rng(0)
    pred = sim.Prediction(np.full(35, 5 / 35), np.full(10, 0.1))
    history = np.sort(np.argsort(rng.random((500, 35)), axis=1)[:, :5] + 1, axis=1)
    constraint = sim.StructureConstraint.from_values(history)
    mains, supers = sim.sample_tickets(pred, 2_000, rng, main_lo=1, super_lo=1, constraint=constraint)
    assert mains.shape == (2_000, 5) and supers.shape == (2_000,)
    assert all(len(set(row)) == 5 for row in mains)
    assert mains.min() >= 1 and mains.max() <= 35 and supers.min() >= 1 and supers.max() <= 10
    assert constraint.mask(mains).all()


def test_prepare_draws_drops_bad_and_duplicate_rows():
    raw = sim.make_synthetic_draws(60, main_max=35, super_max=10, rng=np.random.default_rng(1))
    raw["excel_row"] = np.arange(len(raw)) + 2
    df, rep = sim.prepare_draws(raw, main_max=35, super_max=10)
    assert len(rep.bad_rows) == 3
    assert rep.duplicates_dropped == 1
    assert len(df) == 60
    assert df["date"].is_monotonic_increasing
    assert (np.diff(df[["n1", "n2", "n3", "n4", "n5"]].to_numpy(), axis=1) > 0).all()


def test_load_results_reads_csv_and_reports_missing_columns(tmp_path):
    raw = sim.make_synthetic_draws(40, main_max=35, super_max=10, rng=np.random.default_rng(2))
    good = tmp_path / "draws.csv"
    raw.to_csv(good, index=False)
    assert len(sim.load_results(good, sheet=None)) == len(raw)

    bad = tmp_path / "missing.csv"
    raw.drop(columns=[sim.SUPER_COL]).to_csv(bad, index=False)
    with pytest.raises(sim.DataError, match="Super Ball"):
        sim.load_results(bad, sheet=None)


def _history(seed: int, n: int = 200) -> sim.DrawHistory:
    raw = sim.make_synthetic_draws(n, main_max=35, super_max=10, rng=np.random.default_rng(seed))
    raw["excel_row"] = np.arange(len(raw)) + 2
    df, rep = sim.prepare_draws(raw, main_max=35, super_max=10)
    return sim.DrawHistory.from_frame(df, rep)


def test_models_never_see_future_draws():
    """Changing draws after time t must not change any model's prediction for draw t (no leakage)."""
    hist_a = _history(seed=3)
    t = 150
    df_b = hist_a.frame.copy()
    future = _history(seed=4).frame.iloc[t:]
    df_b.loc[t:, ["n1", "n2", "n3", "n4", "n5", "super"]] = future[["n1", "n2", "n3", "n4", "n5", "super"]].values
    hist_b = sim.DrawHistory.from_frame(df_b, sim.PrepReport(main_range=(1, 35), super_range=(1, 10)))

    cfg = sim.Config(input=None, sheet=None, output_dir=None, use_ml=sim.HAVE_SKLEARN)  # type: ignore[arg-type]
    for model_a, model_b in zip(sim.build_components(cfg), sim.build_components(cfg)):
        pa = model_a.fit(hist_a.view(t)).predict_proba()
        pb = model_b.fit(hist_b.view(t)).predict_proba()
        np.testing.assert_allclose(pa.main, pb.main, err_msg=model_a.name)
        np.testing.assert_allclose(pa.super_, pb.super_, err_msg=model_a.name)


def test_markov_model_returns_valid_probabilities():
    hist = _history(seed=5, n=80)
    pred = sim.MarkovModel().fit(hist.view(80)).predict_proba()
    assert pred.main.sum() == pytest.approx(5)
    assert pred.super_.sum() == pytest.approx(1)
    assert (pred.main > 0).all() and (pred.main < 1).all()
