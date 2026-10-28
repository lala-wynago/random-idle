# 🎲 random-idle

> A public collection of small, self-contained side projects built in idle time. Each one is runnable,
> tested and documented on its own.

[![CI](https://github.com/lala-wynago/random-idle/actions/workflows/ci.yml/badge.svg)](https://github.com/lala-wynago/random-idle/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

---

## 📚 Table of contents

- [🧭 About](#-about)
- [📦 Projects](#-projects)
- [📁 Repository structure](#-repository-structure)
- [🔧 Installation](#-installation)
- [🚀 Quick start](#-quick-start)
- [🧪 Tests and checks](#-tests-and-checks)
- [🤝 Contributing](#-contributing)
- [📢 Disclaimer](#-disclaimer)
- [📄 License](#-license)

---

## 🧭 About

**random-idle** is where small experiments become finished projects. Every project:

- 📂 **lives in its own folder** under [`projects/`](projects), with its own README, dependencies and tests
- 🔁 **is reproducible**: fixed seeds and pinned minimum versions
- 🧪 **is checked automatically** by [GitHub Actions](.github/workflows/ci.yml) on every push and pull request
- 🔐 **keeps data out of the repo**: inputs and outputs stay on your machine

---

## 📦 Projects

| Project | What it does | Built with | Status |
|---|---|---|---|
| 🎰 [**Super Lotto Simulator**](projects/super-lotto-simulator) | Analyses past Super Lotto draws, tests whether they look random, backtests 7 prediction models against pure chance and simulates candidate tickets. Runs as a command-line tool or a macOS drag-and-drop app. | Python · pandas · SciPy · scikit-learn · Tkinter | ✅ Working |

---

## 📁 Repository structure

```text
random-idle/
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.yml               # structured bug-report form
│   │   ├── config.yml                   # private security-report link
│   │   └── feature_request.yml          # feature / new-project idea form
│   ├── workflows/
│   │   └── ci.yml                       # lint + unit tests + self-tests on every PR
│   └── pull_request_template.md
├── projects/
│   └── super-lotto-simulator/
│       ├── Lotto Simulator.app/         # macOS launcher (double-click in Finder)
│       ├── tests/
│       │   ├── conftest.py
│       │   └── test_simulator.py
│       ├── README.md                    # full project documentation
│       ├── Set Up Lotto Simulator.command  # one-time macOS setup
│       ├── lotto_gui.py                 # desktop app (drag-and-drop)
│       ├── requirements.txt             # runtime dependencies
│       └── super_lotto_simulator.py     # analysis engine + CLI
├── .gitignore                           # blocks data, outputs, secrets, virtualenvs
├── CONTRIBUTING.md                      # how to report bugs, suggest ideas, open PRs
├── README.md                            # you are here
├── pyproject.toml                       # shared ruff + pytest settings
└── requirements-dev.txt                 # dev tools: pytest, ruff
```

---

## 🔧 Installation

### Prerequisites

| Requirement | Notes |
|---|---|
| **Git** | To clone the repository |
| **Python 3.10+** | On macOS, use the [python.org installer](https://www.python.org/downloads/macos/): it includes Tk, which the desktop app needs |
| **macOS** | Only for the desktop app. The command-line tools run anywhere Python does (CI runs them on Linux). |

### 1. Clone the repository

```bash
git clone https://github.com/lala-wynago/random-idle.git
cd random-idle
```

### 2. Set up a project

Each project has its own virtual environment. For the Super Lotto Simulator:

**macOS, one click:** in Finder, open `projects/super-lotto-simulator` and double-click
**`Set Up Lotto Simulator.command`**.

**Any platform, manually:**

```bash
cd projects/super-lotto-simulator
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 🚀 Quick start

### 🎰 Super Lotto Simulator

**Your input** is an `.xlsx` or `.csv` file with these column headers:

```text
Draw Date | Ball 1 | Ball 2 | Ball 3 | Ball 4 | Ball 5 | Super Ball | Jackpot (JMD)
```

**🖱️ Desktop app (macOS)**

1. Double-click **`Lotto Simulator.app`** in `projects/super-lotto-simulator`.
2. Drag your results file onto the window.
3. Choose where to save the results file and the tests report.
4. Click **Generate simulated results**. Hover over any control for an explanation.

**⌨️ Command line**

```bash
cd projects/super-lotto-simulator
source .venv/bin/activate

# Check that everything works, using synthetic random draws (about 30 seconds)
python super_lotto_simulator.py --self-test

# Analyse your own results and simulate 10 tickets
python super_lotto_simulator.py --input ~/Downloads/results.xlsx --n-tickets 10 --seed 42

# Use a specific model and save outputs to custom folders
python super_lotto_simulator.py --input results.csv --model frequency \
    --results-dir ~/Desktop/lotto --report-dir ~/Desktop/lotto/report
```

**📤 What you get**

| File | Contents |
|---|---|
| `predictions.xlsx` | Tickets, each model's per-number probabilities, the backtest leaderboard and the test results |
| `analysis_report.md` | Data summary, randomness tests, backtest verdict and caveats |
| `chart_*.png` | Number frequency, gaps, backtest comparison, simulated vs. historical sums, pair heatmap |

➡️ For every option, how the models work and how to read the results, see the
**[project README](projects/super-lotto-simulator/README.md)**.

---

## 🧪 Tests and checks

From the repository root, with a project's virtual environment active:

```bash
pip install -r requirements-dev.txt   # once: pytest + ruff
ruff check .                          # lint
python -m pytest                      # unit tests for every project
```

The same checks, plus each project's end-to-end self-test, run in
[GitHub Actions](https://github.com/lala-wynago/random-idle/actions) on every push and pull request.

---

## 🤝 Contributing

Bug reports, ideas and pull requests are welcome! Please read **[CONTRIBUTING.md](CONTRIBUTING.md)** first.
It covers how to report bugs, suggest features, follow the coding standards and submit a pull request,
step by step.

- 🐛 [Report a bug](https://github.com/lala-wynago/random-idle/issues/new/choose)
- 💡 [Suggest a feature or project](https://github.com/lala-wynago/random-idle/issues/new/choose)
- 🔒 [Report a security issue privately](https://github.com/lala-wynago/random-idle/security/advisories/new)

---

## 📢 Disclaimer

The projects here are for learning and entertainment. In particular, lottery draws are designed to be
independent and random: the Super Lotto Simulator's backtest is there to show whether any pattern *actually*
beats chance, and on fair draws it shouldn't. Nothing in this repository is financial advice or a way to
make money.

---

## 📄 License

No license has been chosen yet. Until a `LICENSE` file is added, default copyright applies: you're
welcome to read and fork the code on GitHub, but please ask before reusing it elsewhere.
