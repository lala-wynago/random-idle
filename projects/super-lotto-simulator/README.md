# 🎰 Super Lotto Simulator

> Analyse past Super Lotto draws, test whether they look random, backtest seven prediction models against pure
> chance, and simulate candidate tickets. It runs as a command-line tool or as a macOS drag-and-drop app.

> [!IMPORTANT]
> Lottery draws are designed to be independent and random, so historical patterns generally don't predict
> future draws. This tool is for entertainment and analysis. Its built-in backtest tells you, honestly,
> whether any model beat chance.

---

## 📚 Table of contents

- [✨ Features](#-features)
- [📋 Input format](#-input-format)
- [🔧 Installation](#-installation)
- [🚀 Usage](#-usage)
- [📤 Outputs](#-outputs)
- [🧠 How it works](#-how-it-works)
- [📈 Reading the results](#-reading-the-results)
- [🧪 Testing](#-testing)
- [🩺 Troubleshooting](#-troubleshooting)

---

## ✨ Features

- 📥 **Flexible input**: `.xlsx` or `.csv`. Number ranges are inferred from the data, and jackpot text such as
  `J$25,000,000` or `$25.5M` is cleaned automatically.
- 🧹 **Validation**: rows with missing values, duplicate balls or numbers out of range are dropped and listed,
  instead of crashing the run.
- 🔍 **Exploratory analysis**: hot and cold numbers, recency-weighted frequency, overdue gaps, sums, odd/even
  and low/high splits, consecutive pairs, pair co-occurrence, and jackpot correlations.
- 📐 **Randomness tests**: chi-square uniformity, Ljung-Box and runs tests on draw sums, and repeat-rate tests.
  All p-values are Benjamini-Hochberg corrected.
- 🤖 **Seven interchangeable models**, from a uniform baseline to calibrated gradient boosting and a
  backtest-weighted ensemble.
- 🔁 **Walk-forward backtest**: every model is scored only on draws it hadn't seen. Each model is compared with
  the uniform baseline using bootstrap confidence intervals and permutation tests.
- 🎟️ **Monte Carlo tickets**: valid 5 + 1 tickets, ranked by the model's exact probability.
- 🖱️ **Desktop app** with drag-and-drop, output folder pickers and tooltips on every control.

---

## 📋 Input format

The first sheet of an `.xlsx` file (or a `.csv` file) must have these headers. Their order doesn't matter, and
headers are matched without regard to case or extra spaces.

| Column | Example | Notes |
|---|---|---|
| `Draw Date` | `2024-03-16` | Any common date format, including day-first |
| `Ball 1` … `Ball 5` | `7` | The five main numbers, in any order |
| `Super Ball` | `4` | Drawn from its own pool |
| `Jackpot (JMD)` | `J$25,000,000` | Currency symbols, commas, `M`/`million` and `B`/`billion` are fine |

> [!NOTE]
> Keep your results files out of the repository. `.xlsx` and `.csv` files are git-ignored, and so is the
> `Past Results/` folder.

---

## 🔧 Installation

**macOS (recommended for the app):**

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/macos/). It includes Tk.
2. Double-click **`Set Up Lotto Simulator.command`** in this folder. It creates `.venv` here and installs
   everything the project needs.

**Manual setup (any platform):**

```bash
cd projects/super-lotto-simulator
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Optional: `pip install lightgbm` makes the ML model faster. On macOS it also needs `brew install libomp`.
Without LightGBM, the tool uses scikit-learn instead.

---

## 🚀 Usage

### 🖱️ Desktop app (macOS)

1. Double-click **`Lotto Simulator.app`**. It must stay in this folder; to launch it from the Dock or
   Applications, use an alias (**Make Alias** in Finder).
2. Drag an `.xlsx` or `.csv` file onto the window, or click the drop area to browse.
3. Optionally change the number of tickets, the model or the seed.
4. Under **Save to**, choose one folder for the results file and one for the tests report.
5. Click **Generate simulated results**. The tickets and the verdict on whether any model beat chance appear
   when it's done.

💡 Hover over any control for an explanation.

### ⌨️ Command line

```bash
# Check that everything works, using synthetic random draws
python super_lotto_simulator.py --self-test

# Typical run
python super_lotto_simulator.py --input results.xlsx --n-tickets 10 --n-sims 100000 \
    --backtest-sims 2000 --model ensemble --seed 42

# Faster exploratory run, with tickets matched to the historical sum and odd/even balance
python super_lotto_simulator.py --input results.csv --backtest-draws 100 --backtest-sims 500 --constrain

# Show every option
python super_lotto_simulator.py --help
```

| Option | Default | What it does |
|---|---|---|
| `--input PATH` | – | Results file (`.xlsx` or `.csv`). Relative paths use the current folder. |
| `--sheet NAME` | first sheet | Sheet to read from an `.xlsx` file |
| `--n-tickets N` | `10` | Number of tickets to output |
| `--model NAME` | `ensemble` | `baseline`, `frequency`, `recency`, `gap`, `markov`, `ml` or `ensemble` |
| `--seed N` | `42` | Random seed. The same seed and inputs give the same results. |
| `--n-sims N` | `100000` | Simulated draws for the final tickets |
| `--backtest-draws N` | `200` | Most recent draws to backtest (`0` turns the backtest off) |
| `--backtest-sims N` | `2000` | Simulated tickets per model per backtest draw |
| `--min-train N` | `100` | Minimum number of training draws before backtesting |
| `--constrain` | off | Keep only tickets with a typical sum and odd/even mix |
| `--main-max` / `--super-max` | inferred | Override the highest main ball / Super Ball number |
| `--half-life N` | `50` | Recency half-life, in draws |
| `--no-ml` | off | Turn off the ML model |
| `--output-dir DIR` | `./output` | Default folder for all outputs |
| `--results-dir DIR` | `--output-dir` | Folder for `predictions.xlsx` |
| `--report-dir DIR` | `--output-dir` | Folder for `analysis_report.md` and the charts |
| `--alpha A` | `0.05` | Significance level, after correction |
| `--self-test` | – | Run end-to-end on synthetic random draws and check that no model "beats" chance |

---

## 📤 Outputs

| File | Contents |
|---|---|
| `predictions.xlsx` | **tickets**, per-number probabilities for every model, backtest leaderboard, randomness tests, frequency and gap tables, pair co-occurrence matrix |
| `analysis_report.md` | Data summary, test results in plain English, backtest leaderboard and verdict, ensemble weights, tickets and caveats |
| `chart_number_frequency.png` | Each number's count minus the expected count, with the chance range |
| `chart_gaps.png` | Current gap vs. average gap for each main ball |
| `chart_backtest_comparison.png` | Each model's improvement over the baseline, with 95% confidence intervals |
| `chart_simulated_sum_vs_historical.png` | Distribution of simulated sums vs. historical and uniform sums |
| `chart_pair_cooccurrence.png` | Heatmap of how often pairs appear together, relative to chance |

---

## 🧠 How it works

```text
results file ─► clean & validate ─► explore + randomness tests
                                          │
                     walk-forward backtest (each model vs. uniform baseline)
                                          │
              ensemble weights ◄──────────┘
                                          │
              Monte Carlo simulation ─► top tickets ─► report, workbook, charts
```

| Model | Idea |
|---|---|
| `baseline` | Pure chance: every number equally likely. This is the control. |
| `frequency` | Historical frequency, Laplace-smoothed ("hot" numbers) |
| `recency` | Frequency with exponential decay, so recent draws count more |
| `gap` | "Overdue" numbers, with hit rates calibrated by how long since each number last appeared |
| `markov` | Which numbers tend to follow the previous draw's numbers |
| `ml` | Gradient boosting with sigmoid calibration on lag features; time-aware splits only |
| `ensemble` | A blend of the models above, weighted by out-of-sample backtest log-likelihood |

**The backtest.** For each test draw, every model is fitted only on earlier draws. It then predicts the next
draw and is scored on:
- mean balls matched
- Super Ball hit rate
- log-likelihood and Brier score
- how well its top tickets would have done

Differences from the baseline are tested with paired sign-flip permutation tests and bootstrap confidence
intervals, then Benjamini-Hochberg corrected. A unit test checks that changing future draws never changes a
model's prediction, so there is no leakage.

---

## 📈 Reading the results

- **"No model beat the uniform random baseline"** is the expected result for a fair lottery, and it means
  the tool is working.
- **If a model appears to beat the baseline**, be sceptical:
  - **Multiple comparisons:** 18 model × metric tests are run, so about 1 run in 20 can produce a false "win"
    even after correction. Check it on later draws and other seeds.
  - **Overfitting:** a gain only in balls matched, while log-likelihood and Brier score get worse, is noise.
  - **Data problems:** typos, a changed number range or mixed game formats can create fake "patterns".
  - **Real but useless:** even a genuine tiny edge wouldn't make the expected return positive.

---

## 🧪 Testing

```bash
# From the repository root, with .venv active
pip install -r requirements-dev.txt
python -m pytest projects/super-lotto-simulator/tests     # unit tests (~5 s)
python projects/super-lotto-simulator/super_lotto_simulator.py --self-test   # end-to-end (~30 s)
```

---

## 🩺 Troubleshooting

| Problem | Fix |
|---|---|
| "Lotto Simulator isn't set up yet" | Double-click `Set Up Lotto Simulator.command` in this folder |
| "Can't find the Lotto Simulator code" | Move the `.app` back into this folder, and use an alias to launch it from elsewhere |
| macOS asks for access to Documents | Click **Allow**. The app needs to read your results file and write its outputs. |
| Drag-and-drop doesn't work | Run setup again (it installs `tkinterdnd2`), or click the drop area to browse instead |
| Missing-column error | Check the headers against the [input format](#-input-format) |
| App closes straight away | See `~/Library/Logs/Lotto Simulator.log` |
