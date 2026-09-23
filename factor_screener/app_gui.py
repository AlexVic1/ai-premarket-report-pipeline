"""
Desktop GUI (PyQt6) for the FACTOR CONT screener.

All signal math lives in factor_screener.py and is used unchanged: the GUI only
sets its CONFIG constants (fs.apply_settings) and calls fs.analyze_ticker on a
background thread pool, so the window stays responsive during a scan.

Run:  .venv\\Scripts\\python factor_screener\\app_gui.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
from PyQt6.QtCore import (QAbstractTableModel, QModelIndex, QSettings, QSortFilterProxyModel, Qt,
                          QThread, QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFont, QGuiApplication, QPalette
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                             QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QTableView,
                             QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factor_screener as fs  # noqa: E402
import universes  # noqa: E402

RESULTS_DIR = fs.EXPORT_DIR
TIMEFRAMES = ["15m", "30m", "1h", "2h", "4h", "1d", "1wk"]
TV_INTERVAL = {"15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240", "1d": "D", "1wk": "W"}
EMA_PRESETS = ["9 / 21 / 50", "21 / 50 / 150", "21 / 50 / 200", "50 / 100 / 200", "9 / 50 / 200", "5 / 13 / 34"]
FACTOR_NAMES = {"F": "FVG", "L": "Liquidity", "K": "Killzone", "D": "OTE", "M": "Structure", "X": "Displacement"}

COLUMNS = ["Ticker", "Signal Time", "Timeframe", "Direction", "Factors", "Score", "Seq",
           "Price", "Last", "Avg Vol (M)", "Sector"]
LONG_TINT, SHORT_TINT = QColor(38, 166, 154, 50), QColor(239, 83, 80, 50)
LONG_TEXT, SHORT_TEXT = QColor(38, 200, 154), QColor(255, 99, 96)


# ============================================================================
# Background workers
# ============================================================================

class UniverseLoader(QThread):
    loaded = pyqtSignal(dict, str)

    def __init__(self, force: bool = False):
        super().__init__()
        self.force = force

    def run(self):
        data, source = universes.load_universe(force_refresh=self.force)
        self.loaded.emit(data, source)


class ScanWorker(QThread):
    progress = pyqtSignal(int, int, str)       # done, total, ticker
    rows_ready = pyqtSignal(list)              # result rows for one ticker
    log = pyqtSignal(str)
    finished_scan = pyqtSignal(dict)           # summary

    def __init__(self, tickers, spec, filters, universe, workers):
        super().__init__()
        self.tickers, self.spec, self.filters = tickers, spec, filters
        self.universe, self.workers = universe, workers
        self._stop = False

    def stop(self):
        self._stop = True

    def _job(self, ticker):
        if self._stop:
            return ticker, None, "cancelled"
        try:
            res = fs.analyze_ticker(ticker, self.spec)
            if res["error"]:
                return ticker, None, res["error"]
            return ticker, res, None
        except Exception as e:
            return ticker, None, f"{type(e).__name__}: {e}"

    def _passes(self, res) -> bool:
        f, price, vol = self.filters, res["price"], res["avg_volume"] or 0.0
        if price is None:
            return False
        if f["min_price"] > 0 and price < f["min_price"]:
            return False
        if f["max_price"] > 0 and price > f["max_price"]:
            return False
        return not (f["min_volume"] > 0 and vol < f["min_volume"])

    def run(self):
        total, done = len(self.tickers), 0
        stats = {"total": total, "scanned": 0, "hits": 0, "signals": 0, "filtered": 0, "errors": 0,
                 "cancelled": False}
        pool = ThreadPoolExecutor(max_workers=self.workers)
        futures = [pool.submit(self._job, t) for t in self.tickers]
        try:
            for fut in as_completed(futures):
                ticker, res, err = fut.result()
                done += 1
                self.progress.emit(done, total, ticker)
                if self._stop:
                    break
                stats["scanned"] += 1
                if err:
                    stats["errors"] += 1
                    self.log.emit(f"{ticker}: skipped ({err})")
                    continue
                if not self._passes(res):
                    stats["filtered"] += 1
                    continue
                if not res["signals"]:
                    continue
                sector = universes.sector_of(ticker, self.universe)
                rows = [{
                    "Ticker": ticker, "Signal Time": s["time"], "Timeframe": self.spec.label,
                    "Direction": s["direction"], "Factors": s["factors"], "Score": s["score"],
                    "Seq": s["seq"], "Price": float(s["close"]), "Last": res["price"],
                    "Avg Vol (M)": (res["avg_volume"] or 0.0) / 1e6, "Sector": sector,
                } for s in res["signals"]]
                stats["hits"] += 1
                stats["signals"] += len(rows)
                self.rows_ready.emit(rows)
        finally:
            if self._stop:
                stats["cancelled"] = True
                for f in futures:
                    f.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
        self.finished_scan.emit(stats)


# ============================================================================
# Results table model + filter
# ============================================================================

class ResultsModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self.intraday = True

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def display(self, row: dict, col: str) -> str:
        v = row.get(col)
        if col == "Signal Time":
            return v.strftime("%Y-%m-%d %H:%M" if self.intraday else "%Y-%m-%d")
        if col in ("Price", "Last"):
            return f"{v:,.2f}" if v is not None else ""
        if col == "Avg Vol (M)":
            return f"{v:,.2f}"
        return "" if v is None else str(v)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = self.rows[index.row()], COLUMNS[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self.display(row, col)
        if role == Qt.ItemDataRole.UserRole:  # sort key
            v = row.get(col)
            if col == "Signal Time":
                return v.timestamp()
            if col == "Seq":
                return int(re.sub(r"\D", "", v) or 0)
            return v
        if role == Qt.ItemDataRole.BackgroundRole:
            return LONG_TINT if row["Direction"] == "LONG" else SHORT_TINT
        if role == Qt.ItemDataRole.ForegroundRole and col == "Direction":
            return LONG_TEXT if row["Direction"] == "LONG" else SHORT_TEXT
        if role == Qt.ItemDataRole.FontRole and col in ("Ticker", "Direction"):
            f = QFont()
            f.setBold(True)
            return f
        if role == Qt.ItemDataRole.FontRole and col == "Seq" and row["Seq"] == "F#1":
            f = QFont()
            f.setBold(True)
            return f
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in ("Score", "Price", "Last", "Avg Vol (M)"):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if col in ("Timeframe", "Direction", "Seq"):
                return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ToolTipRole and col == "Factors":
            return ", ".join(f"{k} = {FACTOR_NAMES[k]}" for k in row["Factors"].split("+") if k in FACTOR_NAMES)
        return None

    def add_rows(self, rows):
        start = len(self.rows)
        self.beginInsertRows(QModelIndex(), start, start + len(rows) - 1)
        self.rows.extend(rows)
        self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.rows = []
        self.endResetModel()


class ResultsFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.direction = "All"
        self.min_score = 0
        self.first_only = False
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set_filters(self, text, direction, min_score, first_only):
        self.text = text.strip().lower()
        self.direction, self.min_score, self.first_only = direction, min_score, first_only
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model: ResultsModel = self.sourceModel()
        row = model.rows[source_row]
        if self.direction != "All" and row["Direction"] != self.direction:
            return False
        if row["Score"] < self.min_score or (self.first_only and row["Seq"] != "F#1"):
            return False
        if not self.text:
            return True
        hay = " ".join(model.display(row, c) for c in COLUMNS).lower()
        return all(term in hay for term in self.text.split())


# ============================================================================
# Main window
# ============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FACTOR CONT Screener: EMA + SMC Confluence")
        self.resize(1480, 900)
        self.settings = QSettings("FactorScreener", "GUI")
        self.universe = {"sp500": {}, "ndx": {}}
        self.worker: ScanWorker | None = None
        self.loader: UniverseLoader | None = None
        self.scan_started = 0.0
        self.last_scan_meta: dict = {}

        self.model = ResultsModel()
        self.proxy = ResultsFilter()
        self.proxy.setSourceModel(self.model)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_controls())
        splitter.addWidget(self._build_tabs())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 1100])

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("Idle")
        self.status = QLabel("Ready")
        bottom = QHBoxLayout()
        bottom.addWidget(self.progress, 3)
        bottom.addWidget(self.status, 2)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addWidget(splitter, 1)
        lay.addLayout(bottom)
        self.setCentralWidget(central)

        self._restore_settings()
        self._sync_factor_string()
        self._on_mode_changed()
        self._load_universe(force=False)
        self.refresh_history()

    # ---------------------------------------------------------------- controls
    def _build_controls(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)

        # --- Scan settings
        g = QGroupBox("Scan")
        f = QFormLayout(g)
        self.tf_combo = QComboBox()
        self.tf_combo.addItems(TIMEFRAMES)
        self.tf_combo.setCurrentText(fs.TIMEFRAME)
        f.addRow("Timeframe", self.tf_combo)
        lb_row = QHBoxLayout()
        self.lb_slider = QSlider(Qt.Orientation.Horizontal)
        self.lb_slider.setRange(1, 30)
        self.lb_spin = QSpinBox()
        self.lb_spin.setRange(1, 30)
        self.lb_slider.valueChanged.connect(self.lb_spin.setValue)
        self.lb_spin.valueChanged.connect(self.lb_slider.setValue)
        self.lb_spin.setValue(fs.LOOKBACK_DAYS)
        lb_row.addWidget(self.lb_slider, 1)
        lb_row.addWidget(self.lb_spin)
        f.addRow("Lookback days", lb_row)
        self.lb_mode = QComboBox()
        self.lb_mode.addItems(["trading", "calendar"])
        self.lb_mode.setToolTip("trading = last N sessions in the data; calendar = now minus N days")
        f.addRow("Lookback mode", self.lb_mode)
        self.closed_only = QCheckBox("Only closed bars (ignore the forming bar)")
        self.closed_only.setChecked(fs.ONLY_CLOSED_BARS)
        f.addRow(self.closed_only)
        v.addWidget(g)

        # --- Factor rule
        g = QGroupBox("Factor rule")
        f = QFormLayout(g)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Any (OR)", "All (AND)", "Pairs"])
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        f.addRow("Match mode", self.mode_combo)
        grid = QHBoxLayout()
        self.factor_checks: dict[str, QCheckBox] = {}
        for k in fs.FACTOR_ORDER:
            cb = QCheckBox(k)
            cb.setToolTip(FACTOR_NAMES[k])
            cb.setChecked(k in fs.REQUIRED_FACTORS)
            cb.toggled.connect(self._sync_factor_string)
            self.factor_checks[k] = cb
            grid.addWidget(cb)
        f.addRow("Required", grid)
        self.factor_str = QLineEdit()
        self.factor_str.setPlaceholderText("e.g. MX")
        self.factor_str.textEdited.connect(self._on_factor_string_edited)
        f.addRow("Factor string", self.factor_str)
        self.pairs_edit = QLineEdit(",".join(a + b for a, b in fs.FACTOR_PAIRS))
        self.pairs_edit.setPlaceholderText("e.g. XM,XL")
        f.addRow("Pairs", self.pairs_edit)
        hint = QLabel("F FVG · L Liquidity · K Killzone · D OTE · M Structure · X Displacement")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8a93a6; font-size: 11px;")
        f.addRow(hint)
        v.addWidget(g)

        # --- Strategy
        g = QGroupBox("EMA && strategy")
        f = QFormLayout(g)
        self.ema_combo = QComboBox()
        self.ema_combo.addItems(EMA_PRESETS)
        f.addRow("EMA set", self.ema_combo)
        self.chk_slow_align = QCheckBox("Require EMA-Mid above/below EMA-Slow for CONT")
        self.chk_slow_align.setChecked(fs.REQUIRE_EMA_SLOW_ALIGN)
        self.chk_break = QCheckBox("Confirm only if breaks signal candle H/L")
        self.chk_break.setChecked(fs.USE_BREAK_SIGNAL)
        self.chk_rearm = QCheckBox("Auto re-arm trend from EMA stack")
        self.chk_rearm.setChecked(fs.CONT_AUTO_REARM)
        self.chk_unlock = QCheckBox("Avg-move unlock for repeat CONTs (FIX1)")
        self.chk_unlock.setChecked(fs.USE_AVG_MOVE_UNLOCK)
        self.chk_mature = QCheckBox("Mature-trend CONT (FIX2)")
        self.chk_mature.setChecked(fs.USE_MATURE_TREND)
        self.chk_wicks = QCheckBox("Hold test uses wicks instead of closes")
        self.chk_wicks.setChecked(fs.USE_WICKS_FOR_HOLD)
        for cb in (self.chk_slow_align, self.chk_break, self.chk_rearm, self.chk_unlock,
                   self.chk_mature, self.chk_wicks):
            f.addRow(cb)
        self.inv_combo = QComboBox()
        self.inv_combo.addItems(["closes", "cross", "either"])
        self.inv_combo.setToolTip("How the active trend is cleared: N closes through EMA-mid, fast/mid cross, or either")
        f.addRow("Clear trend when", self.inv_combo)
        self.inv_closes = QSpinBox()
        self.inv_closes.setRange(1, 20)
        self.inv_closes.setValue(fs.CONT_INV_CLOSES)
        f.addRow("N closes to clear", self.inv_closes)
        v.addWidget(g)

        # --- Universe
        g = QGroupBox("Tickers")
        f = QFormLayout(g)
        pre_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Loading lists…")
        self.preset_combo.currentIndexChanged.connect(self._update_ticker_count)
        refresh = QPushButton("Reload")
        refresh.setToolTip("Re-download S&P 500 / Nasdaq-100 constituents")
        refresh.clicked.connect(lambda: self._load_universe(force=True))
        pre_row.addWidget(self.preset_combo, 1)
        pre_row.addWidget(refresh)
        f.addRow("Preset", pre_row)
        self.custom_edit = QPlainTextEdit()
        self.custom_edit.setPlaceholderText("Custom tickers, comma or newline separated\n(added to the preset)")
        self.custom_edit.setFixedHeight(80)
        self.custom_edit.textChanged.connect(self._update_ticker_count)
        f.addRow("Custom", self.custom_edit)
        self.ticker_count = QLabel("")
        self.ticker_count.setStyleSheet("color: #8a93a6;")
        f.addRow(self.ticker_count)
        v.addWidget(g)

        # --- Filters
        g = QGroupBox("Price && volume filters (0 = off)")
        f = QFormLayout(g)
        self.min_price = QDoubleSpinBox()
        self.max_price = QDoubleSpinBox()
        for sp in (self.min_price, self.max_price):
            sp.setRange(0, 100000)
            sp.setDecimals(2)
            sp.setPrefix("$ ")
        self.min_price.setValue(10)
        self.max_price.setValue(0)
        f.addRow("Min price", self.min_price)
        f.addRow("Max price", self.max_price)
        self.min_vol = QDoubleSpinBox()
        self.min_vol.setRange(0, 1000)
        self.min_vol.setDecimals(2)
        self.min_vol.setSuffix(" M shares/day")
        self.min_vol.setValue(1.0)
        self.min_vol.setToolTip("Average daily volume over the last 20 sessions")
        f.addRow("Min avg volume", self.min_vol)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setValue(fs.MAX_WORKERS)
        self.workers_spin.setToolTip("Parallel downloads. Lower it if Yahoo starts rate-limiting.")
        f.addRow("Parallel workers", self.workers_spin)
        self.autosave = QCheckBox("Auto-save every scan to History (CSV)")
        self.autosave.setChecked(True)
        f.addRow(self.autosave)
        v.addWidget(g)

        btns = QHBoxLayout()
        self.scan_btn = QPushButton("▶  Scan")
        self.scan_btn.setObjectName("primary")
        self.scan_btn.setMinimumHeight(36)
        self.scan_btn.clicked.connect(self.start_scan)
        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setMinimumHeight(36)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_scan)
        btns.addWidget(self.scan_btn, 2)
        btns.addWidget(self.stop_btn, 1)
        v.addLayout(btns)
        v.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        scroll.setMinimumWidth(360)
        return scroll

    # ---------------------------------------------------------------- tabs
    def _build_tabs(self) -> QWidget:
        self.tabs = QTabWidget()

        # Results
        res = QWidget()
        v = QVBoxLayout(res)
        fbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter results… (ticker, sector, factors, e.g. 'NVDA' or 'M+X long')")
        self.search.setClearButtonEnabled(True)
        self.dir_filter = QComboBox()
        self.dir_filter.addItems(["All", "LONG", "SHORT"])
        self.score_filter = QSpinBox()
        self.score_filter.setRange(0, 6)
        self.score_filter.setPrefix("Score ≥ ")
        self.first_only = QCheckBox("F#1 only")
        self.first_only.setToolTip("Only the first factor signal after a direction flip")
        for w in (self.search, self.dir_filter, self.score_filter, self.first_only):
            fbar.addWidget(w, 3 if w is self.search else 0)
        self.search.textChanged.connect(self._apply_result_filter)
        self.dir_filter.currentIndexChanged.connect(self._apply_result_filter)
        self.score_filter.valueChanged.connect(self._apply_result_filter)
        self.first_only.toggled.connect(self._apply_result_filter)
        v.addLayout(fbar)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COLUMNS.index("Signal Time"), Qt.SortOrder.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setShowGrid(False)
        self.table.doubleClicked.connect(self._open_row_on_tradingview)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        for col, w in {"Ticker": 80, "Signal Time": 140, "Timeframe": 80, "Direction": 85, "Factors": 90,
                       "Score": 60, "Seq": 60, "Price": 90, "Last": 90, "Avg Vol (M)": 95}.items():
            self.table.setColumnWidth(COLUMNS.index(col), w)
        v.addWidget(self.table, 1)

        ebar = QHBoxLayout()
        self.result_count = QLabel("No results yet. Double-click a row to open it on TradingView.")
        self.result_count.setStyleSheet("color: #8a93a6;")
        xlsx = QPushButton("Export Excel…")
        csv = QPushButton("Export CSV…")
        xlsx.setToolTip("Exports the rows currently shown (after filters)")
        csv.setToolTip("Exports the rows currently shown (after filters)")
        xlsx.clicked.connect(lambda: self.export_results("xlsx"))
        csv.clicked.connect(lambda: self.export_results("csv"))
        ebar.addWidget(self.result_count, 1)
        ebar.addWidget(xlsx)
        ebar.addWidget(csv)
        v.addLayout(ebar)
        self.tabs.addTab(res, "Results")

        # History
        hist = QWidget()
        hv = QVBoxLayout(hist)
        hbar = QHBoxLayout()
        hbar.addWidget(QLabel(f"Saved scans in {RESULTS_DIR}"), 1)
        for text, slot in (("Refresh", self.refresh_history), ("Open file", self._open_history_file),
                           ("Load into Results", self._load_history_into_results),
                           ("Open folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(RESULTS_DIR))))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            hbar.addWidget(b)
        hv.addLayout(hbar)
        hsplit = QSplitter(Qt.Orientation.Vertical)
        self.hist_table = QTableWidget(0, 7)
        self.hist_table.setHorizontalHeaderLabels(["Saved at", "Timeframe", "Universe", "Rule", "Lookback",
                                                   "Signals", "File"])
        self.hist_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.hist_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.hist_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hist_table.verticalHeader().setVisible(False)
        self.hist_table.horizontalHeader().setStretchLastSection(True)
        self.hist_table.itemSelectionChanged.connect(self._preview_history)
        self.hist_table.doubleClicked.connect(self._load_history_into_results)
        self.hist_preview = QTableWidget()
        self.hist_preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hist_preview.verticalHeader().setVisible(False)
        hsplit.addWidget(self.hist_table)
        hsplit.addWidget(self.hist_preview)
        hsplit.setSizes([300, 400])
        hv.addWidget(hsplit, 1)
        self.tabs.addTab(hist, "History")

        # Log
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.tabs.addTab(self.log_view, "Log")
        return self.tabs

    # ---------------------------------------------------------------- factor controls
    def _sync_factor_string(self):
        self.factor_str.setText("".join(k for k, cb in self.factor_checks.items() if cb.isChecked()))

    def _on_factor_string_edited(self, text):
        wanted = set(text.upper())
        for k, cb in self.factor_checks.items():
            cb.blockSignals(True)
            cb.setChecked(k in wanted)
            cb.blockSignals(False)

    def _on_mode_changed(self):
        pairs = self.mode_combo.currentText() == "Pairs"
        self.pairs_edit.setEnabled(pairs)
        self.factor_str.setEnabled(not pairs)
        for cb in self.factor_checks.values():
            cb.setEnabled(not pairs)

    # ---------------------------------------------------------------- universe
    def _load_universe(self, force: bool):
        if self.loader and self.loader.isRunning():
            return
        self.status.setText("Loading index constituents…")
        self.loader = UniverseLoader(force)
        self.loader.loaded.connect(self._on_universe_loaded)
        self.loader.start()

    def _on_universe_loaded(self, data, source):
        self.universe = data
        wanted = self.preset_combo.currentText()
        if wanted == "Loading lists…":
            wanted = self.settings.value("preset", "MegaCap Tech")
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(universes.preset_names(data))
        idx = self.preset_combo.findText(wanted)
        self.preset_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.preset_combo.blockSignals(False)
        self._update_ticker_count()
        self.status.setText(f"Index lists loaded ({source}): S&P 500 {len(data['sp500'])}, "
                            f"Nasdaq-100 {len(data['ndx'])}")
        self._log(f"Index lists loaded from {source}.")

    def selected_tickers(self) -> list[str]:
        preset = universes.preset_tickers(self.preset_combo.currentText(), self.universe)
        custom = fs.load_tickers(self.custom_edit.toPlainText().replace("\n", ","), None) \
            if self.custom_edit.toPlainText().strip() else []
        seen, out = set(), []
        for t in list(preset) + custom:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _update_ticker_count(self):
        n_custom = len(fs.load_tickers(self.custom_edit.toPlainText().replace("\n", ","), None)) \
            if self.custom_edit.toPlainText().strip() else 0
        n_preset = len(universes.preset_tickers(self.preset_combo.currentText(), self.universe))
        total = len(self.selected_tickers())
        self.ticker_count.setText(f"{n_preset} preset + {n_custom} custom → {total} tickers to scan")

    # ---------------------------------------------------------------- scanning
    def _engine_settings(self) -> dict:
        fast, mid, slow = (int(x) for x in self.ema_combo.currentText().split("/"))
        mode = {"Any (OR)": "any", "All (AND)": "all", "Pairs": "pairs"}[self.mode_combo.currentText()]
        pairs = [(p[0], p[1]) for p in re.split(r"[,\s]+", self.pairs_edit.text().upper())
                 if len(p) == 2 and set(p) <= set(fs.FACTOR_ORDER)]
        return {
            "TIMEFRAME": self.tf_combo.currentText(),
            "LOOKBACK_DAYS": self.lb_spin.value(),
            "LOOKBACK_MODE": self.lb_mode.currentText(),
            "ONLY_CLOSED_BARS": self.closed_only.isChecked(),
            "EMA_FAST": fast, "EMA_MID": mid, "EMA_SLOW": slow,
            "REQUIRE_EMA_SLOW_ALIGN": self.chk_slow_align.isChecked(),
            "USE_BREAK_SIGNAL": self.chk_break.isChecked(),
            "CONT_AUTO_REARM": self.chk_rearm.isChecked(),
            "USE_AVG_MOVE_UNLOCK": self.chk_unlock.isChecked(),
            "USE_MATURE_TREND": self.chk_mature.isChecked(),
            "USE_WICKS_FOR_HOLD": self.chk_wicks.isChecked(),
            "CONT_INV_MODE": self.inv_combo.currentText(),
            "CONT_INV_CLOSES": self.inv_closes.value(),
            "MATCH_MODE": mode,
            "REQUIRED_FACTORS": {k for k, cb in self.factor_checks.items() if cb.isChecked()},
            "FACTOR_PAIRS": pairs,
        }

    def _rule_text(self, s: dict) -> str:
        if s["MATCH_MODE"] == "pairs":
            return " or ".join(a + "+" + b for a, b in s["FACTOR_PAIRS"])
        joiner = " OR " if s["MATCH_MODE"] == "any" else " AND "
        return joiner.join(k for k in fs.FACTOR_ORDER if k in s["REQUIRED_FACTORS"])

    def start_scan(self):
        if self.worker and self.worker.isRunning():
            return
        s = self._engine_settings()
        if s["MATCH_MODE"] == "pairs" and not s["FACTOR_PAIRS"]:
            QMessageBox.warning(self, "Factor rule", "Enter at least one pair, e.g. XM or XM,XL.")
            return
        if s["MATCH_MODE"] != "pairs" and not s["REQUIRED_FACTORS"]:
            QMessageBox.warning(self, "Factor rule", "Select at least one required factor.")
            return
        tickers = self.selected_tickers()
        if not tickers:
            QMessageBox.warning(self, "Tickers", "Choose a preset or enter custom tickers.")
            return
        if self.max_price.value() and self.max_price.value() < self.min_price.value():
            QMessageBox.warning(self, "Filters", "Max price is below min price.")
            return

        fs.apply_settings(s)  # safe: only one scan runs at a time
        spec = fs.parse_timeframe(s["TIMEFRAME"])
        filters = {"min_price": self.min_price.value(), "max_price": self.max_price.value(),
                   "min_volume": self.min_vol.value() * 1e6}
        self.model.clear()
        self.model.intraday = spec.intraday
        self.last_scan_meta = {
            "timeframe": spec.label, "lookback": f"{s['LOOKBACK_DAYS']} {s['LOOKBACK_MODE']}",
            "rule": self._rule_text(s), "universe": self.preset_combo.currentText()
            + (" + custom" if self.custom_edit.toPlainText().strip() else ""),
            "tickers": len(tickers), "ema": self.ema_combo.currentText(),
            "slow_align": s["REQUIRE_EMA_SLOW_ALIGN"], "filters": filters,
        }
        self._log(f"Scan started: {len(tickers)} tickers | {spec.label} | last {s['LOOKBACK_DAYS']} "
                  f"{s['LOOKBACK_MODE']} days | rule {self.last_scan_meta['rule']} | "
                  f"price {filters['min_price']:g}-{filters['max_price'] or '∞'} | "
                  f"min vol {self.min_vol.value():g}M")

        self.worker = ScanWorker(tickers, spec, filters, self.universe, self.workers_spin.value())
        self.worker.progress.connect(self._on_progress)
        self.worker.rows_ready.connect(self._on_rows)
        self.worker.log.connect(self._log)
        self.worker.finished_scan.connect(self._on_scan_finished)
        self.scan_started = time.monotonic()
        self.progress.setRange(0, len(tickers))
        self.progress.setValue(0)
        self.progress.setFormat(f"Starting… 0 / {len(tickers)}")
        self.scan_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.tabs.setCurrentIndex(0)
        self.worker.start()

    def stop_scan(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.status.setText("Stopping… (waiting for in-flight downloads)")

    def _on_progress(self, done, total, ticker):
        elapsed = max(time.monotonic() - self.scan_started, 1e-6)
        rate = done / elapsed
        eta = (total - done) / rate if rate > 0 else 0
        self.progress.setValue(done)
        self.progress.setFormat(f"Scanning ticker {done} of {total}: {ticker}  (%p%)")
        self.status.setText(f"{rate:.1f} tickers/s · elapsed {self._fmt_secs(elapsed)} · "
                            f"ETA {self._fmt_secs(eta)} · {len(self.model.rows)} signals")

    def _on_rows(self, rows):
        self.model.add_rows(rows)
        self._update_result_count()

    def _on_scan_finished(self, st):
        elapsed = time.monotonic() - self.scan_started
        self.scan_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        state = "Stopped" if st["cancelled"] else "Done"
        self.progress.setFormat(f"{state}: {st['scanned']} / {st['total']} scanned")
        summary = (f"{state} in {self._fmt_secs(elapsed)} · {st['signals']} signals on {st['hits']} tickers · "
                   f"{st['filtered']} filtered by price/volume · {st['errors']} skipped")
        self.status.setText(summary)
        self._log(summary)
        self.table.resizeColumnToContents(COLUMNS.index("Sector"))
        self._update_result_count()
        if self.autosave.isChecked() and self.model.rows and not st["cancelled"]:
            path = self._save(pd.DataFrame(self.model.rows), RESULTS_DIR / self._default_name("csv"))
            self._log(f"Auto-saved {path.name}")
            self.refresh_history()

    # ---------------------------------------------------------------- results
    def _apply_result_filter(self):
        self.proxy.set_filters(self.search.text(), self.dir_filter.currentText(), self.score_filter.value(),
                               self.first_only.isChecked())
        self._update_result_count()

    def _update_result_count(self):
        shown, total = self.proxy.rowCount(), len(self.model.rows)
        longs = sum(r["Direction"] == "LONG" for r in self.model.rows)
        self.result_count.setText(f"Showing {shown} of {total} signals ({longs} long / {total - longs} short). "
                                  "Double-click a row to open it on TradingView.")

    def _row_at(self, proxy_index) -> dict:
        return self.model.rows[self.proxy.mapToSource(proxy_index).row()]

    def _tv_url(self, row: dict) -> str:
        tf = self.tf_combo.currentText()
        label_to_tf = {fs.parse_timeframe(t).label: t for t in TIMEFRAMES}
        tf = label_to_tf.get(row.get("Timeframe", ""), tf)
        symbol = row["Ticker"].replace("-", ".")
        return f"https://www.tradingview.com/chart/?symbol={symbol}&interval={TV_INTERVAL.get(tf, 'D')}"

    def _open_row_on_tradingview(self, index):
        QDesktopServices.openUrl(QUrl(self._tv_url(self._row_at(index))))

    def _table_menu(self, pos):
        index = self.table.indexAt(pos)
        menu = QMenu(self)
        if index.isValid():
            row = self._row_at(index)
            a = QAction(f"Open {row['Ticker']} on TradingView", self)
            a.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(self._tv_url(row))))
            menu.addAction(a)
            c = QAction(f"Copy {row['Ticker']}", self)
            c.triggered.connect(lambda: QGuiApplication.clipboard().setText(row["Ticker"]))
            menu.addAction(c)
        ca = QAction("Copy all shown tickers", self)
        ca.triggered.connect(self._copy_shown_tickers)
        menu.addAction(ca)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_shown_tickers(self):
        seen = []
        for r in range(self.proxy.rowCount()):
            t = self._row_at(self.proxy.index(r, 0))["Ticker"]
            if t not in seen:
                seen.append(t)
        QGuiApplication.clipboard().setText(",".join(seen))
        self.status.setText(f"Copied {len(seen)} tickers")

    def _shown_frame(self) -> pd.DataFrame:
        rows = [self._row_at(self.proxy.index(r, 0)) for r in range(self.proxy.rowCount())]
        return pd.DataFrame(rows, columns=COLUMNS)

    def _default_name(self, ext: str) -> str:
        tf = self.last_scan_meta.get("timeframe", fs.parse_timeframe(self.tf_combo.currentText()).label)
        return f"factor_signals_{tf}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"

    def _save(self, df: pd.DataFrame, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        out = df.copy()
        if not out.empty:
            out["Signal Time"] = pd.to_datetime(out["Signal Time"]).map(
                lambda t: t.tz_localize(None) if getattr(t, "tzinfo", None) else t)
            out["Avg Vol (M)"] = out["Avg Vol (M)"].round(3)
        if path.suffix.lower() == ".xlsx":
            out.to_excel(path, index=False, sheet_name="Factor signals")
        else:
            out.to_csv(path, index=False)
        meta = dict(self.last_scan_meta, saved=datetime.now().isoformat(timespec="seconds"), signals=len(out))
        path.with_name(path.name + ".meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return path

    def export_results(self, ext: str):
        df = self._shown_frame()
        if df.empty:
            QMessageBox.information(self, "Export", "There are no results to export.")
            return
        last_dir = self.settings.value("export_dir", str(RESULTS_DIR))
        filt = "Excel workbook (*.xlsx)" if ext == "xlsx" else "CSV file (*.csv)"
        path, _ = QFileDialog.getSaveFileName(self, "Export results", str(Path(last_dir) / self._default_name(ext)), filt)
        if not path:
            return
        path = Path(path)
        if path.suffix.lower() != f".{ext}":
            path = path.with_suffix(f".{ext}")
        try:
            self._save(df, path)
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        self.settings.setValue("export_dir", str(path.parent))
        self.status.setText(f"Exported {len(df)} rows to {path}")
        self._log(f"Exported {len(df)} rows to {path}")
        self.refresh_history()

    # ---------------------------------------------------------------- history
    def _history_files(self) -> list[Path]:
        if not RESULTS_DIR.exists():
            return []
        files = [p for p in RESULTS_DIR.iterdir() if p.suffix.lower() in (".csv", ".xlsx")]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def refresh_history(self):
        files = self._history_files()
        self.hist_table.setRowCount(len(files))
        for r, p in enumerate(files):
            meta_path = p.with_name(p.name + ".meta.json")
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except ValueError:
                    meta = {}
            tf = meta.get("timeframe") or (re.search(r"factor_signals_([^_]+)_", p.name) or [None, ""])[1]
            saved = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            vals = [saved, tf, meta.get("universe", ""), meta.get("rule", ""), meta.get("lookback", ""),
                    str(meta.get("signals", "")), p.name]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, str(p))
                self.hist_table.setItem(r, c, item)
        self.hist_table.resizeColumnsToContents()

    def _selected_history_path(self) -> Path | None:
        rows = self.hist_table.selectionModel().selectedRows()
        if not rows:
            return None
        return Path(self.hist_table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole))

    def _read_saved(self, path: Path) -> pd.DataFrame:
        return pd.read_excel(path) if path.suffix.lower() == ".xlsx" else pd.read_csv(path)

    def _preview_history(self):
        path = self._selected_history_path()
        if not path or not path.exists():
            return
        try:
            df = self._read_saved(path)
        except Exception as e:
            self._log(f"Could not read {path.name}: {e}")
            return
        self.hist_preview.clear()
        self.hist_preview.setRowCount(len(df))
        self.hist_preview.setColumnCount(len(df.columns))
        self.hist_preview.setHorizontalHeaderLabels([str(c) for c in df.columns])
        dir_col = list(df.columns).index("Direction") if "Direction" in df.columns else None
        for r, row in enumerate(df.itertuples(index=False)):
            tint = None
            if dir_col is not None:
                tint = LONG_TINT if row[dir_col] == "LONG" else SHORT_TINT
            for c, v in enumerate(row):
                item = QTableWidgetItem("" if pd.isna(v) else (f"{v:,.2f}" if isinstance(v, float) else str(v)))
                if tint:
                    item.setBackground(tint)
                self.hist_preview.setItem(r, c, item)
        self.hist_preview.resizeColumnsToContents()

    def _open_history_file(self):
        path = self._selected_history_path()
        if path and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _load_history_into_results(self, *_):
        path = self._selected_history_path()
        if not path or not path.exists():
            return
        try:
            df = self._read_saved(path)
        except Exception as e:
            QMessageBox.critical(self, "History", f"Could not read {path.name}: {e}")
            return
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = None
        df["Signal Time"] = pd.to_datetime(df["Signal Time"])
        df["Sector"] = df["Sector"].fillna("")
        for col in ("Price", "Last", "Avg Vol (M)"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        df["Score"] = pd.to_numeric(df["Score"], errors="coerce").fillna(0).astype(int)
        self.model.clear()
        self.model.intraday = bool((df["Signal Time"].dt.hour != 0).any())
        self.model.add_rows(df[COLUMNS].to_dict("records"))
        self._update_result_count()
        self.tabs.setCurrentIndex(0)
        self.status.setText(f"Loaded {len(df)} saved signals from {path.name}")

    # ---------------------------------------------------------------- misc
    def _log(self, msg: str):
        self.log_view.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    @staticmethod
    def _fmt_secs(s: float) -> str:
        s = int(round(s))
        return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"

    def _persisted(self):
        """(key, getter, setter) for every control remembered between sessions."""
        def combo(w):
            return (lambda: w.currentText(), lambda v: w.setCurrentIndex(max(w.findText(v), 0)))

        def check(w):
            return (lambda: w.isChecked(), lambda v: w.setChecked(str(v).lower() in ("true", "1")))

        def spin(w, cast):
            return (lambda: w.value(), lambda v: w.setValue(cast(v)))

        items = {
            "timeframe": combo(self.tf_combo), "lb_mode": combo(self.lb_mode), "mode": combo(self.mode_combo),
            "ema": combo(self.ema_combo), "inv_mode": combo(self.inv_combo),
            "lookback": spin(self.lb_spin, int), "inv_closes": spin(self.inv_closes, int),
            "min_price": spin(self.min_price, float), "max_price": spin(self.max_price, float),
            "min_vol": spin(self.min_vol, float), "workers": spin(self.workers_spin, int),
            "closed_only": check(self.closed_only), "slow_align": check(self.chk_slow_align),
            "break": check(self.chk_break), "rearm": check(self.chk_rearm), "unlock": check(self.chk_unlock),
            "mature": check(self.chk_mature), "wicks": check(self.chk_wicks), "autosave": check(self.autosave),
            "pairs": (self.pairs_edit.text, self.pairs_edit.setText),
            "custom": (self.custom_edit.toPlainText, self.custom_edit.setPlainText),
            "factors": (lambda: "".join(k for k, cb in self.factor_checks.items() if cb.isChecked()),
                        lambda v: [cb.setChecked(k in str(v)) for k, cb in self.factor_checks.items()]),
        }
        return items

    def _restore_settings(self):
        for key, (_, setter) in self._persisted().items():
            v = self.settings.value(key)
            if v is not None:
                try:
                    setter(v)
                except (TypeError, ValueError):
                    pass
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(15000)
        for key, (getter, _) in self._persisted().items():
            self.settings.setValue(key, getter())
        if self.preset_combo.currentText() != "Loading lists…":
            self.settings.setValue("preset", self.preset_combo.currentText())
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)


# ============================================================================
# Theme + entry point
# ============================================================================

STYLE = """
QWidget { font-size: 13px; }
QGroupBox { border: 1px solid #2a2e39; border-radius: 6px; margin-top: 14px; padding: 8px 6px 6px 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #d1d4dc; font-weight: 600; }
QPushButton { background: #2a2e39; border: 1px solid #363a45; border-radius: 5px; padding: 6px 12px; }
QPushButton:hover { background: #363a45; }
QPushButton:disabled { color: #5d606b; }
QPushButton#primary { background: #2962ff; border-color: #2962ff; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #1e53e5; }
QPushButton#primary:disabled { background: #1c2a52; color: #7a86a8; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #1e222d; border: 1px solid #363a45; border-radius: 4px; padding: 4px; }
QTableView, QTableWidget { background: #131722; alternate-background-color: #171b26; gridline-color: #2a2e39;
    selection-background-color: #2962ff; border: 1px solid #2a2e39; }
QHeaderView::section { background: #1e222d; color: #b2b5be; padding: 5px; border: none;
    border-right: 1px solid #2a2e39; border-bottom: 1px solid #2a2e39; font-weight: 600; }
QProgressBar { border: 1px solid #363a45; border-radius: 4px; text-align: center; background: #1e222d; height: 20px; }
QProgressBar::chunk { background: #2962ff; border-radius: 3px; }
QTabBar::tab { background: #1e222d; padding: 7px 16px; border: 1px solid #2a2e39; border-bottom: none;
    border-top-left-radius: 5px; border-top-right-radius: 5px; margin-right: 2px; }
QTabBar::tab:selected { background: #2a2e39; color: white; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #6a6d78; border-radius: 3px; background: #1e222d; }
QCheckBox::indicator:checked { background: #2962ff; border-color: #5b86ff; }
QCheckBox::indicator:disabled { border-color: #363a45; background: #171b26; }
QCheckBox::indicator:checked:disabled { background: #1c2a52; }
"""


def apply_dark_theme(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: "#131722", QPalette.ColorRole.WindowText: "#d1d4dc",
        QPalette.ColorRole.Base: "#131722", QPalette.ColorRole.AlternateBase: "#171b26",
        QPalette.ColorRole.Text: "#d1d4dc", QPalette.ColorRole.Button: "#2a2e39",
        QPalette.ColorRole.ButtonText: "#d1d4dc", QPalette.ColorRole.Highlight: "#2962ff",
        QPalette.ColorRole.HighlightedText: "#ffffff", QPalette.ColorRole.ToolTipBase: "#1e222d",
        QPalette.ColorRole.ToolTipText: "#d1d4dc", QPalette.ColorRole.PlaceholderText: "#6a6d78",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    app.setPalette(p)
    app.setStyleSheet(STYLE)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("FACTOR CONT Screener")
    apply_dark_theme(app)
    win = MainWindow()
    win.show()
    QTimer.singleShot(0, win._update_ticker_count)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
