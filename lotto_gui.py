#!/usr/bin/env python3
"""
lotto_gui.py
============

Desktop front end for ``super_lotto_simulator.py``.

Drag an .xlsx or .csv file of past results onto the window (or click to browse),
choose where the results file and the tests report should be saved, then click
"Generate simulated results". The heavy lifting runs on a background thread so
the window stays responsive; progress is streamed into the Log tab.

Launch from Finder with "Lotto Simulator.app", or run ``python lotto_gui.py``.
Drag-and-drop needs the optional ``tkinterdnd2`` package; without it the
browse button still works.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import super_lotto_simulator as sim  # noqa: E402

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore

    HAVE_DND = True
except Exception:  # pragma: no cover - optional dependency
    HAVE_DND = False

APP_NAME = "Lotto Simulator"
SETTINGS_PATH = Path.home() / "Library" / "Application Support" / APP_NAME / "settings.json"
DEFAULT_OUTPUT = APP_DIR / "output"
SUPPORTED_SUFFIXES = {".xlsx", ".csv"}
MODELS = ["ensemble", "baseline", "frequency", "recency", "gap", "markov", "ml"]
QUICK_SETTINGS = {"n_sims": 20_000, "backtest_sims": 500, "backtest_draws": 100}

MODEL_HELP = {
    "ensemble": "Blends all the models below, giving more weight to whichever did better in the backtest "
                "on past draws it hadn't seen. If none beat chance, it stays close to random.",
    "baseline": "Pure chance: every number equally likely. The yardstick every other model is measured against.",
    "frequency": "Favours numbers drawn most often across the whole history (\"hot\" numbers).",
    "recency": "Like frequency, but recent draws count more: a draw 50 draws ago counts half as much.",
    "gap": "\"Overdue\" numbers: weights each number by how long since it last appeared, calibrated on "
           "how often overdue numbers actually came up before.",
    "markov": "Looks at which numbers tended to follow the numbers in the previous draw.",
    "ml": "Machine learning (gradient boosting) that estimates each number's chance from its gaps, "
          "recent frequency and the shape of the last draw.",
}

TIPS = {
    "drop": "Your past draw results.\n\n"
            "Accepts .xlsx (first sheet is used) or .csv, with these column headers:\n"
            "Draw Date · Ball 1–5 · Super Ball · Jackpot (JMD)\n\n"
            "Rows with missing or impossible numbers are skipped and listed in the tests report.",
    "simulation": "How the candidate tickets are generated.",
    "tickets": "How many tickets to list (1–100).\n\n"
               "Each ticket is 5 main numbers plus 1 Super Ball: the combinations the chosen model "
               "rates most likely out of all the simulated draws.",
    "model_intro": "Which model generates the final tickets. Every model is first tested against pure "
                   "chance on past draws it hadn't seen; the result is shown under the tickets.",
    "seed": "Starting point for the random number generator.\n\n"
            "Same file + same settings + same seed = exactly the same tickets every time. Change it "
            "(any whole number) to get a different, equally valid set.\n\n"
            "It doesn't make tickets more or less likely to win.",
    "constrain": "Throws away simulated tickets that look unusual compared with past draws: a sum outside "
                 "the middle 95% of historical sums, or an odd/even mix seen in fewer than 5% of draws.\n\n"
                 "Tickets look more \"typical\", but the odds don't change: in a fair draw every "
                 "combination is equally likely.",
    "quick": "Uses fewer simulations: 20,000 final draws instead of 100,000, and a backtest of the last "
             "100 draws with 500 tickets each instead of 200 draws with 2,000.\n\n"
             "Faster, but the \"did any model beat chance?\" test is less sensitive.",
    "save_to": "Where the output files are written. The two files can go to the same folder or different "
               "ones. Existing files with the same names are replaced on each run.",
    "results": "Folder for predictions.xlsx: the tickets, each model's probability for every number, "
               "the backtest leaderboard and the test results, each on its own spreadsheet tab.",
    "report": "Folder for analysis_report.md and its charts: data summary, randomness tests, the "
              "backtest (did any model beat pure chance?) and caveats. Opens in a text editor.",
    "choose": "Pick a folder. It is created if it doesn't exist, and remembered next time.",
    "generate": "Runs the full analysis:\n"
                "1. cleans and checks the data\n"
                "2. tests whether past draws look random\n"
                "3. backtests every model on past draws it hadn't seen\n"
                "4. simulates the next draw with the chosen model and picks the tickets\n\n"
                "Usually 30–60 seconds (faster with Quick run).",
    "progress": "Moves while the analysis is running. Step-by-step messages are in the Log tab.",
    "ticket_columns": "#: the ticket's rank.\n"
                      "Main balls / Super Ball: the ticket itself.\n"
                      "Times simulated: how often this exact combination came up in the simulation. "
                      "Usually 1, because there are hundreds of thousands of possible combinations; "
                      "tickets are ranked by the model's probability instead.\n\n"
                      "The note below the table says whether any model actually beat chance.",
    "log": "Step-by-step progress messages from the analysis. Useful if something goes wrong.",
    "open_results": "Opens predictions.xlsx (available after a run).",
    "open_report": "Opens analysis_report.md, the tests report (available after a run).",
    "reveal": "Shows the results file in Finder (available after a run).",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_settings() -> dict[str, Any]:
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        return {}


def save_settings(values: dict[str, Any]) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(values, indent=2))
    except OSError:
        pass  # remembering folders is a convenience, never a reason to fail


def open_path(path: Path, reveal: bool = False) -> None:
    """Open a file with its default app (or reveal it in Finder); fall back to a text editor."""
    if reveal:
        subprocess.run(["open", "-R", str(path)], check=False)
        return
    if subprocess.run(["open", str(path)], check=False, capture_output=True).returncode != 0:
        subprocess.run(["open", "-t", str(path)], check=False)  # e.g. .md with no default app


def pick_color(widget: tk.Misc, *candidates: str) -> str:
    """First colour name Tk recognises (macOS system colours adapt to dark mode)."""
    for name in candidates:
        try:
            widget.winfo_rgb(name)
            return name
        except tk.TclError:
            continue
    return candidates[-1]


class QueueLogHandler(logging.Handler):
    """Forward simulator log records to the GUI thread."""

    def __init__(self, q: "queue.Queue[tuple[str, Any]]") -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(("log", self.format(record)))


class Tooltip:
    """Hover help for a widget. ``text`` may be a string or a callable (for live content)."""

    DELAY_MS = 450

    def __init__(self, widget: tk.Misc, text: Any, wrap: int = 340) -> None:
        self.widget, self.text, self.wrap = widget, text, wrap
        self._after: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        self._after = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self) -> None:
        self._after = None
        text = self.text() if callable(self.text) else self.text
        if not text or self._tip is not None:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        try:  # native-looking, non-activating help window on macOS
            tip.tk.call("::tk::unsupported::MacWindowStyle", "style", tip._w, "help", "none")
        except tk.TclError:
            pass
        tk.Label(tip, text=text, justify="left", wraplength=self.wrap, padx=10, pady=8,
                 font=("Helvetica Neue", 12),
                 bg=pick_color(tip, "systemWindowBackgroundColor", "#f6f6f6"),
                 fg=pick_color(tip, "systemLabelColor", "systemTextColor", "#1d1d1f"),
                 highlightthickness=1,
                 highlightbackground=pick_color(tip, "systemSeparatorColor", "#c7c7cc")).pack()
        tip.update_idletasks()
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        x = max(8, min(x, self.widget.winfo_screenwidth() - w - 8))
        if y + h > self.widget.winfo_screenheight() - 8:
            y = self.widget.winfo_rooty() - h - 6
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _event: Any = None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def add_tip(widgets: Any, text: Any, wrap: int = 340) -> None:
    """Attach the same tooltip to one widget or several (e.g. a label and its control)."""
    for w in widgets if isinstance(widgets, (list, tuple)) else (widgets,):
        Tooltip(w, text, wrap)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class LottoApp:
    """The single-window GUI."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.input_path: Optional[Path] = None
        self.result: Optional[sim.RunResult] = None
        self.worker: Optional[threading.Thread] = None
        self.started_at = 0.0

        s = load_settings()
        self.results_dir = tk.StringVar(value=s.get("results_dir", str(DEFAULT_OUTPUT)))
        self.report_dir = tk.StringVar(value=s.get("report_dir", str(DEFAULT_OUTPUT)))
        self.n_tickets = tk.StringVar(value=str(s.get("n_tickets", 10)))
        self.model = tk.StringVar(value=s.get("model", "ensemble"))
        self.seed = tk.StringVar(value=str(s.get("seed", 42)))
        self.constrain = tk.BooleanVar(value=s.get("constrain", False))
        self.quick = tk.BooleanVar(value=s.get("quick", False))
        self.last_input_dir = s.get("last_input_dir", str(Path.home()))
        self.status = tk.StringVar(value="Drop a results file to get started.")

        self._build()
        self._attach_logging()
        self.root.after(100, self._poll_events)
        self.root.after(300, self._bring_to_front)

    # ---- layout ----------------------------------------------------------
    def _build(self) -> None:
        root = self.root
        root.title(APP_NAME)
        root.minsize(720, 700)
        root.geometry("780x820")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        page = ttk.Frame(root, padding=(20, 16, 20, 12))
        page.grid(sticky="nsew")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(5, weight=1)

        ttk.Label(page, text="Super Lotto Simulator", font=("Helvetica Neue", 20, "bold")).grid(sticky="w")
        ttk.Label(page, text="Drop in past results, choose where to save, then generate simulated tickets. "
                             "Hover over anything for an explanation.",
                  foreground=pick_color(root, "systemSecondaryLabelColor", "#6e6e73")).grid(sticky="w", pady=(2, 12))

        self._build_drop_zone(page).grid(row=2, sticky="ew")
        self._build_options(page).grid(row=3, sticky="ew", pady=(14, 0))

        actions = ttk.Frame(page)
        actions.grid(row=4, sticky="ew", pady=(14, 10))
        actions.columnconfigure(1, weight=1)
        self.generate_btn = ttk.Button(actions, text="Generate simulated results", command=self._generate,
                                       default="active")
        self.generate_btn.grid(row=0, column=0, ipady=4)
        self.progress = ttk.Progressbar(actions, mode="indeterminate")
        self.progress.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        add_tip(self.generate_btn, TIPS["generate"])
        add_tip(self.progress, TIPS["progress"])

        self.tabs = ttk.Notebook(page)
        self.tabs.grid(row=5, sticky="nsew")
        self.tabs.add(self._build_tickets_tab(self.tabs), text="Tickets")
        self.tabs.add(self._build_log_tab(self.tabs), text="Log")
        add_tip(self.tabs, self._tab_tip)

        footer = ttk.Frame(page)
        footer.grid(row=6, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.open_results_btn = ttk.Button(footer, text="Open results", state="disabled",
                                           command=lambda: self._open_output("results"))
        self.open_report_btn = ttk.Button(footer, text="Open tests report", state="disabled",
                                          command=lambda: self._open_output("report"))
        self.reveal_btn = ttk.Button(footer, text="Show in Finder", state="disabled",
                                     command=lambda: self._open_output("reveal"))
        for col, (btn, tip) in enumerate(((self.open_results_btn, "open_results"),
                                          (self.open_report_btn, "open_report"),
                                          (self.reveal_btn, "reveal")), start=1):
            btn.grid(row=0, column=col, padx=(6, 0))
            add_tip(btn, TIPS[tip])

    def _build_drop_zone(self, parent: tk.Misc) -> tk.Frame:
        self.drop_bg = pick_color(parent, "systemTextBackgroundColor", "#ffffff")
        self.drop_hover = pick_color(parent, "systemSelectedTextBackgroundColor", "#dbe8fb")
        border = pick_color(parent, "systemSeparatorColor", "systemGrayColor", "#c7c7cc")
        accent = pick_color(parent, "systemControlAccentColor", "systemHighlight", "#0a64d6")

        zone = tk.Frame(parent, bg=self.drop_bg, highlightthickness=2, highlightbackground=border,
                        highlightcolor=accent, cursor="pointinghand")
        zone.columnconfigure(0, weight=1)
        hint = "Drag an .xlsx or .csv file here" if HAVE_DND else "Click to choose an .xlsx or .csv file"
        self.drop_title = tk.Label(zone, text=hint, bg=self.drop_bg, font=("Helvetica Neue", 15, "bold"),
                                   fg=pick_color(parent, "systemLabelColor", "systemTextColor", "#1d1d1f"))
        self.drop_title.grid(pady=(26, 2))
        self.drop_sub = tk.Label(zone, text="or click to browse" if HAVE_DND else "Drag-and-drop unavailable "
                                 "(install tkinterdnd2)", bg=self.drop_bg,
                                 fg=pick_color(parent, "systemSecondaryLabelColor", "#6e6e73"))
        self.drop_sub.grid(pady=(0, 26))
        self.drop_widgets = (zone, self.drop_title, self.drop_sub)
        add_tip(list(self.drop_widgets), TIPS["drop"])
        for w in self.drop_widgets:
            w.bind("<Button-1>", lambda _e: self._browse_input())
            if HAVE_DND:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)
                w.dnd_bind("<<DropEnter>>", lambda e: self._set_drop_highlight(True) or e.action)
                w.dnd_bind("<<DropLeave>>", lambda e: self._set_drop_highlight(False) or e.action)
        return zone

    def _build_options(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)

        sim_title = ttk.Label(frame, text="Simulation  ⓘ")
        add_tip(sim_title, TIPS["simulation"])
        simbox = ttk.LabelFrame(frame, labelwidget=sim_title, padding=(12, 8))
        simbox.grid(row=0, sticky="ew")
        tickets_lbl = ttk.Label(simbox, text="Tickets")
        tickets_lbl.grid(row=0, column=0, sticky="w")
        tickets_box = ttk.Spinbox(simbox, from_=1, to=100, width=5, textvariable=self.n_tickets)
        tickets_box.grid(row=0, column=1, padx=(6, 18))
        add_tip([tickets_lbl, tickets_box], TIPS["tickets"])
        model_lbl = ttk.Label(simbox, text="Model")
        model_lbl.grid(row=0, column=2, sticky="w")
        self.models = MODELS if sim.HAVE_SKLEARN else [m for m in MODELS if m != "ml"]
        model_box = ttk.Combobox(simbox, values=self.models, textvariable=self.model, state="readonly", width=11)
        model_box.grid(row=0, column=3, padx=(6, 18))
        add_tip([model_lbl, model_box], self._model_tip, wrap=440)
        seed_lbl = ttk.Label(simbox, text="Seed")
        seed_lbl.grid(row=0, column=4, sticky="w")
        seed_box = ttk.Entry(simbox, width=8, textvariable=self.seed)
        seed_box.grid(row=0, column=5, padx=(6, 0))
        add_tip([seed_lbl, seed_box], TIPS["seed"])
        constrain_box = ttk.Checkbutton(simbox, text="Match historical sum and odd/even balance",
                                        variable=self.constrain)
        constrain_box.grid(row=1, column=0, columnspan=6, sticky="w", pady=(8, 0))
        add_tip(constrain_box, TIPS["constrain"])
        quick_box = ttk.Checkbutton(simbox, text="Quick run (fewer simulations, about 4x faster)",
                                    variable=self.quick)
        quick_box.grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))
        add_tip(quick_box, TIPS["quick"])

        out_title = ttk.Label(frame, text="Save to  ⓘ")
        add_tip(out_title, TIPS["save_to"])
        outbox = ttk.LabelFrame(frame, labelwidget=out_title, padding=(12, 8))
        outbox.grid(row=1, sticky="ew", pady=(10, 0))
        outbox.columnconfigure(1, weight=1)
        for row, (label, var, note, tip) in enumerate((
            ("Results file", self.results_dir, "predictions.xlsx", "results"),
            ("Tests report", self.report_dir, "analysis_report.md + charts", "report"),
        )):
            row_lbl = ttk.Label(outbox, text=label)
            row_lbl.grid(row=row * 2, column=0, sticky="w", padx=(0, 10))
            row_entry = ttk.Entry(outbox, textvariable=var)
            row_entry.grid(row=row * 2, column=1, sticky="ew")
            choose = ttk.Button(outbox, text="Choose…", command=lambda v=var, l=label: self._choose_folder(v, l))
            choose.grid(row=row * 2, column=2, padx=(8, 0))
            row_note = ttk.Label(outbox, text=note, font=("Helvetica Neue", 11),
                                 foreground=pick_color(parent, "systemSecondaryLabelColor", "#6e6e73"))
            row_note.grid(row=row * 2 + 1, column=1, sticky="w", pady=(0, 6))
            add_tip([row_lbl, row_entry, row_note], TIPS[tip])
            add_tip(choose, TIPS["choose"])
        return frame

    def _model_tip(self) -> str:
        """Live model help: every model, with the current choice marked."""
        current = self.model.get()
        lines = [TIPS["model_intro"], ""]
        for name in self.models:
            marker = "▶ " if name == current else "    "
            lines.append(f"{marker}{name}: {MODEL_HELP[name]}")
        return "\n".join(lines)

    def _tab_tip(self) -> str:
        """Help for the tab header under the pointer (only shown over the tab strip)."""
        x = self.tabs.winfo_pointerx() - self.tabs.winfo_rootx()
        y = self.tabs.winfo_pointery() - self.tabs.winfo_rooty()
        try:
            index = self.tabs.index(f"@{x},{y}")
        except tk.TclError:
            return ""
        return TIPS["ticket_columns"] if index == 0 else TIPS["log"]

    def _build_tickets_tab(self, parent: tk.Misc) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=10)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        about = ttk.Label(tab, text="ⓘ What do these columns mean?", font=("Helvetica Neue", 11),
                          foreground=pick_color(parent, "systemSecondaryLabelColor", "#6e6e73"))
        about.grid(row=0, sticky="e", pady=(0, 6))
        add_tip(about, TIPS["ticket_columns"], wrap=400)
        cols = ("rank", "main", "super", "count")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", height=10)
        for col, title, width, anchor in (("rank", "#", 40, "center"), ("main", "Main balls", 260, "w"),
                                          ("super", "Super Ball", 100, "center"),
                                          ("count", "Times simulated", 130, "center")):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "main"))
        self.tree.grid(row=1, sticky="nsew")
        self.verdict = ttk.Label(tab, text="Simulated tickets will appear here.", wraplength=660, justify="left")
        self.verdict.grid(row=2, sticky="ew", pady=(10, 0))
        tab.bind("<Configure>", lambda e: self.verdict.configure(wraplength=max(300, e.width - 24)))
        return tab

    def _build_log_tab(self, parent: tk.Misc) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=(10, 10, 0, 10))
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.log = tk.Text(tab, height=12, wrap="none", font=("Menlo", 11), state="disabled",
                           borderwidth=0, highlightthickness=0)
        self.log.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(tab, orient="vertical", command=self.log.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=yscroll.set)
        return tab

    # ---- input -----------------------------------------------------------
    def _set_drop_highlight(self, on: bool) -> None:
        for w in self.drop_widgets:
            w.configure(bg=self.drop_hover if on else self.drop_bg)

    def _on_drop(self, event: Any) -> str:
        self._set_drop_highlight(False)
        paths = self.root.tk.splitlist(event.data)
        if paths:
            self._set_input(Path(paths[0]))
        return event.action

    def _open_document(self, *paths: str) -> None:
        """macOS: a file dropped on the Dock icon while the app is running."""
        if paths:
            self._set_input(Path(paths[0]))

    def _browse_input(self) -> None:
        if self._busy():
            return
        chosen = filedialog.askopenfilename(
            title="Choose past results", initialdir=self.last_input_dir,
            filetypes=[("Results files", "*.xlsx *.csv"), ("Excel workbook", "*.xlsx"), ("CSV", "*.csv")])
        if chosen:
            self._set_input(Path(chosen))

    def _set_input(self, path: Path) -> None:
        if self._busy():
            return
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            hint = " Re-save it as .xlsx or .csv first." if path.suffix.lower() in {".xls", ".numbers"} else ""
            messagebox.showerror(APP_NAME, f"“{path.name}” isn't an .xlsx or .csv file.{hint}")
            return
        if not path.is_file():
            messagebox.showerror(APP_NAME, f"Can't find “{path}”.")
            return
        self.input_path = path
        self.last_input_dir = str(path.parent)
        self.drop_title.configure(text=path.name)
        self.drop_sub.configure(text=f"{path.parent}   ·   drop or click to change")
        self.status.set("Ready. Click “Generate simulated results”.")

    def _choose_folder(self, var: tk.StringVar, label: str) -> None:
        start = var.get() if Path(var.get()).expanduser().exists() else str(Path.home())
        chosen = filedialog.askdirectory(title=f"Where should the {label.lower()} be saved?", initialdir=start)
        if chosen:
            var.set(chosen)
            self._save()

    # ---- run -------------------------------------------------------------
    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _save(self) -> None:
        save_settings({
            "results_dir": self.results_dir.get(), "report_dir": self.report_dir.get(),
            "n_tickets": self.n_tickets.get(), "model": self.model.get(), "seed": self.seed.get(),
            "constrain": self.constrain.get(), "quick": self.quick.get(), "last_input_dir": self.last_input_dir,
        })

    def _generate(self) -> None:
        if self._busy():
            return
        if self.input_path is None:
            messagebox.showinfo(APP_NAME, "Drop a past-results .xlsx or .csv file first.")
            return
        try:
            n_tickets = int(self.n_tickets.get())
            seed = int(self.seed.get())
            if not 1 <= n_tickets <= 100:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Tickets must be a whole number from 1 to 100, and Seed a whole number.")
            return
        results_dir = Path(self.results_dir.get().strip() or DEFAULT_OUTPUT).expanduser()
        report_dir = Path(self.report_dir.get().strip() or DEFAULT_OUTPUT).expanduser()
        cfg = sim.Config(
            input=self.input_path, sheet=None, output_dir=results_dir, results_dir=results_dir,
            report_dir=report_dir, n_tickets=n_tickets, model=self.model.get(), seed=seed,
            constrain=self.constrain.get(), use_ml=sim.HAVE_SKLEARN,
            **(QUICK_SETTINGS if self.quick.get() else {}),
        )
        self._save()
        self.result = None
        self.tree.delete(*self.tree.get_children())
        self.verdict.configure(text="Working… the backtest compares every model against pure chance first.")
        self._clear_log()
        for btn in (self.open_results_btn, self.open_report_btn, self.reveal_btn):
            btn.configure(state="disabled")
        self.generate_btn.configure(state="disabled")
        self.progress.start(12)
        self.tabs.select(1)
        self.started_at = time.monotonic()
        self.worker = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self.worker.start()
        self._tick()

    def _run(self, cfg: sim.Config) -> None:
        """Background thread: run the pipeline and report back through the queue."""
        try:
            self.events.put(("done", sim.run_pipeline(cfg)))
        except sim.DataError as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:  # unexpected: keep the traceback in the log
            sim.LOG.exception("Unexpected error")
            self.events.put(("error", f"Unexpected error: {exc}"))

    def _tick(self) -> None:
        if self._busy():
            secs = int(time.monotonic() - self.started_at)
            self.status.set(f"Running… {secs // 60}:{secs % 60:02d}")
            self.root.after(1000, self._tick)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "error":
                    self._fail(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _finish(self, result: sim.RunResult) -> None:
        self.result = result
        self._stop_running()
        for _, r in result.tickets.iterrows():
            balls = "   ".join(f"{int(r[f'ball_{i}']):2d}" for i in range(1, 6))
            self.tree.insert("", "end", values=(int(r["rank"]), balls, int(r["super_ball"]), int(r["sim_count"])))
        self.verdict.configure(text=sim.disclaimer(result.verdict))
        for btn in (self.open_results_btn, self.open_report_btn, self.reveal_btn):
            btn.configure(state="normal")
        secs = time.monotonic() - self.started_at
        self.status.set(f"Done in {secs:.0f}s. Saved predictions.xlsx and analysis_report.md.")
        self.tabs.select(0)

    def _fail(self, message: str) -> None:
        self._stop_running()
        self.verdict.configure(text="No results — see the Log tab for details.")
        self.status.set("Failed. See the Log tab.")
        messagebox.showerror("Couldn't generate results", message)

    def _stop_running(self) -> None:
        self.progress.stop()
        self.generate_btn.configure(state="normal")

    def _open_output(self, which: str) -> None:
        if self.result is None:
            return
        if which == "results":
            open_path(self.result.results_path)
        elif which == "report":
            open_path(self.result.report_path)
        else:
            open_path(self.result.results_path, reveal=True)

    # ---- log -------------------------------------------------------------
    def _attach_logging(self) -> None:
        handler = QueueLogHandler(self.events)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        sim.LOG.addHandler(handler)
        sim.LOG.setLevel(logging.INFO)
        sim.LOG.propagate = False

    def _append_log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _bring_to_front(self) -> None:
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(250, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()


def create_root() -> tk.Tk:
    """A Tk root with drag-and-drop support when tkinterdnd2 is available."""
    global HAVE_DND
    if HAVE_DND:
        try:
            return TkinterDnD.Tk()
        except Exception:
            HAVE_DND = False
    return tk.Tk()


def main() -> None:
    root = create_root()
    app = LottoApp(root)
    try:
        root.createcommand("::tk::mac::OpenDocument", app._open_document)
    except tk.TclError:
        pass
    if len(sys.argv) > 1:
        app._set_input(Path(sys.argv[1]).expanduser().resolve())
    root.mainloop()


if __name__ == "__main__":
    main()
