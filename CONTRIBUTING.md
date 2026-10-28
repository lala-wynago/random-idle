# Contributing to random-idle

Thanks for your interest in contributing! 🎉

**random-idle** is a collection of small, self-contained side projects built in spare time. Bug reports, ideas,
documentation fixes and code are all welcome. Because this is maintained in spare time, replies are best-effort:
please be patient, and a short ping after a week of silence is fine.

## 📚 Table of contents

- [📜 Ground rules](#-ground-rules)
- [🐛 Reporting bugs](#-reporting-bugs)
- [🔒 Reporting security issues](#-reporting-security-issues)
- [💡 Suggesting features](#-suggesting-features)
- [🧰 Development setup](#-development-setup)
- [📏 Coding standards](#-coding-standards)
- [🔀 Submitting a pull request](#-submitting-a-pull-request)
- [🆕 Adding a new project](#-adding-a-new-project)
- [📄 License](#-license)

---

## 📜 Ground rules

- **Be kind.** Assume good intent, keep feedback about the code and not the person, and help newcomers.
  Harassment or personal attacks aren't tolerated and may be removed.
- **Discuss in the open.** Use issues and pull requests, so others can learn from the conversation. The
  only exception is security issues (see below).
- **Never commit data, secrets or personal information.** That includes draw-result spreadsheets, generated
  outputs, API keys and tokens, and absolute paths from your machine. The `.gitignore` blocks the common
  cases, but please double-check `git status` before you commit.
- **Keep changes focused.** One problem or feature per issue and per pull request.

---

## 🐛 Reporting bugs

### Before you report

1. **Search [existing issues](https://github.com/lala-wynago/random-idle/issues?q=is%3Aissue)**, including
   closed ones. If you find a match, add a 👍 or a comment with new details instead of opening a duplicate.
2. **Update to the latest `main`.** The bug may already be fixed.
3. **Try to reproduce it with synthetic data.** For example, the Super Lotto Simulator has a built-in check:

   ```bash
   python super_lotto_simulator.py --self-test
   ```

### How to report

Open a [**🐛 Bug report**](https://github.com/lala-wynago/random-idle/issues/new/choose) and fill in the
form. A good report includes:

| Include | Example |
|---|---|
| **Project** | `super-lotto-simulator` |
| **What happened vs. what you expected** | "The app crashed after I dropped a CSV" vs. "Tickets are generated" |
| **Steps to reproduce** | Numbered, starting from a fresh clone if possible |
| **Error output / logs** | Paste the text, not a screenshot. The GUI log is in `~/Library/Logs/Lotto Simulator.log`. |
| **Commit** | Output of `git rev-parse --short HEAD` |
| **Environment** | OS and version, chip (Apple Silicon / Intel), `python3 --version` |

> [!TIP]
> If the problem depends on an input file, share a **few anonymised or made-up rows** that trigger it,
> not your full data file.

---

## 🔒 Reporting security issues

Please **don't** open a public issue for security problems. Report them privately through the repository's
[**Security** tab → **Report a vulnerability**](https://github.com/lala-wynago/random-idle/security/advisories/new).
Include steps to reproduce and the impact you expect. You'll get a reply as soon as the maintainer is able.

---

## 💡 Suggesting features

1. **Check existing issues** to see if someone has already suggested it.
2. Open a [**💡 Feature request**](https://github.com/lala-wynago/random-idle/issues/new/choose) and
   describe:
   - **The problem.** What are you trying to do, and why is it hard today? Start here rather than with a
     solution.
   - **Your proposed solution.** What would you like to happen?
   - **The alternatives you considered**, and any workarounds you use now.
3. **Ideas for a whole new project are welcome too.** Choose "New project idea" in the form. Good fits are
   small, self-contained and finishable in a few sessions.

For anything larger than a small change, please **wait for a reply on the issue before writing code**. That
way your time isn't spent on something that doesn't fit the project.

---

## 🧰 Development setup

**Prerequisites:** Git and Python 3.10 or newer. On macOS, the
[python.org installer](https://www.python.org/downloads/macos/) includes Tk, which the desktop apps need.

```bash
# 1. Fork the repo on GitHub, then clone your fork
git clone https://github.com/<your-username>/random-idle.git
cd random-idle
git remote add upstream https://github.com/lala-wynago/random-idle.git

# 2. Create a virtual environment for the project you're working on
cd projects/super-lotto-simulator
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install runtime + development dependencies
pip install -r requirements.txt -r ../../requirements-dev.txt
```

Run the checks from the **repository root**, with the virtual environment active:

```bash
ruff check .            # lint and import order
python -m pytest        # unit tests for every project
```

---

## 📏 Coding standards

### 🐍 Python style

- **Python 3.10+**, following **PEP 8**, enforced by [ruff](https://docs.astral.sh/ruff/). The settings
  are in [`pyproject.toml`](pyproject.toml): 120-character lines and sorted imports. Run
  `ruff check . --fix` to fix most issues automatically.
- **Type hints** on all public functions and methods. Add `from __future__ import annotations` at the top of
  each module.
- **Docstrings** for every module, class and public function. Explain *what* it does and *why*, in a
  sentence or two.
- **Names that say what things are**: `snake_case` for functions and variables, `PascalCase` for classes,
  `UPPER_CASE` for constants. Avoid one-letter names except for short loop indices and maths.
- **`logging`, not `print`**, for anything a program reports while running.
- **Command-line tools** use `argparse`, have a `main()` behind `if __name__ == "__main__":`, give clear error
  messages and return a non-zero exit code on failure.
- **Paths** use `pathlib`. Default locations resolve relative to the script (`Path(__file__)`), not the
  current directory.
- **Randomness** goes through `numpy.random.default_rng(seed)` (or `random.Random(seed)`) with a `--seed`
  option, so every result can be reproduced.
- **Dependencies** go in the project's own `requirements.txt` with minimum versions. Prefer the standard
  library and well-known packages. Optional dependencies must fail gracefully.

### 🔐 Data and privacy

- **No real data in the repo.** Tests and examples generate synthetic data.
- **No secrets, tokens or machine-specific paths** in code, configs or docs.

### 📊 Statistical honesty (analysis and ML projects)

- Always compare models against a **simple baseline**. For random processes, that means pure chance.
- Validate with **time-aware or held-out data only**, and add a test that proves there's no leakage.
- **Correct for multiple comparisons**, and report negative results as plainly as positive ones.

### 🧪 Tests

- Use **pytest**, with tests in `projects/<name>/tests/`.
- Tests must be **fast** (a few seconds per project), **deterministic** (fixed seeds) and **offline**: no
  network access and no real data files.
- **Every bug fix comes with a test** that fails without the fix.

### 📝 Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/), with the project as the scope:

```text
feat(super-lotto): accept .csv input files
fix(super-lotto): parse day-first dates in CSV files
docs: add installation steps for Windows
test(super-lotto): cover jackpot values with "billion"
chore(ci): run tests on Python 3.13
```

- Write the subject line in the imperative mood ("add", not "added"), in 72 characters or fewer.
- Use the body to explain **why** the change was made, if that isn't obvious.

---

## 🔀 Submitting a pull request

1. **Start from an issue.** For anything beyond a typo, open or find an issue first and comment that you're
   working on it.

2. **Sync your fork** with the latest `main`:

   ```bash
   git switch main
   git pull upstream main
   ```

3. **Create a branch** named `<type>/<short-description>`:

   ```bash
   git switch -c fix/lotto-csv-dates
   ```

4. **Make your change.** Keep it focused, follow the [coding standards](#-coding-standards) and add or update
   tests.

5. **Run every check locally** from the repository root:

   ```bash
   ruff check .
   python -m pytest
   # plus the project's own end-to-end check, if it has one:
   python projects/super-lotto-simulator/super_lotto_simulator.py --self-test
   ```

6. **Update the documentation**: the project README, the CLI `--help` text, and the root README's project
   table if you added a project.

7. **Commit** using [Conventional Commits](#-commit-messages), then **check that no data or secrets are
   staged**:

   ```bash
   git status
   git commit -m "fix(super-lotto): parse day-first dates in CSV files"
   ```

8. **Push and open a pull request** against `lala-wynago/random-idle:main`:

   ```bash
   git push -u origin fix/lotto-csv-dates
   ```

   - Fill in the pull request template.
   - Link the issue with `Closes #123`.
   - Open it as a **draft** if it's still a work in progress.

9. **Make sure CI passes.** GitHub Actions runs the lint, the unit tests and each project's self-test on
   every pull request.

10. **Respond to review.** Push follow-up commits to the same branch. Please don't force-push once a review
    has started unless you're asked to.

11. **Merge.** Once it's approved and green, the maintainer squash-merges it. You can then delete your branch.

---

## 🆕 Adding a new project

Each project lives in its own folder under `projects/`, named in `kebab-case`, and must work on its own:

```text
projects/<project-name>/
├── README.md            # what it does, how to install, how to use it, limitations
├── requirements.txt     # runtime dependencies only (dev tools are in ../../requirements-dev.txt)
├── <entry_point>.py     # or a package folder for larger projects
└── tests/
    ├── conftest.py      # makes the project importable from its tests
    └── test_<module>.py
```

Before you open the pull request:

- [ ] Add the project to the **📦 Projects** table in the root [`README.md`](README.md).
- [ ] Add a job for it to [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
- [ ] Confirm that `ruff check .` and `python -m pytest` pass from the repository root.

---

## 📄 License

By contributing, you agree that your contribution is distributed under the repository's license, in the
`LICENSE` file. **This repository doesn't have a license yet**, so please open an issue and wait
for a reply before you start a large contribution.

---

Thanks again for helping out! 🙌
