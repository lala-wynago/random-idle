#!/usr/bin/env python3
"""
super_lotto_simulator.py
========================

Analyse historical Super Lotto draws (.xlsx or .csv), fit several (deliberately simple and
honest) predictive models, backtest them walk-forward against a uniform random
baseline, and simulate candidate tickets from the chosen model.

The single most important output is the backtest leaderboard: it says whether
any model beat pure chance on draws it had never seen. For a fair lottery the
expected answer is "no".

requirements.txt
----------------
pandas>=1.5
numpy>=1.23
scipy>=1.9
scikit-learn>=1.2
matplotlib>=3.6
openpyxl>=3.0
# optional (graceful fallback if missing)
statsmodels>=0.13
lightgbm>=3.3

Example
-------
python super_lotto_simulator.py --input results.xlsx --n-tickets 10 \
    --n-sims 100000 --backtest-sims 2000 --model ensemble --seed 42
python super_lotto_simulator.py --self-test
"""

from __future__ import annotations

import argparse
import itertools
import logging
import math
import re
import sys
import time
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

import matplotlib
import numpy as np
import pandas as pd
from scipy import optimize, stats

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

try:  # optional: Ljung-Box cross-implementation
    from statsmodels.stats.diagnostic import acorr_ljungbox  # type: ignore

    HAVE_STATSMODELS = True
except Exception:  # pragma: no cover - optional dependency
    HAVE_STATSMODELS = False

try:  # optional: preferred gradient-boosting backend
    import lightgbm as lgb  # type: ignore

    HAVE_LIGHTGBM = True
except Exception:  # pragma: no cover - optional dependency
    HAVE_LIGHTGBM = False

try:
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import HistGradientBoostingClassifier

    HAVE_SKLEARN = True
except Exception:  # pragma: no cover - optional dependency
    HAVE_SKLEARN = False

LOG = logging.getLogger("super_lotto")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
DATE_COL = "Draw Date"
MAIN_COLS = [f"Ball {i}" for i in range(1, 6)]
SUPER_COL = "Super Ball"
JACKPOT_COL = "Jackpot (JMD)"
REQUIRED_COLUMNS = [DATE_COL, *MAIN_COLS, SUPER_COL, JACKPOT_COL]
K_MAIN = 5
EPS = 1e-9
ML_WINDOWS = (5, 10, 25, 50, 100)
GAP_BIN_EDGES = np.array([0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, np.inf])
BACKTEST_METRICS = {
    "main_ll": "main-ball log-likelihood",
    "matches": "mean main balls matched",
    "super_ll": "Super Ball log-likelihood",
}

DISCLAIMER_CORE = (
    "Lottery draws are designed to be independent and random, so historical patterns "
    "generally do not predict future draws. These tickets are simulations for "
    "entertainment and analysis only; they are not a guarantee of anything and not a "
    "basis for expecting a profit."
)


class DataError(Exception):
    """Raised for unrecoverable input problems (missing file, sheet or columns)."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    """All run-time settings (populated from the CLI)."""

    input: Optional[Path]
    sheet: Optional[str]
    output_dir: Path
    results_dir: Optional[Path] = None  # predictions.xlsx (defaults to output_dir)
    report_dir: Optional[Path] = None   # analysis_report.md + charts (defaults to output_dir)
    n_tickets: int = 10
    n_sims: int = 100_000
    backtest_sims: int = 2_000
    backtest_draws: int = 200
    min_train: int = 100
    model: str = "ensemble"
    seed: int = 42
    main_max: Optional[int] = None
    super_max: Optional[int] = None
    half_life: float = 50.0
    constrain: bool = False
    ml_refit_every: int = 25
    use_ml: bool = True
    permutations: int = 10_000
    bootstraps: int = 5_000
    alpha: float = 0.05


# --------------------------------------------------------------------------- #
# Data loading & preparation
# --------------------------------------------------------------------------- #
_MULTIPLIERS = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "mn": 1e6, "mil": 1e6, "million": 1e6, "millions": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9, "billions": 1e9,
}
_JACKPOT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(billions?|bn|b|millions?|mil|mn|m|thousand|k)?(?![a-z])"
)


def clean_jackpot(value: Any) -> float:
    """Convert a jackpot cell such as ``'J$25,000,000'`` or ``'$25.5M'`` to a float (NaN if unparseable)."""
    if value is None:
        return math.nan
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        return float(value) if np.isfinite(value) else math.nan
    text = str(value).strip().lower().replace(",", "")
    match = _JACKPOT_RE.search(text)
    if not match:
        return math.nan
    number = float(match.group(1))
    suffix = match.group(2)
    return number * _MULTIPLIERS.get(suffix, 1.0) if suffix else number


def _normalise_header(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name)).strip().lower()


def _read_csv(path: Path) -> pd.DataFrame:
    """Read a CSV, sniffing the delimiter and tolerating Excel-style (cp1252) encodings."""
    last_exc: Optional[Exception] = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return pd.read_csv(path, sep=None, engine="python", encoding=encoding)
        except UnicodeDecodeError as exc:
            last_exc = exc
        except Exception as exc:
            raise DataError(f"Could not read {path} as a CSV file: {exc}") from exc
    raise DataError(f"Could not decode {path} (tried UTF-8 and Windows-1252): {last_exc}")


def load_results(path: Path, sheet: Optional[str]) -> pd.DataFrame:
    """Read the required columns from an .xlsx or .csv file, with clear errors for common problems."""
    if not path.exists():
        raise DataError(f"Input file not found: {path}")
    if path.is_dir():
        raise DataError(f"Input path is a directory, not an .xlsx or .csv file: {path}")
    if path.suffix.lower() == ".csv":
        if sheet is not None:
            LOG.info("--sheet is ignored for CSV input")
        raw, source = _read_csv(path), "CSV file"
    else:
        try:
            xls = pd.ExcelFile(path, engine="openpyxl")
        except Exception as exc:
            raise DataError(f"Could not open {path} as an Excel (.xlsx) workbook: {exc}") from exc
        sheets = xls.sheet_names
        if sheet is None:
            sheet_name = sheets[0]
        elif sheet in sheets:
            sheet_name = sheet
        else:
            raise DataError(f"Sheet '{sheet}' not found in {path.name}. Available sheets: {sheets}")
        raw, source = pd.read_excel(xls, sheet_name=sheet_name), f"sheet '{sheet_name}'"
    lookup = {_normalise_header(c): c for c in raw.columns}
    missing = [c for c in REQUIRED_COLUMNS if _normalise_header(c) not in lookup]
    if missing:
        raise DataError(
            f"The {source} is missing required column(s) {missing}. "
            f"Found columns: {[str(c) for c in raw.columns]}. "
            f"Expected headers: {' | '.join(REQUIRED_COLUMNS)}"
        )
    df = raw[[lookup[_normalise_header(c)] for c in REQUIRED_COLUMNS]].copy()
    df.columns = REQUIRED_COLUMNS
    df["excel_row"] = np.arange(len(df)) + 2  # header is row 1
    LOG.info("Loaded %d rows from %s [%s]", len(df), path, source)
    return df


def _parse_dates(series: pd.Series) -> pd.Series:
    """Parse dates, choosing month-first or day-first by whichever parses more rows."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        candidates = []
        for dayfirst in (False, True):
            try:
                parsed = pd.to_datetime(series, errors="coerce", dayfirst=dayfirst, format="mixed")
            except (TypeError, ValueError):
                parsed = pd.to_datetime(series, errors="coerce", dayfirst=dayfirst)
            candidates.append(parsed)
    return max(candidates, key=lambda s: s.notna().sum())


@dataclass
class PrepReport:
    """Book-keeping for the data-preparation step."""

    rows_read: int = 0
    blank_rows: int = 0
    bad_rows: list[tuple[int, str]] = field(default_factory=list)
    duplicates_dropped: int = 0
    jackpot_unparsed: int = 0
    main_range: tuple[int, int] = (1, 1)
    super_range: tuple[int, int] = (1, 1)
    main_range_source: str = "inferred"
    super_range_source: str = "inferred"
    warnings: list[str] = field(default_factory=list)

    @property
    def rows_dropped(self) -> int:
        return len(self.bad_rows) + self.duplicates_dropped


def _infer_range(values: np.ndarray, override: Optional[int]) -> tuple[int, int, str]:
    lo = 0 if values.min() == 0 else 1
    if override is not None:
        return lo, int(override), "CLI override"
    return lo, int(values.max()), "inferred from data (max observed)"


def prepare_draws(
    raw: pd.DataFrame, main_max: Optional[int], super_max: Optional[int]
) -> tuple[pd.DataFrame, PrepReport]:
    """Validate, clean, sort and de-duplicate the raw draw table.

    Bad rows are logged and dropped rather than raising. Main balls are sorted
    ascending per draw (the draw is an unordered set).
    """
    rep = PrepReport(rows_read=len(raw))
    df = raw.copy()
    blank = df[REQUIRED_COLUMNS].isna().all(axis=1)
    rep.blank_rows = int(blank.sum())
    df = df.loc[~blank].copy()

    df["date"] = _parse_dates(df[DATE_COL])
    ball_cols = MAIN_COLS + [SUPER_COL]
    nums = df[ball_cols].apply(pd.to_numeric, errors="coerce")

    def drop(mask: pd.Series, reason: str) -> None:
        nonlocal df, nums
        for row in df.loc[mask, "excel_row"]:
            rep.bad_rows.append((int(row), reason))
        df, nums = df.loc[~mask], nums.loc[~mask]

    drop(df["date"].isna(), "missing/unparseable Draw Date")
    drop(nums.isna().any(axis=1), "missing or non-numeric ball value")
    drop(((nums % 1) != 0).any(axis=1), "non-integer ball value")
    drop((nums < 0).any(axis=1), "negative ball value")
    if df.empty:
        raise DataError("No valid draws remain after removing rows with missing/invalid values.")

    nums = nums.astype(int)
    main_lo, main_hi, rep.main_range_source = _infer_range(nums[MAIN_COLS].to_numpy(), main_max)
    super_lo, super_hi, rep.super_range_source = _infer_range(nums[SUPER_COL].to_numpy(), super_max)
    rep.main_range, rep.super_range = (main_lo, main_hi), (super_lo, super_hi)

    drop(((nums[MAIN_COLS] < main_lo) | (nums[MAIN_COLS] > main_hi)).any(axis=1),
         f"main ball outside {main_lo}-{main_hi}")
    drop((nums[SUPER_COL] < super_lo) | (nums[SUPER_COL] > super_hi),
         f"Super Ball outside {super_lo}-{super_hi}")
    drop(nums[MAIN_COLS].nunique(axis=1) < K_MAIN, "duplicate number within the five main balls")
    if df.empty:
        raise DataError("No valid draws remain after validation.")
    if main_hi - main_lo + 1 <= K_MAIN:
        raise DataError(f"Main-ball range {main_lo}-{main_hi} is too small to draw {K_MAIN} unique numbers.")

    sorted_main = np.sort(nums[MAIN_COLS].to_numpy(), axis=1)
    out = pd.DataFrame(sorted_main, columns=[f"n{i}" for i in range(1, 6)], index=df.index)
    out.insert(0, "date", df["date"].values)
    out["super"] = nums[SUPER_COL].values
    out["jackpot"] = df[JACKPOT_COL].map(clean_jackpot).values
    out["excel_row"] = df["excel_row"].values
    rep.jackpot_unparsed = int((out["jackpot"].isna() & df[JACKPOT_COL].notna().values).sum())

    key = ["date", "n1", "n2", "n3", "n4", "n5", "super"]
    before = len(out)
    out = out.drop_duplicates(subset=key, keep="first")
    rep.duplicates_dropped = before - len(out)
    out = out.sort_values(["date", "excel_row"], kind="mergesort").reset_index(drop=True)

    same_day = out["date"].duplicated(keep=False)
    if same_day.any():
        rep.warnings.append(
            f"{int(same_day.sum())} draws share a date with another draw but have different "
            "numbers; they were kept (multiple draws per day assumed)."
        )
    # Range sanity: a top number that is far rarer than expected may be a typo inflating the range.
    if main_max is None:
        n_main = main_hi - main_lo + 1
        top_count = int((sorted_main[:, -1] == main_hi).sum() if len(out) else 0)
        expected = len(out) * K_MAIN / n_main
        if expected >= 20 and top_count < 0.25 * expected:
            rep.warnings.append(
                f"Highest main number {main_hi} appears only {top_count} times (expected ~{expected:.0f}); "
                "if it is a data-entry error, pass --main-max to fix the range."
            )
    for row, reason in rep.bad_rows:
        LOG.warning("Dropped Excel row %d: %s", row, reason)
    for msg in rep.warnings:
        LOG.warning(msg)
    return out, rep


# --------------------------------------------------------------------------- #
# History representation (causal state, precomputed once)
# --------------------------------------------------------------------------- #
@dataclass
class PoolState:
    """Causal per-number state for one ball pool (main or Super).

    Row ``t`` of ``count``/``gap`` describes the state *before* draw ``t`` (i.e. it
    only uses draws ``0..t-1``), so slicing ``[:t+1]`` never leaks the future.
    """

    name: str
    lo: int
    size: int
    k: int
    X: np.ndarray       # (T, N) 0/1 indicator of each number in each draw
    count: np.ndarray   # (T+1, N) appearances before draw t
    gap: np.ndarray     # (T+1, N) draws since last appearance before draw t
    cache: dict = field(default_factory=dict)

    @classmethod
    def build(cls, name: str, values: np.ndarray, lo: int, hi: int, k: int) -> PoolState:
        values = values.reshape(len(values), -1)
        T, N = len(values), hi - lo + 1
        X = np.zeros((T, N), dtype=np.float64)
        np.put_along_axis(X, values - lo, 1.0, axis=1)
        count = np.vstack([np.zeros((1, N)), np.cumsum(X, axis=0)])
        gap = np.zeros((T + 1, N))
        cur = np.zeros(N)
        for t in range(T):
            gap[t] = cur
            cur = np.where(X[t] > 0, 0.0, cur + 1.0)
        gap[T] = cur
        return cls(name, lo, N, k, X, count, gap)

    @property
    def base_rate(self) -> float:
        return self.k / self.size

    def overdue_ratio(self) -> np.ndarray:
        """(gap+1) / (expected gap+1), where expected gap+1 = (t+1)/(count+1). Shape (T+1, N)."""
        if "overdue" not in self.cache:
            t = np.arange(self.count.shape[0])[:, None]
            self.cache["overdue"] = (self.gap + 1.0) * (self.count + 1.0) / (t + 1.0)
        return self.cache["overdue"]

    def recency(self, half_life: float) -> np.ndarray:
        """Exponentially decayed appearance counts R[t] = d*R[t-1] + X[t-1]. Shape (T+1, N)."""
        key = ("recency", half_life)
        if key not in self.cache:
            d = 0.5 ** (1.0 / half_life)
            R = np.zeros_like(self.count)
            for t in range(1, R.shape[0]):
                R[t] = d * R[t - 1] + self.X[t - 1]
            self.cache[key] = R
        return self.cache[key]


@dataclass
class DrawHistory:
    """All cleaned draws plus precomputed causal state for both pools."""

    frame: pd.DataFrame
    main_values: np.ndarray  # (T, 5)
    super_values: np.ndarray  # (T,)
    main: PoolState
    super_: PoolState
    structure: np.ndarray    # (T+1, 3) scaled sum/odd/range of the previous draw

    @classmethod
    def from_frame(cls, df: pd.DataFrame, rep: PrepReport) -> DrawHistory:
        mains = df[["n1", "n2", "n3", "n4", "n5"]].to_numpy(dtype=int)
        supers = df["super"].to_numpy(dtype=int)
        (mlo, mhi), (slo, shi) = rep.main_range, rep.super_range
        main = PoolState.build("main", mains, mlo, mhi, K_MAIN)
        sup = PoolState.build("super", supers, slo, shi, 1)
        struct = np.zeros((len(df) + 1, 3))
        struct[1:, 0] = mains.sum(axis=1) / (K_MAIN * mhi)
        struct[1:, 1] = (mains % 2 == 1).sum(axis=1) / K_MAIN
        struct[1:, 2] = (mains.max(axis=1) - mains.min(axis=1)) / max(mhi - mlo, 1)
        struct[0] = struct[1:].mean(axis=0) if len(df) else 0.0
        return cls(df, mains, supers, main, sup, struct)

    @property
    def T(self) -> int:
        return len(self.main_values)

    def view(self, t: int) -> HistoryView:
        """A view that only exposes draws ``0..t-1`` (for predicting draw ``t``)."""
        if not 0 <= t <= self.T:
            raise ValueError(f"t={t} outside 0..{self.T}")
        return HistoryView(self, t)


@dataclass(frozen=True)
class HistoryView:
    """Read-only, time-truncated view of a :class:`DrawHistory`."""

    history: DrawHistory
    t: int

    @property
    def main(self) -> PoolState:
        return self.history.main

    @property
    def super_(self) -> PoolState:
        return self.history.super_

    @property
    def main_values(self) -> np.ndarray:
        return self.history.main_values[: self.t]


# --------------------------------------------------------------------------- #
# Probability helpers & sampling
# --------------------------------------------------------------------------- #
@dataclass
class Prediction:
    """Per-number probabilities for the next draw.

    ``main`` holds inclusion probabilities P(number appears) summing to 5;
    ``super_`` is a categorical distribution summing to 1.
    """

    main: np.ndarray
    super_: np.ndarray


def normalize_inclusion(weights: np.ndarray, k: int, cap: float = 0.995) -> np.ndarray:
    """Scale non-negative weights to inclusion probabilities that sum to ``k`` and stay below ``cap``."""
    w = np.clip(np.asarray(weights, dtype=float), EPS, None)
    p = k * w / w.sum()
    for _ in range(20):
        over = p > cap
        if not over.any():
            break
        free = ~over
        p[over] = cap
        p[free] = (k - cap * over.sum()) * p[free] / p[free].sum()
    return np.clip(p, EPS, cap)


@dataclass(frozen=True)
class StructureConstraint:
    """Soft structural filter (sum range and odd count) learned from history."""

    sum_lo: float
    sum_hi: float
    odd_allowed: frozenset

    @classmethod
    def from_values(cls, mains: np.ndarray, coverage: float = 0.95, min_share: float = 0.05) -> StructureConstraint:
        sums = mains.sum(axis=1)
        tail = (1 - coverage) / 2
        lo, hi = np.quantile(sums, [tail, 1 - tail])
        odd = (mains % 2 == 1).sum(axis=1)
        shares = np.bincount(odd, minlength=K_MAIN + 1) / len(odd)
        allowed = frozenset(int(i) for i in np.flatnonzero(shares >= min_share)) or frozenset(range(K_MAIN + 1))
        return cls(float(lo), float(hi), allowed)

    def mask(self, mains: np.ndarray) -> np.ndarray:
        s = mains.sum(axis=1)
        odd = (mains % 2 == 1).sum(axis=1)
        return (s >= self.sum_lo) & (s <= self.sum_hi) & np.isin(odd, list(self.odd_allowed))

    def describe(self) -> str:
        return f"sum in [{self.sum_lo:.0f}, {self.sum_hi:.0f}], odd count in {sorted(self.odd_allowed)}"


def sample_tickets(
    pred: Prediction,
    n: int,
    rng: np.random.Generator,
    main_lo: int,
    super_lo: int,
    constraint: Optional[StructureConstraint] = None,
    chunk: int = 50_000,
) -> tuple[np.ndarray, np.ndarray]:
    """Draw ``n`` valid tickets: 5 unique main numbers (without replacement) + 1 Super Ball.

    Main numbers use the Gumbel-top-k trick, which is exactly successive sampling
    without replacement with probability proportional to ``pred.main``. With a
    constraint, invalid tickets are rejected and resampled.
    """
    logw = np.log(np.clip(pred.main, EPS, None))
    N = len(logw)
    p_super = pred.super_ / pred.super_.sum()

    def batch(m: int) -> tuple[np.ndarray, np.ndarray]:
        keys = logw[None, :] + rng.gumbel(size=(m, N))
        idx = np.argpartition(keys, N - K_MAIN, axis=1)[:, N - K_MAIN:]
        return np.sort(idx, axis=1) + main_lo, rng.choice(len(p_super), size=m, p=p_super) + super_lo

    mains_out, supers_out, have, accept_rate = [], [], 0, 1.0
    for _ in range(200):
        if have >= n:
            break
        need = n - have
        m = min(chunk, int(need / max(accept_rate, 0.02) * 1.1) + 8) if constraint else min(chunk, need)
        mains, supers = batch(m)
        if constraint is not None:
            ok = constraint.mask(mains)
            accept_rate = max(ok.mean(), 0.01)
            mains, supers = mains[ok], supers[ok]
        mains_out.append(mains[:need])
        supers_out.append(supers[:need])
        have += len(mains_out[-1])
    if have < n:  # pathological constraint: fill the remainder unconstrained
        LOG.warning("Constraint rejection rate too high; filling %d tickets without constraint", n - have)
        mains, supers = sample_tickets(pred, n - have, rng, main_lo, super_lo, None, chunk)
        mains_out.append(mains)
        supers_out.append(supers)
    return np.vstack(mains_out), np.concatenate(supers_out)


_PERMS: dict[int, np.ndarray] = {}


def set_probabilities(combos_idx: np.ndarray, weights: np.ndarray, chunk: int = 4000) -> np.ndarray:
    """Exact probability of each unordered 5-set under successive sampling with ``weights``.

    Sums the Plackett-Luce probability over all 5! orderings.
    """
    k = combos_idx.shape[1]
    perms = _PERMS.setdefault(k, np.array(list(itertools.permutations(range(k)))))
    w = weights / weights.sum()
    out = np.empty(len(combos_idx))
    for start in range(0, len(combos_idx), chunk):
        seq = w[combos_idx[start:start + chunk]][:, perms]          # (c, k!, k)
        removed = np.cumsum(seq, axis=2) - seq
        out[start:start + chunk] = np.prod(seq / (1.0 - removed), axis=2).sum(axis=1)
    return out


def select_top_tickets(
    mains: np.ndarray, supers: np.ndarray, pred: Prediction, main_lo: int, super_lo: int, n: int
) -> pd.DataFrame:
    """Rank distinct simulated main-ball sets by exact model probability, then by simulated count.

    Each set's Super Ball is the one most often co-simulated with it (ties broken by model probability).
    """
    combos, inverse, counts = np.unique(mains, axis=0, return_inverse=True, return_counts=True)
    inverse = np.asarray(inverse).ravel()
    probs = set_probabilities(combos - main_lo, pred.main)
    key = np.round(probs / probs.max(), 9)
    order = np.lexsort((np.arange(len(combos)), -counts, -key))[:n]
    p_super = pred.super_ / pred.super_.sum()
    rows = []
    for rank, c in enumerate(order, start=1):
        sup_counts = np.bincount(supers[inverse == c] - super_lo, minlength=len(p_super)).astype(float)
        s_idx = int(np.argmax(sup_counts + p_super * 1e-3))
        rows.append({
            "rank": rank,
            **{f"ball_{i + 1}": int(v) for i, v in enumerate(combos[c])},
            "super_ball": s_idx + super_lo,
            "sim_count": int(counts[c]),
            "sim_frequency": counts[c] / len(mains),
            "model_prob_main_set": probs[c],
            "model_prob_super": p_super[s_idx],
            "model_prob_ticket": probs[c] * p_super[s_idx],
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class LottoModel(ABC):
    """Common interface: ``fit(view)`` -> ``predict_proba()`` -> ``sample(n, rng)``.

    ``fit`` takes a :class:`HistoryView` (cleaned draws truncated at time ``t``),
    so a model can never see the draw it is predicting.
    """

    name = "base"
    description = ""

    def __init__(self) -> None:
        self._pred: Optional[Prediction] = None
        self._view: Optional[HistoryView] = None

    def fit(self, view: HistoryView) -> LottoModel:
        self._view = view
        main = normalize_inclusion(self._pool_weights(view.main, view), view.main.k)
        sup = normalize_inclusion(self._pool_weights(view.super_, view), 1)
        self._pred = Prediction(main, sup / sup.sum())
        return self

    @abstractmethod
    def _pool_weights(self, pool: PoolState, view: HistoryView) -> np.ndarray:
        """Non-negative relative weights for each number in ``pool`` for draw ``view.t``."""

    def predict_proba(self) -> Prediction:
        if self._pred is None:
            raise RuntimeError(f"{self.name}: call fit() before predict_proba()")
        return self._pred

    def sample(
        self, n: int, rng: np.random.Generator, constraint: Optional[StructureConstraint] = None
    ) -> tuple[np.ndarray, np.ndarray]:
        assert self._view is not None
        return sample_tickets(self.predict_proba(), n, rng, self._view.main.lo, self._view.super_.lo, constraint)


class UniformModel(LottoModel):
    """Control: every number equally likely."""

    name = "baseline"
    description = "Uniform random sampling (control)"

    def _pool_weights(self, pool: PoolState, view: HistoryView) -> np.ndarray:
        return np.ones(pool.size)


class FrequencyModel(LottoModel):
    """Historical frequency with Laplace (add-alpha) smoothing."""

    name = "frequency"
    description = "Historical frequency, Laplace-smoothed"

    def __init__(self, alpha: float = 1.0) -> None:
        super().__init__()
        self.alpha = alpha

    def _pool_weights(self, pool: PoolState, view: HistoryView) -> np.ndarray:
        return pool.count[view.t] + self.alpha


class RecencyModel(LottoModel):
    """Exponentially decayed frequency (half-life in draws)."""

    name = "recency"
    description = "Recency-weighted frequency (exponential decay)"

    def __init__(self, half_life: float = 50.0, alpha: float = 0.5) -> None:
        super().__init__()
        self.half_life, self.alpha = half_life, alpha

    def _pool_weights(self, pool: PoolState, view: HistoryView) -> np.ndarray:
        return pool.recency(self.half_life)[view.t] + self.alpha * pool.base_rate


class GapModel(LottoModel):
    """Empirically calibrated hit rate as a function of overdue ratio (current gap / average gap).

    The overdue ratio is binned and each bin's historical hit rate (shrunk towards
    the base rate) becomes the number's weight. If "overdue" numbers are not
    actually more likely, the calibration learns that and stays near uniform.
    """

    name = "gap"
    description = "Gap/overdue, calibrated hit rate by overdue-ratio bin"

    def __init__(self, prior_strength: float = 50.0, warmup: int = 10) -> None:
        super().__init__()
        self.prior_strength, self.warmup = prior_strength, warmup

    def _pool_weights(self, pool: PoolState, view: HistoryView) -> np.ndarray:
        ratio = pool.overdue_ratio()
        nb = len(GAP_BIN_EDGES) - 1
        bins_now = np.clip(np.digitize(ratio[view.t], GAP_BIN_EDGES) - 1, 0, nb - 1)
        if view.t <= self.warmup:
            return np.ones(pool.size)
        r = ratio[self.warmup:view.t]
        y = pool.X[self.warmup:view.t]
        b = np.clip(np.digitize(r, GAP_BIN_EDGES) - 1, 0, nb - 1).ravel()
        hits = np.bincount(b, weights=y.ravel(), minlength=nb)
        n = np.bincount(b, minlength=nb)
        rate = (hits + self.prior_strength * pool.base_rate) / (n + self.prior_strength)
        return rate[bins_now]


class MarkovModel(LottoModel):
    """First-order transitions: P(k in draw t | j in draw t-1), smoothed, averaged over last draw's numbers."""

    name = "markov"
    description = "First-order transition model between consecutive draws"

    def __init__(self, alpha: float = 1.0) -> None:
        super().__init__()
        self.alpha = alpha

    def _pool_weights(self, pool: PoolState, view: HistoryView) -> np.ndarray:
        t = view.t
        if t < 2:
            return np.ones(pool.size)
        M = pool.X[: t - 1].T @ pool.X[1:t]  # (N, N) counts j -> k
        P = (M + self.alpha) / (M.sum(axis=1, keepdims=True) + self.alpha * pool.size)
        last = pool.X[t - 1] > 0
        return P[last].mean(axis=0)


def _make_classifier(seed: int) -> Any:
    """LightGBM if installed, otherwise sklearn's HistGradientBoosting (both shallow & regularised)."""
    if HAVE_LIGHTGBM:
        return lgb.LGBMClassifier(
            n_estimators=150, learning_rate=0.05, num_leaves=7, min_child_samples=200,
            reg_lambda=1.0, subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
            random_state=seed, deterministic=True, verbose=-1, n_jobs=1,
        )
    return HistGradientBoostingClassifier(
        max_iter=150, learning_rate=0.05, max_depth=3, min_samples_leaf=200,
        l2_regularization=1.0, early_stopping=False, random_state=seed,
    )


def build_ml_features(pool: PoolState, structure: np.ndarray, half_life: float) -> np.ndarray:
    """Causal lag features, shape (T+1, N, F). Row t uses only draws before t."""
    key = ("ml_features", half_life)
    if key in pool.cache:
        return pool.cache[key]
    T1, N = pool.count.shape
    t = np.arange(T1)
    feats = [np.log1p(pool.gap), np.log(pool.overdue_ratio())]
    for w in ML_WINDOWS:
        prev = np.maximum(t - w, 0)
        denom = np.clip(np.minimum(t, w), 1, None)[:, None]
        feats.append((pool.count - pool.count[prev]) / denom / pool.base_rate)
    feats.append(pool.count / np.clip(t, 1, None)[:, None] / pool.base_rate)
    last = np.zeros((T1, N))
    last[1:] = pool.X
    feats.append(last)
    d = 0.5 ** (1.0 / half_life)
    mass = np.clip((1 - d ** t) / (1 - d), 1e-9, None)[:, None]
    feats.append(pool.recency(half_life) / mass / pool.base_rate)
    for j in range(structure.shape[1]):
        feats.append(np.broadcast_to(structure[:, j:j + 1], (T1, N)))
    F = np.stack(feats, axis=2).astype(np.float64)
    pool.cache[key] = F
    return F


class MLModel(LottoModel):
    """Pooled, sigmoid-calibrated gradient-boosting classifier for P(number in next draw).

    One classifier per pool is trained on (draw, number) rows using causal lag
    features; calibration folds are expanding-window, split on draw boundaries.
    Refit every ``refit_every`` draws during backtesting (predictions between
    refits still use up-to-date features, never future draws).
    """

    name = "ml"
    description = "Calibrated gradient boosting on lag features"

    def __init__(self, half_life: float = 50.0, refit_every: int = 25, seed: int = 0,
                 warmup: int = 20, min_train_draws: int = 60) -> None:
        super().__init__()
        self.half_life, self.refit_every, self.seed = half_life, refit_every, seed
        self.warmup, self.min_train_draws = warmup, min_train_draws
        self._clf: dict[str, Any] = {}
        self._fit_t: dict[str, int] = {}

    def _train(self, pool: PoolState, view: HistoryView) -> Optional[Any]:
        t = view.t
        n_draws = t - self.warmup
        if n_draws < self.min_train_draws:
            return None
        F = build_ml_features(pool, view.history.structure, self.half_life)
        N, nf = pool.size, F.shape[2]
        Xtr = F[self.warmup:t].reshape(-1, nf)
        ytr = pool.X[self.warmup:t].reshape(-1)
        edges = (np.linspace(0, n_draws, 5).astype(int)) * N
        folds = [(np.arange(0, edges[i]), np.arange(edges[i], edges[i + 1])) for i in range(1, 4)]
        clf = CalibratedClassifierCV(_make_classifier(self.seed), method="sigmoid", cv=folds)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            clf.fit(Xtr, ytr)
        return clf

    def fit(self, view: HistoryView) -> MLModel:
        for pool in (view.main, view.super_):
            last = self._fit_t.get(pool.name)
            if last is None or view.t - last >= self.refit_every or view.t < last:
                self._clf[pool.name] = self._train(pool, view)
                self._fit_t[pool.name] = view.t
        return super().fit(view)  # type: ignore[return-value]

    def _pool_weights(self, pool: PoolState, view: HistoryView) -> np.ndarray:
        clf = self._clf.get(pool.name)
        if clf is None:  # not enough history yet: fall back to smoothed frequency
            return pool.count[view.t] + 1.0
        F = build_ml_features(pool, view.history.structure, self.half_life)[view.t]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return clf.predict_proba(F)[:, 1]


class EnsembleModel(LottoModel):
    """Convex blend of component inclusion probabilities.

    Weights are learned (by :func:`fit_mixture_weights`) from the components'
    *out-of-sample* walk-forward predictions, never from in-sample fit.
    """

    name = "ensemble"
    description = "Backtest-weighted blend of the models above"

    def __init__(self, components: list[LottoModel], w_main: Optional[np.ndarray] = None,
                 w_super: Optional[np.ndarray] = None) -> None:
        super().__init__()
        self.components = components
        m = len(components)
        self.w_main = np.full(m, 1 / m) if w_main is None else np.asarray(w_main)
        self.w_super = np.full(m, 1 / m) if w_super is None else np.asarray(w_super)

    @staticmethod
    def combine(preds: list[Prediction], w_main: np.ndarray, w_super: np.ndarray) -> Prediction:
        main = np.einsum("m,mn->n", w_main, np.stack([p.main for p in preds]))
        sup = np.einsum("m,mn->n", w_super, np.stack([p.super_ for p in preds]))
        return Prediction(normalize_inclusion(main, K_MAIN), sup / sup.sum())

    def fit(self, view: HistoryView) -> EnsembleModel:
        preds = [c.fit(view).predict_proba() for c in self.components]
        return self.fit_from_predictions(view, preds)

    def fit_from_predictions(self, view: HistoryView, preds: list[Prediction]) -> EnsembleModel:
        self._view = view
        self._pred = self.combine(preds, self.w_main, self.w_super)
        return self

    def _pool_weights(self, pool: PoolState, view: HistoryView) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError("EnsembleModel combines component predictions directly")


def fit_mixture_weights(P: np.ndarray, Y: np.ndarray, kind: str, l2: float = 0.05) -> np.ndarray:
    """Softmax-parameterised convex weights maximising out-of-sample log-likelihood.

    P: (D, M, N) component probabilities over D draws. Y: (D, N) 0/1 for
    ``kind='bernoulli'`` (main balls) or (D,) winning index for ``'categorical'``.
    A small L2 penalty on the logits shrinks towards equal weights.
    """
    D, M, _ = P.shape

    def nll(theta: np.ndarray) -> float:
        w = np.exp(theta - theta.max())
        w /= w.sum()
        p = np.clip(np.einsum("m,dmn->dn", w, P), EPS, 1 - EPS)
        if kind == "bernoulli":
            ll = (Y * np.log(p) + (1 - Y) * np.log1p(-p)).sum()
        else:
            ll = np.log(p[np.arange(D), Y] / p.sum(axis=1)).sum()
        return -ll / D + l2 * float(theta @ theta)

    res = optimize.minimize(nll, np.zeros(M), method="L-BFGS-B")
    w = np.exp(res.x - res.x.max())
    return w / w.sum()


def build_components(cfg: Config) -> list[LottoModel]:
    """Instantiate the non-ensemble models (baseline first)."""
    models: list[LottoModel] = [
        UniformModel(), FrequencyModel(), RecencyModel(cfg.half_life), GapModel(), MarkovModel(),
    ]
    if cfg.use_ml and HAVE_SKLEARN:
        models.append(MLModel(cfg.half_life, cfg.ml_refit_every, cfg.seed))
    return models


# --------------------------------------------------------------------------- #
# Statistics helpers
# --------------------------------------------------------------------------- #
def bh_adjust(pvalues: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values (FDR)."""
    p = np.asarray(pvalues, dtype=float)
    n = len(p)
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * n / np.arange(1, n + 1)
    adjusted = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.clip(adjusted, 0, 1)
    return out


def paired_test(diff: np.ndarray, rng: np.random.Generator, n_perm: int, n_boot: int) -> dict[str, float]:
    """Mean of paired differences, bootstrap 95% CI and one-sided sign-flip permutation p-value (H1: mean > 0)."""
    diff = np.asarray(diff, dtype=float)
    D = len(diff)
    obs = diff.mean()
    if D == 0 or np.allclose(diff, 0):
        return {"mean": float(obs) if D else math.nan, "ci_lo": 0.0, "ci_hi": 0.0, "p": 1.0}
    ge = 0
    for start in range(0, n_perm, 1000):
        m = min(1000, n_perm - start)
        signs = rng.choice((-1.0, 1.0), size=(m, D))
        ge += int(((signs * diff).mean(axis=1) >= obs - 1e-12).sum())
    p = (ge + 1) / (n_perm + 1)
    boots = np.empty(n_boot)
    for start in range(0, n_boot, 1000):
        m = min(1000, n_boot - start)
        boots[start:start + m] = diff[rng.integers(0, D, size=(m, D))].mean(axis=1)
    lo, hi = np.quantile(boots, [0.025, 0.975])
    return {"mean": float(obs), "ci_lo": float(lo), "ci_hi": float(hi), "p": float(p)}


def _ljung_box(x: np.ndarray, lags: int) -> tuple[float, float]:
    if HAVE_STATSMODELS:
        res = acorr_ljungbox(x, lags=[lags], return_df=True)
        return float(res["lb_stat"].iloc[0]), float(res["lb_pvalue"].iloc[0])
    x = x - x.mean()
    n = len(x)
    denom = (x * x).sum()
    acf = np.array([(x[k:] * x[:-k]).sum() / denom for k in range(1, lags + 1)])
    q = n * (n + 2) * np.sum(acf ** 2 / (n - np.arange(1, lags + 1)))
    return float(q), float(stats.chi2.sf(q, lags))


def _runs_test(x: np.ndarray) -> tuple[float, float]:
    """Wald-Wolfowitz runs test above/below the median (values equal to the median dropped)."""
    med = np.median(x)
    s = x[x != med] > med
    n1, n2 = int(s.sum()), int((~s).sum())
    if n1 == 0 or n2 == 0:
        return math.nan, 1.0
    runs = 1 + int((s[1:] != s[:-1]).sum())
    mu = 2 * n1 * n2 / (n1 + n2) + 1
    var = 2 * n1 * n2 * (2 * n1 * n2 - n1 - n2) / ((n1 + n2) ** 2 * (n1 + n2 - 1))
    z = (runs - mu) / math.sqrt(var)
    return float(z), float(2 * stats.norm.sf(abs(z)))


def randomness_tests(hist: DrawHistory, alpha: float) -> pd.DataFrame:
    """The required family of randomness tests, with Benjamini-Hochberg correction."""
    T = hist.T
    rows = []
    Nm, Ns = hist.main.size, hist.super_.size

    counts = hist.main.X.sum(axis=0)
    chi, _ = stats.chisquare(counts)
    # Five balls are drawn without replacement, so each count has variance T*p*(1-p) with p=5/N
    # rather than the multinomial's; rescale by (N-1)/(N-5) to restore the chi-square reference.
    chi_adj = chi * (Nm - 1) / (Nm - K_MAIN)
    rows.append(("Chi-square uniformity: main balls", f"chi2={chi_adj:.1f}, df={Nm - 1}",
                 float(stats.chi2.sf(chi_adj, Nm - 1)),
                 "Are some main numbers drawn more often than chance allows?"))
    s_counts = hist.super_.X.sum(axis=0)
    chi_s, p_s = stats.chisquare(s_counts)
    rows.append(("Chi-square uniformity: Super Ball", f"chi2={chi_s:.1f}, df={Ns - 1}", float(p_s),
                 "Are some Super Ball numbers drawn more often than chance allows?"))

    sums = hist.main_values.sum(axis=1).astype(float)
    lags = int(max(1, min(10, T // 5)))
    q, p_lb = _ljung_box(sums, lags)
    rows.append((f"Ljung-Box autocorrelation of draw sums (lags 1-{lags})", f"Q={q:.1f}", p_lb,
                 "Does a draw's sum carry information about the next sums?"))
    z, p_runs = _runs_test(sums)
    rows.append(("Runs test on draw sums (above/below median)", f"z={z:.2f}", p_runs,
                 "Do high/low sums cluster or alternate more than chance?"))

    Xm = hist.main.X
    repeats = (Xm[1:] * Xm[:-1]).sum(axis=1)
    hg = stats.hypergeom(Nm, K_MAIN, K_MAIN)
    n_pairs = len(repeats)
    z_rep = (repeats.sum() - n_pairs * hg.mean()) / math.sqrt(n_pairs * hg.var()) if n_pairs else 0.0
    rows.append(("Repeat rate: main balls from previous draw",
                 f"observed {repeats.mean():.3f}/draw vs expected {hg.mean():.3f}, z={z_rep:.2f}",
                 float(2 * stats.norm.sf(abs(z_rep))),
                 "Does the previous draw's set predict the next draw?"))
    sup = hist.super_values
    s_rep = int((sup[1:] == sup[:-1]).sum())
    p_srep = float(stats.binomtest(s_rep, max(len(sup) - 1, 1), 1 / Ns).pvalue) if len(sup) > 1 else 1.0
    rows.append(("Repeat rate: Super Ball from previous draw",
                 f"observed {s_rep}/{len(sup) - 1} vs expected {(len(sup) - 1) / Ns:.1f}", p_srep,
                 "Does the previous Super Ball predict the next?"))

    df = pd.DataFrame(rows, columns=["test", "statistic", "p_value", "question"])
    df["p_bh"] = bh_adjust(df["p_value"].to_numpy())
    df["significant"] = df["p_bh"] < alpha
    df["interpretation"] = np.where(
        df["significant"],
        "Evidence of non-randomness after correction (check data quality first).",
        "Consistent with random, independent draws.",
    )
    return df


# --------------------------------------------------------------------------- #
# EDA
# --------------------------------------------------------------------------- #
def structural_features(mains: np.ndarray, lo: int, hi: int) -> pd.DataFrame:
    """Per-draw structure: sum, odd/even, low/high, consecutive pairs, range."""
    mid = (lo + hi) / 2
    odd = (mains % 2 == 1).sum(axis=1)
    low = (mains <= mid).sum(axis=1)
    srt = np.sort(mains, axis=1)
    return pd.DataFrame({
        "sum": mains.sum(axis=1), "odd": odd, "even": K_MAIN - odd, "low": low, "high": K_MAIN - low,
        "consecutive_pairs": (np.diff(srt, axis=1) == 1).sum(axis=1), "range": srt[:, -1] - srt[:, 0],
    })


def _empirical_mean_gap(x: np.ndarray) -> float:
    idx = np.flatnonzero(x)
    return float(np.diff(idx).mean() - 1) if len(idx) > 1 else math.nan


def number_table(pool: PoolState, T: int, half_life: float) -> pd.DataFrame:
    """Frequency, recency-weighted frequency and gap statistics per number."""
    counts = pool.X.sum(axis=0)
    expected = T * pool.base_rate
    sd = math.sqrt(T * pool.base_rate * (1 - pool.base_rate))
    rec = pool.recency(half_life)[T]
    d = 0.5 ** (1.0 / half_life)
    rec_expected = (1 - d ** T) / (1 - d) * pool.base_rate
    avg_gap = np.array([_empirical_mean_gap(pool.X[:, k]) for k in range(pool.size)])
    cur_gap = pool.gap[T]
    return pd.DataFrame({
        "number": np.arange(pool.size) + pool.lo,
        "count": counts.astype(int),
        "expected": expected,
        "z_score": (counts - expected) / sd if sd > 0 else 0.0,
        "recency_weighted": rec,
        "recency_expected": rec_expected,
        "current_gap": cur_gap.astype(int),
        "avg_gap": avg_gap,
        "overdue_ratio": (cur_gap + 1) / (avg_gap + 1),
    })


def jackpot_analysis(df: pd.DataFrame, feats: pd.DataFrame, alpha: float) -> pd.DataFrame:
    """Exploratory Spearman correlations between jackpot size and draw structure."""
    jp = df["jackpot"].to_numpy(dtype=float)
    ok = np.isfinite(jp)
    rows = []
    if ok.sum() >= 10 and np.unique(jp[ok]).size > 1:
        for col in ["sum", "range", "odd", "consecutive_pairs"]:
            rho, p = stats.spearmanr(jp[ok], feats[col].to_numpy()[ok])
            rows.append((col, float(rho), float(p)))
    out = pd.DataFrame(rows, columns=["feature", "spearman_rho", "p_value"])
    if not out.empty:
        out["p_bh"] = bh_adjust(out["p_value"].to_numpy())
        out["significant"] = out["p_bh"] < alpha
    return out


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
@dataclass
class BacktestResult:
    records: pd.DataFrame
    leaderboard: pd.DataFrame
    match_dist: pd.DataFrame
    weights_main: np.ndarray
    weights_super: np.ndarray
    component_names: list[str]
    n_draws: int
    n_sims: int
    any_significant: bool
    verdict: str
    n_comparisons: int


def _score(pred: Prediction, y_main: np.ndarray, s_idx: int) -> dict[str, float]:
    p = pred.main
    ps = pred.super_ / pred.super_.sum()
    onehot = np.zeros_like(ps)
    onehot[s_idx] = 1.0
    return {
        "main_ll": float((y_main * np.log(p) + (1 - y_main) * np.log1p(-p)).sum()),
        "main_brier": float(((p - y_main) ** 2).mean()),
        "super_ll": float(np.log(max(ps[s_idx], EPS))),
        "super_brier": float(((ps - onehot) ** 2).sum()),
    }


def run_backtest(hist: DrawHistory, cfg: Config, rng: np.random.Generator) -> Optional[BacktestResult]:
    """Walk-forward (expanding window) backtest of every model vs. the uniform baseline."""
    T = hist.T
    B = min(cfg.backtest_draws, T - cfg.min_train)
    if B <= 0:
        LOG.warning("Backtest skipped: need more than --min-train=%d draws (have %d).", cfg.min_train, T)
        return None
    if B < cfg.backtest_draws:
        LOG.info("Backtest window reduced to %d draws to keep %d training draws.", B, cfg.min_train)
    components = build_components(cfg)
    names = [m.name for m in components]
    M = len(components)
    Nm, Ns = hist.main.size, hist.super_.size
    P_main, P_super = np.zeros((B, M, Nm)), np.zeros((B, M, Ns))
    Y_main, Y_super = np.zeros((B, Nm)), np.zeros(B, dtype=int)
    w_main, w_super = np.full(M, 1 / M), np.full(M, 1 / M)
    ens_warmup, records, topn = 20, [], []
    t0 = time.perf_counter()

    for i, t in enumerate(range(T - B, T)):
        view = hist.view(t)
        constraint = StructureConstraint.from_values(view.main_values) if cfg.constrain else None
        y_main = hist.main.X[t]
        s_idx = int(hist.super_values[t] - hist.super_.lo)
        preds = {m.name: m.fit(view).predict_proba() for m in components}
        if i >= ens_warmup:
            w_main = fit_mixture_weights(P_main[:i], Y_main[:i], "bernoulli")
            w_super = fit_mixture_weights(P_super[:i], Y_super[:i], "categorical")
        preds["ensemble"] = EnsembleModel.combine(list(preds.values()), w_main, w_super)
        for j, n in enumerate(names):
            P_main[i, j], P_super[i, j] = preds[n].main, preds[n].super_
        Y_main[i], Y_super[i] = y_main, s_idx

        draw_seed = int(rng.integers(0, 2**63 - 1))
        for name, pred in preds.items():
            sim_rng = np.random.default_rng(draw_seed)  # common random numbers across models
            mains, supers = sample_tickets(pred, cfg.backtest_sims, sim_rng,
                                           hist.main.lo, hist.super_.lo, constraint)
            matches = y_main[mains - hist.main.lo].sum(axis=1)
            top = select_top_tickets(mains, supers, pred, hist.main.lo, hist.super_.lo, cfg.n_tickets)
            top_idx = top[[f"ball_{k}" for k in range(1, 6)]].to_numpy() - hist.main.lo
            top_matches = y_main[top_idx].sum(axis=1)
            topn.append({"model": name, **{str(k): int((top_matches == k).sum()) for k in range(K_MAIN + 1)}})
            records.append({
                "draw_index": t, "date": hist.frame["date"].iloc[t], "model": name,
                **_score(pred, y_main, s_idx),
                "matches": float(matches.mean()),
                "super_hit": float((supers == hist.super_values[t]).mean()),
                "topn_best_match": int(top_matches.max()),
                "topn_mean_match": float(top_matches.mean()),
                "topn_super_hits": int((top["super_ball"].to_numpy() == hist.super_values[t]).sum()),
            })
        if (i + 1) % max(1, B // 5) == 0:
            LOG.info("  backtest %d/%d draws (%.1fs)", i + 1, B, time.perf_counter() - t0)

    w_main = fit_mixture_weights(P_main, Y_main, "bernoulli")
    w_super = fit_mixture_weights(P_super, Y_super, "categorical")
    rec = pd.DataFrame(records)
    model_order = names + ["ensemble"]
    leaderboard, any_sig, n_comp = _leaderboard(rec, model_order, cfg, rng, Nm, Ns)
    md = pd.DataFrame(topn).groupby("model", sort=False).sum().reindex(model_order)
    md = md.div(md.sum(axis=1), axis=0)
    sig_models = leaderboard.loc[leaderboard["beats_baseline"] == "YES", "model"].tolist()
    if sig_models:
        verdict = (f"Model(s) {', '.join(sig_models)} scored significantly better than the uniform baseline "
                   f"after Benjamini-Hochberg correction across {n_comp} comparisons; treat this with "
                   "suspicion (see caveats) before reading it as real predictive power.")
    else:
        verdict = (f"No model beat the uniform random baseline: none of {n_comp} model-vs-baseline "
                   f"comparisons over {B} out-of-sample draws was significant after "
                   "Benjamini-Hochberg correction.")
    return BacktestResult(rec, leaderboard, md, w_main, w_super, names, B,
                          B * len(model_order) * cfg.backtest_sims, bool(sig_models), verdict, n_comp)


def _leaderboard(rec: pd.DataFrame, models: list[str], cfg: Config, rng: np.random.Generator,
                 Nm: int, Ns: int) -> tuple[pd.DataFrame, bool, int]:
    base = rec[rec["model"] == "baseline"].set_index("draw_index")
    rows, pvals = [], []
    for m in models:
        cur = rec[rec["model"] == m].set_index("draw_index")
        row: dict[str, Any] = {
            "model": m,
            "mean_matches": cur["matches"].mean(),
            "super_hit_rate": cur["super_hit"].mean(),
            "main_ll_per_draw": cur["main_ll"].mean(),
            "super_ll_per_draw": cur["super_ll"].mean(),
            "brier_main": cur["main_brier"].mean(),
            "brier_super": cur["super_brier"].mean(),
            "topn_mean_match": cur["topn_mean_match"].mean(),
            "topn_best_match_avg": cur["topn_best_match"].mean(),
        }
        for metric in BACKTEST_METRICS:
            if m == "baseline":
                row.update({f"d_{metric}": 0.0, f"d_{metric}_lo": 0.0, f"d_{metric}_hi": 0.0, f"p_{metric}": math.nan})
                continue
            res = paired_test((cur[metric] - base[metric]).to_numpy(), rng, cfg.permutations, cfg.bootstraps)
            row.update({f"d_{metric}": res["mean"], f"d_{metric}_lo": res["ci_lo"],
                        f"d_{metric}_hi": res["ci_hi"], f"p_{metric}": res["p"]})
            pvals.append((m, metric, res["p"]))
        rows.append(row)
    lb = pd.DataFrame(rows)
    adj = bh_adjust(np.array([p for *_, p in pvals]))
    for (m, metric, _), pa in zip(pvals, adj, strict=True):
        lb.loc[lb["model"] == m, f"padj_{metric}"] = pa
    padj_cols = [f"padj_{k}" for k in BACKTEST_METRICS]
    sig = (lb[padj_cols] < cfg.alpha).any(axis=1)
    lb["beats_baseline"] = np.where(lb["model"] == "baseline", "(control)", np.where(sig, "YES", "no"))
    lb.loc[lb["model"] == "baseline", padj_cols] = math.nan
    return lb, bool(sig[lb["model"] != "baseline"].any()), len(pvals)


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
C_BASE, C_MODEL, C_ACCENT, C_MUTED = "#8a8f98", "#2f6db5", "#d9822b", "#c9ccd1"


def _style(ax: plt.Axes) -> None:
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e6e7ea", linewidth=0.8)
    ax.set_axisbelow(True)


def chart_frequency(main_tbl: pd.DataFrame, super_tbl: pd.DataFrame, T: int, path: Path) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), gridspec_kw={"height_ratios": [2, 1]})
    for ax, tbl, title, k, N in (
        (axes[0], main_tbl, "Main balls", K_MAIN, len(main_tbl)),
        (axes[1], super_tbl, "Super Ball", 1, len(super_tbl)),
    ):
        p = k / N
        exp, sd = T * p, math.sqrt(T * p * (1 - p))
        dev = tbl["count"] - exp
        ax.axhspan(-2 * sd, 2 * sd, color=C_MUTED, alpha=0.45, zorder=0, label="±2 SD (chance range)")
        ax.bar(tbl["number"], dev, color=np.where(dev.abs() > 2 * sd, C_ACCENT, C_MODEL), width=0.75, zorder=2)
        ax.axhline(0, color="#222", linewidth=1)
        ax.set_title(f"{title}: times drawn minus uniform expectation ({exp:.0f} each)", loc="left", fontsize=11)
        ax.set_xlabel("Number")
        ax.set_ylabel("Count − expected")
        ax.set_xticks(tbl["number"])
        ax.tick_params(axis="x", labelsize=7)
        _style(ax)
        ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def chart_gaps(main_tbl: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(12, 4.5))
    over = main_tbl["overdue_ratio"] > 2
    ax.bar(main_tbl["number"], main_tbl["current_gap"], color=np.where(over, C_ACCENT, C_MODEL),
           width=0.75, label="current gap (draws since last seen)")
    ax.scatter(main_tbl["number"], main_tbl["avg_gap"], color="#222", s=14, zorder=3, label="average gap")
    ax.set_title("Main balls: current gap vs. average gap (orange = more than 2x average)", loc="left", fontsize=11)
    ax.set_xlabel("Number")
    ax.set_ylabel("Draws")
    ax.set_xticks(main_tbl["number"])
    ax.tick_params(axis="x", labelsize=7)
    _style(ax)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def chart_backtest(lb: pd.DataFrame, path: Path) -> None:
    sub = lb[lb["model"] != "baseline"].reset_index(drop=True)
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.2), sharey=True)
    y = np.arange(len(sub))
    for ax, metric in zip(axes, BACKTEST_METRICS, strict=True):
        d, lo, hi = sub[f"d_{metric}"], sub[f"d_{metric}_lo"], sub[f"d_{metric}_hi"]
        colors = np.where(sub[f"padj_{metric}"] < 0.05, C_ACCENT, C_MODEL)
        ax.errorbar(d, y, xerr=[d - lo, hi - d], fmt="none", ecolor=C_BASE, elinewidth=1.5, capsize=3)
        ax.scatter(d, y, color=colors, zorder=3, s=36)
        ax.axvline(0, color="#222", linewidth=1)
        ax.set_title(f"Δ {BACKTEST_METRICS[metric]}\nvs. baseline (95% CI)", loc="left", fontsize=10)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="x", color="#e6e7ea", linewidth=0.8)
        ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(5))
    axes[0].set_yticks(y)
    axes[0].set_yticklabels(sub["model"])
    axes[0].invert_yaxis()
    fig.text(0.01, 0.01, "Right of zero = better than random. Orange = significant after BH correction.",
             fontsize=8, color="#555")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def chart_sums(hist_sums: np.ndarray, sim_sums: np.ndarray, uniform_sums: np.ndarray,
               model_name: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.5))
    lo, hi = min(hist_sums.min(), sim_sums.min()), max(hist_sums.max(), sim_sums.max())
    bins = np.linspace(lo, hi, 40)
    ax.hist(hist_sums, bins=bins, density=True, color=C_MUTED, label=f"historical (n={len(hist_sums)})")
    ax.hist(sim_sums, bins=bins, density=True, histtype="step", linewidth=2, color=C_MODEL,
            label=f"simulated: {model_name} (n={len(sim_sums):,})")
    ax.hist(uniform_sums, bins=bins, density=True, histtype="step", linewidth=1.2, color="#222",
            linestyle="--", label="uniform reference")
    ax.set_title("Sum of main balls: simulated vs. historical", loc="left", fontsize=11)
    ax.set_xlabel("Sum of the five main balls")
    ax.set_ylabel("Density")
    _style(ax)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def chart_pairs(co: np.ndarray, lo: int, expected: float, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 7))
    rel = co / expected if expected > 0 else co
    np.fill_diagonal(rel, np.nan)
    im = ax.imshow(rel, cmap="RdBu_r", vmin=0, vmax=2, origin="lower")
    ticks = np.arange(0, co.shape[0], max(1, co.shape[0] // 12))
    ax.set_xticks(ticks)
    ax.set_xticklabels(ticks + lo)
    ax.set_yticks(ticks)
    ax.set_yticklabels(ticks + lo)
    ax.set_title("Pair co-occurrence / expected (1.0 = chance)", loc="left", fontsize=11)
    fig.colorbar(im, ax=ax, shrink=0.8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _md_table(df: pd.DataFrame, floatfmt: str = "{:.6g}") -> str:
    def fmt(v: Any) -> str:
        if isinstance(v, (float, np.floating)):
            return "" if np.isnan(v) else floatfmt.format(v)
        if isinstance(v, pd.Timestamp):
            return v.strftime("%Y-%m-%d")
        return str(v)

    head = "| " + " | ".join(map(str, df.columns)) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    body = ["| " + " | ".join(fmt(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, sep, *body])


def _fmt_ci(row: pd.Series, metric: str, scale: float = 1.0, digits: int = 4) -> str:
    return (f"{row[f'd_{metric}'] * scale:+.{digits}f} "
            f"[{row[f'd_{metric}_lo'] * scale:+.{digits}f}, {row[f'd_{metric}_hi'] * scale:+.{digits}f}]")


def leaderboard_display(lb: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({
        "model": lb["model"],
        "mean matches": lb["mean_matches"].round(4),
        "Δ matches [95% CI]": [_fmt_ci(r, "matches") for _, r in lb.iterrows()],
        "p_adj (matches)": lb["padj_matches"],
        "main LL/draw": lb["main_ll_per_draw"].round(3),
        "Δ main LL [95% CI]": [_fmt_ci(r, "main_ll", digits=3) for _, r in lb.iterrows()],
        "p_adj (main LL)": lb["padj_main_ll"],
        "super hit rate": lb["super_hit_rate"].round(4),
        "Δ super LL [95% CI]": [_fmt_ci(r, "super_ll", digits=3) for _, r in lb.iterrows()],
        "p_adj (super LL)": lb["padj_super_ll"],
        "Brier main": lb["brier_main"].round(5),
        "Brier super": lb["brier_super"].round(5),
        "beats baseline?": lb["beats_baseline"],
    })
    return out


def disclaimer(verdict: str) -> str:
    return f"{DISCLAIMER_CORE} Backtest result: {verdict}"


@dataclass
class RunResult:
    results_path: Path
    report_path: Path
    backtest: Optional[BacktestResult]
    tickets: pd.DataFrame
    prep: PrepReport
    total_sims: int
    verdict: str


def write_report(path: Path, cfg: Config, hist: DrawHistory, rep: PrepReport, tests: pd.DataFrame,
                 main_tbl: pd.DataFrame, super_tbl: pd.DataFrame, feats: pd.DataFrame,
                 uniform_feats: pd.DataFrame, odd_tbl: pd.DataFrame, decade_tbl: pd.DataFrame,
                 top_pairs: pd.DataFrame, jackpot: pd.DataFrame, bt: Optional[BacktestResult],
                 tickets: pd.DataFrame, model_name: str, constraint: Optional[StructureConstraint],
                 verdict: str, total_sims: int, charts: list[str]) -> None:
    df = hist.frame
    (mlo, mhi), (slo, shi) = rep.main_range, rep.super_range
    L: list[str] = ["# Super Lotto analysis report", ""]
    L += ["> " + disclaimer(verdict), ""]
    L += ["## 1. Data summary", "",
          f"- Source: `{cfg.input}`",
          f"- Draws analysed: **{hist.T}** ({df['date'].min():%Y-%m-%d} to {df['date'].max():%Y-%m-%d})",
          f"- Main balls: {K_MAIN} from {mlo}-{mhi} ({rep.main_range_source}); "
          f"Super Ball: 1 from {slo}-{shi} ({rep.super_range_source})",
          f"- Rows read: {rep.rows_read}; blank rows ignored: {rep.blank_rows}; invalid rows dropped: "
          f"{len(rep.bad_rows)}; exact duplicate draws dropped: {rep.duplicates_dropped}",
          f"- Jackpot values parsed: {int(df['jackpot'].notna().sum())} (unparseable: {rep.jackpot_unparsed})", ""]
    if rep.bad_rows:
        L += ["Dropped rows:", "", _md_table(pd.DataFrame(rep.bad_rows, columns=["excel_row", "reason"])), ""]
    for w in rep.warnings:
        L.append(f"- ⚠ {w}")
    L += ["", "## 2. Randomness tests", "",
          "p-values are adjusted with **Benjamini-Hochberg** across the family of tests below "
          f"(α = {cfg.alpha}). The main-ball chi-square is rescaled by (N-1)/(N-5) because balls are "
          "drawn without replacement within a draw.", "",
          _md_table(tests[["test", "statistic", "p_value", "p_bh", "interpretation"]]), ""]
    n_sig = int(tests["significant"].sum())
    L.append("**Plain English:** " + (
        "none of the tests finds evidence that the draws deviate from independent, uniform randomness."
        if n_sig == 0 else
        f"{n_sig} test(s) flag a deviation from randomness. Before reading anything into it, check for "
        "data-entry errors, changes in game format (number range) over time, or a short history."))
    L += ["", "## 3. Exploratory analysis", "",
          "### Number frequency (main balls)", "",
          f"Expected count per number under uniformity: {main_tbl['expected'].iloc[0]:.1f}. "
          "Most and least frequent:", "",
          _md_table(pd.concat([main_tbl.nlargest(5, "count"), main_tbl.nsmallest(5, "count")])
                    [["number", "count", "expected", "z_score", "recency_weighted", "current_gap",
                      "avg_gap", "overdue_ratio"]]), "",
          f"With {len(main_tbl)} numbers, a handful with |z| > 2 is expected by chance alone "
          f"(~{0.0455 * len(main_tbl):.1f} on average).", "",
          "### Super Ball frequency", "",
          _md_table(super_tbl[["number", "count", "expected", "z_score", "recency_weighted",
                               "current_gap", "avg_gap", "overdue_ratio"]]), "",
          f"### Most overdue main numbers (recency half-life = {cfg.half_life:g} draws)", "",
          _md_table(main_tbl.nlargest(8, "overdue_ratio")[["number", "current_gap", "avg_gap", "overdue_ratio"]]), "",
          "\"Overdue\" is descriptive only: under independence, a number's chance is the same no matter how "
          "long since it last appeared (the gap model's calibration in the backtest tests exactly this).", "",
          "### Structural features per draw (historical vs. uniform reference)", ""]
    struct = pd.DataFrame({
        "feature": feats.columns,
        "hist_mean": feats.mean().values, "uniform_mean": uniform_feats.mean().values,
        "hist_sd": feats.std().values, "uniform_sd": uniform_feats.std().values,
        "hist_p5": feats.quantile(0.05).values, "hist_p95": feats.quantile(0.95).values,
    })
    L += [_md_table(struct), "", "Odd-number count per draw (observed vs. hypergeometric expectation):", "",
          _md_table(odd_tbl), "", "Decade distribution of main balls:", "", _md_table(decade_tbl), "",
          "### Most frequent pairs", "", _md_table(top_pairs), "",
          f"Expected co-occurrences per pair: {top_pairs['expected'].iloc[0]:.2f}. With "
          f"{len(main_tbl) * (len(main_tbl) - 1) // 2} pairs, the top few will look 'hot' by chance.", "",
          "### Jackpot size vs. outcomes (exploratory only)", ""]
    if jackpot.empty:
        L.append("Not enough parseable jackpot values to analyse.")
    else:
        L += [_md_table(jackpot), "",
              "Spearman correlations with BH-adjusted p-values. Jackpot size is set by ticket sales and "
              "roll-overs, not by the balls, so no relationship is expected."]
    L += ["", "## 4. Backtest (walk-forward, expanding window)", ""]
    if bt is None:
        L.append("Backtest skipped (not enough draws). No claim about predictive power can be made.")
    else:
        L += [f"- Test draws: **{bt.n_draws}** (each predicted using only earlier draws); "
              f"{cfg.backtest_sims:,} simulated tickets per model per draw; top-{cfg.n_tickets} tickets scored.",
              "- Δ columns are paired differences vs. the uniform baseline (positive = better), with bootstrap "
              f"95% CIs ({cfg.bootstraps:,} resamples) and one-sided sign-flip permutation tests "
              f"({cfg.permutations:,} permutations), Benjamini-Hochberg corrected across all "
              f"{bt.n_comparisons} comparisons.",
              f"- Uniform expectation: {K_MAIN * K_MAIN / hist.main.size:.4f} main balls matched per ticket; "
              f"Super Ball hit rate {1 / hist.super_.size:.4f}.", "",
              _md_table(leaderboard_display(bt.leaderboard)), "",
              f"**Verdict:** {bt.verdict}", "",
              f"Match-count distribution of the top-{cfg.n_tickets} tickets per draw (share of tickets):", "",
              _md_table(bt.match_dist.reset_index().rename(columns={str(k): f"{k} match" for k in range(6)})), "",
              "Ensemble weights (learned from out-of-sample backtest predictions):", "",
              _md_table(pd.DataFrame({"component": bt.component_names, "weight_main": bt.weights_main,
                                      "weight_super": bt.weights_super})), ""]
    L += ["## 5. Simulated tickets", "",
          f"Model: **{model_name}**; {cfg.n_sims:,} simulated draws; seed {cfg.seed}; structural constraint: "
          f"{constraint.describe() if constraint else 'off'}.", "",
          "Tickets are the distinct simulated combinations with the highest model probability (ties broken by "
          "simulated frequency). `uniform_prob_ticket` is the chance of any single ticket under a fair draw.", "",
          _md_table(tickets), "",
          "## 6. Caveats", "",
          "- A fair lottery has no exploitable pattern; all models other than the baseline are expected to "
          "match or slightly underperform it out of sample (they fit noise).",
          "- Many models x metrics are compared; without multiple-testing correction some would look "
          "'significant' by chance. Even after correction, a single significant result should be replicated "
          "on later draws before it is believed.",
          "- The backtest only uses information available before each draw; ensemble weights at each step are "
          "fitted only on earlier backtest predictions.",
          "- Number ranges are inferred from the data; if the game format changed during the history, analyse "
          "each format period separately.",
          f"- Total simulated tickets this run: {total_sims:,}.", "",
          "## 7. Charts", "", *[f"- `{c}`" for c in charts], ""]
    path.write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def run_pipeline(cfg: Config) -> RunResult:
    """Full pipeline: load -> clean -> EDA -> tests -> backtest -> simulate -> outputs."""
    start = time.perf_counter()
    results_dir = cfg.results_dir or cfg.output_dir
    report_dir = cfg.report_dir or cfg.output_dir
    for folder in (results_dir, report_dir):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DataError(f"Cannot create output folder {folder}: {exc}") from exc
    results_path = results_dir / "predictions.xlsx"
    report_path = report_dir / "analysis_report.md"
    rng = np.random.default_rng(cfg.seed)
    assert cfg.input is not None

    raw = load_results(cfg.input, cfg.sheet)
    df, rep = prepare_draws(raw, cfg.main_max, cfg.super_max)
    hist = DrawHistory.from_frame(df, rep)
    (mlo, mhi), (slo, shi) = rep.main_range, rep.super_range
    LOG.info("Data summary: %d draws from %s to %s", hist.T, f"{df['date'].min():%Y-%m-%d}",
             f"{df['date'].max():%Y-%m-%d}")
    LOG.info("  main balls %d-%d (%s); Super Ball %d-%d (%s)", mlo, mhi, rep.main_range_source,
             slo, shi, rep.super_range_source)
    LOG.info("  rows read %d, blank ignored %d, invalid dropped %d, duplicates dropped %d",
             rep.rows_read, rep.blank_rows, len(rep.bad_rows), rep.duplicates_dropped)
    if hist.T < 30:
        raise DataError(f"Only {hist.T} valid draws; at least 30 are needed for any meaningful analysis.")

    # --- EDA ---------------------------------------------------------------
    main_tbl = number_table(hist.main, hist.T, cfg.half_life)
    super_tbl = number_table(hist.super_, hist.T, cfg.half_life)
    feats = structural_features(hist.main_values, mlo, mhi)
    ref_rng = np.random.default_rng([cfg.seed, 7])
    uni_pred = Prediction(np.full(hist.main.size, K_MAIN / hist.main.size),
                          np.full(hist.super_.size, 1 / hist.super_.size))
    uni_mains, _ = sample_tickets(uni_pred, 100_000, ref_rng, mlo, slo)
    uniform_feats = structural_features(uni_mains, mlo, mhi)
    n_odd = sum(1 for v in range(mlo, mhi + 1) if v % 2 == 1)
    hg = stats.hypergeom(hist.main.size, n_odd, K_MAIN)
    odd_counts = np.bincount(feats["odd"], minlength=K_MAIN + 1)
    odd_tbl = pd.DataFrame({"odd_count": np.arange(K_MAIN + 1), "observed": odd_counts,
                            "expected": hg.pmf(np.arange(K_MAIN + 1)) * hist.T})
    decades = hist.main_values // 10
    dec_ids = np.arange(mlo // 10, mhi // 10 + 1)
    pool_vals = np.arange(mlo, mhi + 1)
    decade_tbl = pd.DataFrame({
        "decade": [f"{max(d * 10, mlo)}-{min(d * 10 + 9, mhi)}" for d in dec_ids],
        "observed_share": [(decades == d).mean() for d in dec_ids],
        "expected_share": [(pool_vals // 10 == d).mean() for d in dec_ids],
    })
    co = hist.main.X.T @ hist.main.X
    pair_expected = hist.T * K_MAIN * (K_MAIN - 1) / (hist.main.size * (hist.main.size - 1))
    iu = np.triu_indices(hist.main.size, 1)
    pairs = pd.DataFrame({"a": iu[0] + mlo, "b": iu[1] + mlo, "count": co[iu].astype(int)})
    pairs["expected"] = pair_expected
    top_pairs = pairs.nlargest(10, "count").reset_index(drop=True)
    jackpot = jackpot_analysis(df, feats, cfg.alpha)

    # --- Randomness tests --------------------------------------------------
    tests = randomness_tests(hist, cfg.alpha)
    LOG.info("Randomness tests (Benjamini-Hochberg adjusted):")
    for _, r in tests.iterrows():
        LOG.info("  %-52s p=%.4f  p_bh=%.4f  -> %s", r["test"], r["p_value"], r["p_bh"], r["interpretation"])

    # --- Backtest ----------------------------------------------------------
    LOG.info("Walk-forward backtest: up to %d draws, %d sims per model per draw ...",
             cfg.backtest_draws, cfg.backtest_sims)
    bt = run_backtest(hist, cfg, rng) if cfg.backtest_draws > 0 else None
    if bt is not None:
        LOG.info("Backtest leaderboard:")
        for _, r in bt.leaderboard.iterrows():
            LOG.info("  %-10s matches=%.4f (Δ %+.4f, p_adj=%s)  ΔmainLL=%+.3f  superHit=%.4f  -> %s",
                     r["model"], r["mean_matches"], r["d_matches"],
                     "-" if pd.isna(r["padj_matches"]) else f"{r['padj_matches']:.3f}",
                     r["d_main_ll"], r["super_hit_rate"], r["beats_baseline"])
        verdict = bt.verdict
    else:
        verdict = "the backtest was not run, so no model has been shown to beat chance."

    # --- Final fit & simulation -------------------------------------------
    full = hist.view(hist.T)
    components = build_components(cfg)
    preds = {m.name: m.fit(full).predict_proba() for m in components}
    w_main = bt.weights_main if bt else np.full(len(components), 1 / len(components))
    w_super = bt.weights_super if bt else np.full(len(components), 1 / len(components))
    ensemble = EnsembleModel(components, w_main, w_super).fit_from_predictions(full, list(preds.values()))
    preds["ensemble"] = ensemble.predict_proba()
    model_name = cfg.model
    if model_name not in preds:
        LOG.warning("Model '%s' unavailable (missing dependency?); using 'ensemble' instead.", model_name)
        model_name = "ensemble"
    pred = preds[model_name]
    constraint = StructureConstraint.from_values(hist.main_values) if cfg.constrain else None
    LOG.info("Simulating %s draws from '%s'%s ...", f"{cfg.n_sims:,}", model_name,
             f" with constraint ({constraint.describe()})" if constraint else "")
    sim_rng = np.random.default_rng([cfg.seed, 1])
    mains, supers = sample_tickets(pred, cfg.n_sims, sim_rng, mlo, slo, constraint)
    tickets = select_top_tickets(mains, supers, pred, mlo, slo, cfg.n_tickets)
    tickets.insert(1, "model", model_name)
    tickets["uniform_prob_ticket"] = 1.0 / (math.comb(hist.main.size, K_MAIN) * hist.super_.size)

    # --- Outputs -----------------------------------------------------------
    charts = ["chart_number_frequency.png", "chart_gaps.png", "chart_simulated_sum_vs_historical.png",
              "chart_pair_cooccurrence.png"]
    chart_frequency(main_tbl, super_tbl, hist.T, report_dir / charts[0])
    chart_gaps(main_tbl, report_dir / charts[1])
    chart_sums(feats["sum"].to_numpy(), mains.sum(axis=1), uniform_feats["sum"].to_numpy(), model_name,
               report_dir / charts[2])
    chart_pairs(co.astype(float), mlo, pair_expected, report_dir / charts[3])
    if bt is not None:
        charts.append("chart_backtest_comparison.png")
        chart_backtest(bt.leaderboard, report_dir / charts[-1])

    prob_main = pd.DataFrame({"number": np.arange(hist.main.size) + mlo,
                              "uniform": K_MAIN / hist.main.size,
                              **{n: p.main for n, p in preds.items()}})
    prob_super = pd.DataFrame({"number": np.arange(hist.super_.size) + slo,
                               "uniform": 1 / hist.super_.size,
                               **{n: p.super_ for n, p in preds.items()}})
    with pd.ExcelWriter(results_path, engine="openpyxl") as xw:
        tickets.to_excel(xw, sheet_name="tickets", index=False)
        prob_main.to_excel(xw, sheet_name="main_number_probs", index=False)
        prob_super.to_excel(xw, sheet_name="super_ball_probs", index=False)
        if bt is not None:
            leaderboard_display(bt.leaderboard).to_excel(xw, sheet_name="backtest_leaderboard", index=False)
            bt.leaderboard.to_excel(xw, sheet_name="backtest_raw", index=False)
        tests.to_excel(xw, sheet_name="randomness_tests", index=False)
        main_tbl.to_excel(xw, sheet_name="eda_main_numbers", index=False)
        super_tbl.to_excel(xw, sheet_name="eda_super_ball", index=False)
        pd.DataFrame(co.astype(int), index=prob_main["number"], columns=prob_main["number"]).to_excel(
            xw, sheet_name="pair_cooccurrence")
        pd.DataFrame({"note": [disclaimer(verdict)]}).to_excel(xw, sheet_name="read_me", index=False)

    final_sims = cfg.n_sims
    total = final_sims + (bt.n_sims if bt else 0)
    write_report(report_path, cfg, hist, rep, tests, main_tbl, super_tbl, feats, uniform_feats,
                 odd_tbl, decade_tbl, top_pairs, jackpot, bt, tickets, model_name, constraint, verdict,
                 total, charts)

    # --- Console summary ---------------------------------------------------
    LOG.info("=" * 78)
    LOG.info("Top %d tickets (model: %s)", len(tickets), model_name)
    for _, r in tickets.iterrows():
        balls = " ".join(f"{int(r[f'ball_{i}']):2d}" for i in range(1, 6))
        LOG.info("  #%-2d  %s  + Super %2d   model P(main set)=%.3e (uniform %.3e)  P(ticket)=%.3e  sim count=%d",
                 r["rank"], balls, r["super_ball"], r["model_prob_main_set"],
                 1.0 / math.comb(hist.main.size, K_MAIN), r["model_prob_ticket"], r["sim_count"])
    LOG.info("-" * 78)
    LOG.info("Randomness tests significant after BH correction: %d of %d", int(tests["significant"].sum()), len(tests))
    LOG.info("DISCLAIMER: %s", disclaimer(verdict))
    LOG.info("-" * 78)
    if bt is not None:
        LOG.info("Simulations: backtest %s (%d draws x %d models x %s) + final %s = %s total",
                 f"{bt.n_sims:,}", bt.n_draws, len(bt.leaderboard), f"{cfg.backtest_sims:,}",
                 f"{final_sims:,}", f"{total:,}")
    else:
        LOG.info("Simulations: backtest 0 + final %s = %s total", f"{final_sims:,}", f"{total:,}")
    LOG.info("Results file written to: %s", results_path.resolve())
    LOG.info("Tests report + charts written to: %s", report_dir.resolve())
    LOG.info("Run time: %.1fs", time.perf_counter() - start)
    return RunResult(results_path.resolve(), report_path.resolve(), bt, tickets, rep, total, verdict)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def make_synthetic_draws(n: int, main_max: int, super_max: int, rng: np.random.Generator) -> pd.DataFrame:
    """Uniform-random draws in the input-file format, plus a few deliberately bad rows."""
    mains = np.argsort(rng.random((n, main_max)), axis=1)[:, :K_MAIN] + 1
    supers = rng.integers(1, super_max + 1, size=n)
    dates = pd.Timestamp("2010-01-02") + pd.to_timedelta(np.arange(n) * 3.5, unit="D").floor("D")
    jackpots = rng.integers(10, 400, size=n) * 1_000_000
    jp_text = [f"J${j:,.0f}" if i % 3 else f"${j / 1e6:.1f}M" for i, j in enumerate(jackpots)]
    df = pd.DataFrame({DATE_COL: dates, **{c: mains[:, i] for i, c in enumerate(MAIN_COLS)},
                       SUPER_COL: supers, JACKPOT_COL: jp_text})
    bad = pd.DataFrame([
        {DATE_COL: "2009-06-01", "Ball 1": 3, "Ball 2": 3, "Ball 3": 10, "Ball 4": 20, "Ball 5": 30,
         SUPER_COL: 2, JACKPOT_COL: "J$1,000,000"},                       # duplicate main ball
        {DATE_COL: "2009-06-04", "Ball 1": 1, "Ball 2": None, "Ball 3": 10, "Ball 4": 20, "Ball 5": 30,
         SUPER_COL: 2, JACKPOT_COL: "n/a"},                              # missing value
        {DATE_COL: "2009-06-08", "Ball 1": 1, "Ball 2": 2, "Ball 3": 10, "Ball 4": 20, "Ball 5": main_max + 1,
         SUPER_COL: 2, JACKPOT_COL: "J$2m"},                             # out of range (with override)
    ])
    df = pd.concat([df, bad, df.iloc[[5]]], ignore_index=True)           # + one exact duplicate draw
    return df.sample(frac=1.0, random_state=int(rng.integers(0, 2**31 - 1))).reset_index(drop=True)


def run_self_test(cfg: Config) -> int:
    """Synthetic uniform draws -> full pipeline -> check no model 'beats' chance (false-positive check)."""
    out = cfg.output_dir / "self_test"
    out.mkdir(parents=True, exist_ok=True)
    main_max, super_max, n_draws = 35, 10, 1000
    rng = np.random.default_rng([cfg.seed, 99])
    path = out / "synthetic_draws.xlsx"
    make_synthetic_draws(n_draws, main_max, super_max, rng).to_excel(path, index=False)
    LOG.info("SELF-TEST: wrote %d synthetic uniform draws (+4 bad/duplicate rows) to %s", n_draws, path)
    st_cfg = replace(cfg, input=path, sheet=None, output_dir=out, results_dir=None, report_dir=None,
                     n_sims=5_000, backtest_sims=500,
                     backtest_draws=min(cfg.backtest_draws, 150) or 150, main_max=main_max, super_max=super_max)
    res = run_pipeline(st_cfg)
    checks = {
        "3 invalid rows dropped": len(res.prep.bad_rows) == 3,
        "1 duplicate draw dropped": res.prep.duplicates_dropped == 1,
        "backtest ran": res.backtest is not None,
        "tickets produced": len(res.tickets) == st_cfg.n_tickets,
        "outputs written": all((out / f).exists() for f in ("predictions.xlsx", "analysis_report.md",
                                                           "chart_backtest_comparison.png")),
        "no model beats baseline (no false positive)": res.backtest is not None and not res.backtest.any_significant,
    }
    LOG.info("=" * 78)
    for name, ok in checks.items():
        LOG.info("SELF-TEST %-45s %s", name, "PASS" if ok else "FAIL")
    passed = all(checks.values())
    if not checks["no model beats baseline (no false positive)"] and res.backtest is not None:
        LOG.warning("A model 'beat' chance on purely random data. At alpha=%.2f this happens occasionally by "
                    "chance; re-run with another --seed to distinguish bad luck from a leak.", cfg.alpha)
    LOG.info("SELF-TEST %s", "PASSED" if passed else "FAILED")
    return 0 if passed else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _positive_int(text: str) -> int:
    v = int(text)
    if v <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return v


def _non_negative_int(text: str) -> int:
    v = int(text)
    if v < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Super Lotto analysis, walk-forward backtest and Monte Carlo ticket simulator.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input", type=Path,
                   help="Path to the results .xlsx or .csv file (relative paths use the CWD).")
    p.add_argument("--sheet", default=None, help="Sheet name for .xlsx input (default: first sheet).")
    p.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "output",
                   help="Output folder.")
    p.add_argument("--results-dir", type=Path, default=None,
                   help="Folder for predictions.xlsx (default: --output-dir).")
    p.add_argument("--report-dir", type=Path, default=None,
                   help="Folder for analysis_report.md and charts (default: --output-dir).")
    p.add_argument("--n-tickets", type=_positive_int, default=10, help="Number of tickets to output.")
    p.add_argument("--n-sims", type=_positive_int, default=100_000, help="Monte Carlo draws for the final simulation.")
    p.add_argument("--backtest-sims", type=_positive_int, default=2_000,
                   help="Simulated tickets per model per historical draw in the backtest.")
    p.add_argument("--backtest-draws", type=_non_negative_int, default=200,
                   help="Number of most-recent draws to backtest walk-forward (0 disables).")
    p.add_argument("--min-train", type=_positive_int, default=100, help="Minimum training draws before backtesting.")
    p.add_argument("--model", default="ensemble",
                   choices=["baseline", "frequency", "recency", "gap", "markov", "ml", "ensemble"],
                   help="Model used for the final simulation.")
    p.add_argument("--seed", type=int, default=42, help="Random seed (fully reproducible).")
    p.add_argument("--main-max", type=_positive_int, default=None, help="Override highest main-ball number.")
    p.add_argument("--super-max", type=_positive_int, default=None, help="Override highest Super Ball number.")
    p.add_argument("--half-life", type=float, default=50.0, help="Recency half-life in draws.")
    p.add_argument("--constrain", action="store_true",
                   help="Rejection-sample tickets to match historical sum range and odd/even balance.")
    p.add_argument("--ml-refit-every", type=_positive_int, default=25,
                   help="Refit the ML classifier every N backtest draws.")
    p.add_argument("--no-ml", action="store_true", help="Disable the ML model.")
    p.add_argument("--permutations", type=_positive_int, default=10_000, help="Permutations per significance test.")
    p.add_argument("--alpha", type=float, default=0.05, help="Significance level (after BH correction).")
    p.add_argument("--self-test", action="store_true",
                   help="Run the pipeline on synthetic uniform draws and check for false positives.")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    if args.half_life <= 0:
        LOG.error("--half-life must be positive")
        return 2
    use_ml = not args.no_ml
    if use_ml and not HAVE_SKLEARN:
        LOG.warning("scikit-learn not available: ML model disabled.")
        use_ml = False
    if use_ml:
        LOG.info("ML backend: %s", "LightGBM" if HAVE_LIGHTGBM else "scikit-learn HistGradientBoosting")
    if args.model == "ml" and not use_ml:
        LOG.warning("--model ml requested but the ML model is disabled; falling back to 'ensemble'.")
        args.model = "ensemble"
    cfg = Config(
        input=Path(args.input).expanduser().resolve() if args.input else None,
        sheet=args.sheet, output_dir=Path(args.output_dir).expanduser().resolve(),
        results_dir=Path(args.results_dir).expanduser().resolve() if args.results_dir else None,
        report_dir=Path(args.report_dir).expanduser().resolve() if args.report_dir else None,
        n_tickets=args.n_tickets, n_sims=args.n_sims, backtest_sims=args.backtest_sims,
        backtest_draws=args.backtest_draws, min_train=args.min_train, model=args.model, seed=args.seed,
        main_max=args.main_max, super_max=args.super_max, half_life=args.half_life, constrain=args.constrain,
        ml_refit_every=args.ml_refit_every, use_ml=use_ml, permutations=args.permutations, alpha=args.alpha,
    )
    try:
        if args.self_test:
            return run_self_test(cfg)
        if cfg.input is None:
            LOG.error("--input is required (or use --self-test).")
            return 2
        run_pipeline(cfg)
        return 0
    except DataError as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
