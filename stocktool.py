import os
import sys
import json
import time
import importlib.util
import traceback
import datetime as dt
import urllib.parse
import requests
from typing import Dict, List, Tuple
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

APP_NAME = "StockTool"
APP_VERSION = "1.2"
SETTINGS_FILE = "settings.json"
MODULES_DIR = "modules"

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from plugin_api import BaseModule  # shared plugin base

# ---------------- Yahoo API ----------------
YA_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
YA_BASE = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           "{symbol}?period1={p1}&period2={p2}&interval=1d&includePrePost=false&events=history")

# ---------------- pyqtgraph (fast, crash-proof plotting) ----------------
import pyqtgraph as pg
# Performance-first defaults
pg.setConfigOptions(
    antialias=False,      # faster lines
    useOpenGL=True,       # hardware-accelerated
    foreground='w',       # white text/ticks
    background='k'        # dark background
)

# ---- Compact UI stylesheet (global density reduction) ----
DENSITY_QSS = """
* { font-size: 11px; }

QWidget { padding: 0; margin: 0; }

QLabel { padding: 0 2px; }

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
  padding: 3px 6px;
  min-height: 0;
}

QPushButton {
  padding: 3px 8px;
  min-height: 0;
}

QTabWidget::pane { padding: 0; margin: 0; }
QTabBar::tab { padding: 4px 10px; min-height: 22px; }

QGroupBox { margin-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }

QHeaderView::section { padding: 2px 6px; }
QTableView::item { padding: 2px 4px; }

QToolTip { font-size: 11px; padding: 4px 6px; }

QScrollBar:vertical, QScrollBar:horizontal {
  margin: 0; padding: 0; min-width: 10px; min-height: 10px;
}
"""

# ====================== Data fetch ======================

def _fetch_single(ticker: str, days: int) -> List[dict]:
    if not ticker or days <= 0:
        raise ValueError("Ticker and number of days must be positive.")

    symbol = urllib.parse.quote(ticker.strip())
    now = int(time.time())
    start_time = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    start = int(start_time.timestamp())

    url = YA_BASE.format(symbol=symbol, p1=start, p2=now)
    headers = {"User-Agent": YA_USER_AGENT, "Accept": "application/json"}

    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    chart = data.get("chart", {})
    err = chart.get("error")
    if err:
        raise RuntimeError(f"Yahoo error: {err.get('code')}: {err.get('description')}")

    result_list = chart.get("result") or []
    if not result_list:
        raise RuntimeError("No result returned from Yahoo Finance.")

    result = result_list[0]
    ts = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens, closes = quote.get("open") or [], quote.get("close") or []

    rows: List[dict] = []
    for i in range(min(len(ts), len(opens), len(closes))):
        if opens[i] is None or closes[i] is None:
            continue
        d = dt.datetime.fromtimestamp(ts[i], dt.UTC).strftime("%Y-%m-%d")
        rows.append({"date": d, "open": round(float(opens[i]), 4), "close": round(float(closes[i]), 4)})
    return rows


# ====================== Settings ======================

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {"enabled_modules": []}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"enabled_modules": []}

def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ====================== Chart (pyqtgraph) ======================

class DateAxis(pg.graphicsItems.AxisItem.AxisItem):
    """Bottom axis that formats UNIX seconds -> YYYY-MM-DD (true time scale)."""
    def __init__(self, *args, **kwargs):
        super().__init__(orientation='bottom', *args, **kwargs)

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            try:
                d = dt.datetime.fromtimestamp(v, dt.UTC).date()
                out.append(d.strftime("%Y-%m-%d"))
            except Exception:
                out.append("")
        return out


class PriceAxis(pg.graphicsItems.AxisItem.AxisItem):
    """Left axis that formats prices with 2 decimals and thousands separators."""
    def __init__(self, *args, **kwargs):
        super().__init__(orientation='left', *args, **kwargs)

    def tickStrings(self, values, scale, spacing):
        return [f"{v:,.2f}" for v in values]


class PGChart(QtWidgets.QWidget):
    """Fast pyqtgraph chart using PlotCurveItem + high-variance palette, crosshair, and series toggles."""
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # Plot widget with custom axes
        self.plot = pg.PlotWidget(axisItems={'bottom': DateAxis(), 'left': PriceAxis()})
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel('bottom', 'Date')
        self.plot.setLabel('left', 'Price')
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.setClipToView(True)
        self.plot.getViewBox().setDefaultPadding(0.05)

        # Crosshair items
        self._vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen((180, 180, 180, 120)))
        self._hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen((180, 180, 180, 120)))
        self.plot.addItem(self._vline, ignoreBounds=True)
        self.plot.addItem(self._hline, ignoreBounds=True)

        # Readout
        self.readout = QtWidgets.QLabel("Hover for values")
        self.readout.setStyleSheet("color: gray; padding: 0 2px;")

        # Series toggle panel
        self.series_group = QtWidgets.QGroupBox("Series")
        self.series_layout = QtWidgets.QVBoxLayout(self.series_group)
        self.series_layout.setContentsMargins(8, 8, 8, 8)
        self.series_layout.setSpacing(4)
        self.series_note = QtWidgets.QLabel("Tick boxes to show/hide lines.")
        self.series_note.setStyleSheet("color: gray;")
        self.series_layout.addWidget(self.series_note)
        self.series_layout.addStretch(1)

        # Layout: plot on top, readout, then toggles
        lay.addWidget(self.plot, 1)
        lay.addWidget(self.readout, 0)
        lay.addWidget(self.series_group, 0)

        # Data holders
        self._series: Dict[str, pg.PlotCurveItem] = {}
        self._data_cache: Dict[str, Tuple['np.ndarray', 'np.ndarray']] = {}
        self._palette_idx = 0
        self._palette = self._make_palette()

        # Crosshair / hover
        self.plot.setMouseTracking(True)
        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)

    def _make_palette(self):
        def qc(r, g, b, a=255):
            return QtGui.QColor(int(r), int(g), int(b), int(a))
        okabe_ito = [
            qc(0,114,178), qc(213,94,0), qc(86,180,233), qc(230,159,0),
            qc(0,158,115), qc(240,228,66), qc(204,121,167), qc(0,0,0),
            qc(148,52,110), qc(50,50,50),
        ]
        tableau20 = [
            qc(31,119,180), qc(255,127,14), qc(44,160,44), qc(214,39,40),
            qc(148,103,189), qc(140,86,75), qc(227,119,194), qc(127,127,127),
            qc(188,189,34), qc(23,190,207), qc(174,199,232), qc(255,187,120),
            qc(152,223,138), qc(255,152,150), qc(197,176,213), qc(196,156,148),
            qc(247,182,210), qc(199,199,199), qc(219,219,141), qc(158,218,229)
        ]
        palette = []
        for c in okabe_ito + tableau20:
            if c.red() < 30 and c.green() < 30 and c.blue() < 30:
                c = qc(90, 90, 90)
            palette.append(c)
        h = 0.11
        gr = 0.61803398875
        for _ in range(120):
            h = (h + gr) % 1.0
            if 0.05 <= h <= 0.13:
                h = (h + 0.15) % 1.0
            c = QtGui.QColor.fromHsvF(h, 0.95, 0.98, 1.0)
            palette.append(c)
        return palette

    def _next_pen(self) -> pg.mkPen:
        color = self._palette[self._palette_idx % len(self._palette)]
        self._palette_idx += 1
        return pg.mkPen(color, width=2)

    def clear(self):
        for item in list(self._series.values()):
            try:
                self.plot.removeItem(item)
            except Exception:
                pass
        self._series.clear()
        self._data_cache.clear()
        self._palette_idx = 0

        while self.series_layout.count() > 2:
            item = self.series_layout.takeAt(1)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

        if self._vline not in self.plot.items():
            self.plot.addItem(self._vline, ignoreBounds=True)
        if self._hline not in self.plot.items():
            self.plot.addItem(self._hline, ignoreBounds=True)

        self.plot.enableAutoRange('xy', True)

    def add_line(self, label: str, dates_utc_str: List[str], y_values: List[float]):
        import numpy as np
        xs = np.array(
            [dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.UTC).timestamp() for s in dates_utc_str],
            dtype=float
        )
        ys = np.array(y_values, dtype=float)

        pen = self._next_pen()
        curve = pg.PlotCurveItem(
            x=xs, y=ys, pen=pen,
            antialias=False,
            clipToView=True
        )
        self.plot.addItem(curve)

        self._series[label] = curve
        self._data_cache[label] = (xs, ys)
        self._add_series_checkbox(label, curve)
        self.plot.enableAutoRange('xy', True)

    def _add_series_checkbox(self, label: str, curve: pg.PlotCurveItem):
        cb = QtWidgets.QCheckBox(label)
        cb.setChecked(True)
        def _toggle(_state):
            curve.setVisible(cb.isChecked())
            self.plot.enableAutoRange('xy', True)
        cb.stateChanged.connect(_toggle)
        self.series_layout.insertWidget(self.series_layout.count() - 1, cb)

    def _on_mouse_moved(self, pos):
        if not self._series:
            self.readout.setText("Hover for values")
            return
        vb = self.plot.getViewBox()
        if vb is None:
            return
        mouse_point = vb.mapSceneToView(pos)
        x = float(mouse_point.x())
        y = float(mouse_point.y())

        self._vline.setPos(x)
        self._hline.setPos(y)

        lines = []
        try:
            date_str = dt.datetime.fromtimestamp(x, dt.UTC).strftime("%Y-%m-%d")
        except Exception:
            date_str = ""
        lines.append(date_str)

        import bisect
        for label, (xs, ys) in self._data_cache.items():
            if xs.size == 0:
                continue
            idx = bisect.bisect_left(xs, x)
            if idx >= xs.size:
                idx = xs.size - 1
            if idx > 0 and abs(xs[idx] - x) > abs(xs[idx - 1] - x):
                idx -= 1
            val = ys[idx]
            curve = self._series.get(label)
            if curve is not None and curve.isVisible():
                lines.append(f"{label}: {val:,.2f}")

        self.readout.setText("   |   ".join(lines))


# ====================== Main App ======================

class StockTool(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1100, 700)

        # State (multi-asset)
        self.data_by_symbol: Dict[str, List[dict]] = {}
        self.tickers: List[str] = []

        self.settings = load_settings()
        self.loaded_modules: Dict[str, BaseModule] = {}
        self.module_specs: Dict[str, dict] = {}

        self._build_ui()
        self._discover_modules()
        self._populate_modules_tab()
        self._apply_enabled_modules()

    # ---------------- UI ----------------
    def _build_ui(self):
        # Tabs still at root
        self.tabs = QtWidgets.QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setElideMode(QtCore.Qt.TextElideMode.ElideRight)

        main_tab = QtWidgets.QWidget()
        modules_tab = QtWidgets.QWidget()
        self.tabs.addTab(main_tab, "Main")
        self.tabs.addTab(modules_tab, "Modules")

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)
        root.addWidget(self.tabs)

        # ==== Main tab ====
        main_layout = QtWidgets.QVBoxLayout(main_tab)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # Vertical splitter: top = main content; bottom = scrollable modules page
        self.vertical_split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.vertical_split.setChildrenCollapsible(False)
        self.vertical_split.setHandleWidth(6)

        # -------- Main content (top of vertical splitter) --------
        self.main_container = QtWidgets.QWidget()
        top = QtWidgets.QVBoxLayout(self.main_container)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)

        # Top input bar
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)

        self.ticker_edit = QtWidgets.QLineEdit()
        self.ticker_edit.setPlaceholderText("Tickers (comma-separated, e.g., AAPL, MSFT, NVDA, ^IXIC)")
        self.ticker_edit.setText("QQQ")

        days_layout = QtWidgets.QHBoxLayout()
        days_layout.setContentsMargins(0, 0, 0, 0)
        days_layout.setSpacing(3)

        self.days_edit = QtWidgets.QLineEdit()
        self.days_edit.setPlaceholderText("e.g. 7, 365, 5000")
        self.days_edit.setText("365")
        self.days_edit.setMinimumWidth(120)

        days_label = QtWidgets.QLabel("days")
        days_label.setStyleSheet("color: gray;")

        days_layout.addWidget(self.days_edit)
        days_layout.addWidget(days_label)

        self.fetch_btn = QtWidgets.QPushButton("Fetch")
        self.fetch_btn.clicked.connect(self.on_fetch)

        self.ticker_edit.returnPressed.connect(self.fetch_btn.click)
        self.days_edit.returnPressed.connect(self.fetch_btn.click)

        row.addWidget(QtWidgets.QLabel("Tickers:"))
        row.addWidget(self.ticker_edit, 3)
        row.addSpacing(8)
        row.addWidget(QtWidgets.QLabel("History:"))
        row.addLayout(days_layout, 1)
        row.addSpacing(8)
        row.addWidget(self.fetch_btn, 0)

        # Horizontal splitter for table + chart (inside the main container)
        hsplit = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        hsplit.setHandleWidth(6)
        hsplit.setChildrenCollapsible(False)

        # Table
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Ticker", "Date", "Open", "Close"])
        self._compact_table(self.table)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)

        left = QtWidgets.QWidget()
        left_lay = QtWidgets.QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)
        left_lay.addWidget(self.table)

        # Chart panel
        right = QtWidgets.QWidget()
        right_lay = QtWidgets.QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(4)
        self.chart = PGChart()
        right_lay.addWidget(self.chart)

        hsplit.addWidget(left)
        hsplit.addWidget(right)
        # Give the chart more room
        hsplit.setStretchFactor(0, 1)  # table
        hsplit.setStretchFactor(1, 3)  # chart 3x table

        # Status
        self.status = QtWidgets.QLabel("Ready.")
        self.status.setStyleSheet("color: gray;")

        top.addLayout(row)
        top.addWidget(hsplit, 1)
        top.addWidget(self.status)

        # -------- Modules area (bottom of vertical splitter) --------
        # Scrollable, “infinite” vertical page so modules can render at full size.
        self.modules_scroll = QtWidgets.QScrollArea()
        self.modules_scroll.setWidgetResizable(True)
        self.modules_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.modules_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # The scrollable content widget
        self.modules_content = QtWidgets.QWidget()
        self.modules_scroll.setWidget(self.modules_content)

        self.modules_vbox = QtWidgets.QVBoxLayout(self.modules_content)
        self.modules_vbox.setContentsMargins(8, 8, 8, 16)
        self.modules_vbox.setSpacing(8)

        # Optional title/legend at the top of the scroll page
        modules_title = QtWidgets.QLabel("Modules")
        modules_title.setStyleSheet("font-weight:600;font-size:13px;")
        self.modules_vbox.addWidget(modules_title)

        self.crypto_note = QtWidgets.QLabel("Note: For cryptocurrencies, Change % is since your local midnight.")
        self.crypto_note.setStyleSheet("color:#a2a9b6;font-size:11px;")
        self.modules_vbox.addWidget(self.crypto_note)

        # Keep content pinned to the top; stretch at the end
        self.modules_vbox.addStretch(1)

        # Put the two sections into the vertical splitter
        self.vertical_split.addWidget(self.main_container)
        self.vertical_split.addWidget(self.modules_scroll)

        # Bias towards main content; modules scroll instead of squashing
        self.vertical_split.setStretchFactor(0, 3)  # main
        self.vertical_split.setStretchFactor(1, 2)  # modules
        # Optional: initial sizes (pixels)
        # self.vertical_split.setSizes([900, 250])

        # Add splitter to tab
        main_layout.addWidget(self.vertical_split, 1)

        # ==== Modules tab (list & toggles) ====
        self._build_modules_tab(modules_tab)

    def _build_modules_tab(self, tab_widget: QtWidgets.QWidget):
        lay = QtWidgets.QVBoxLayout(tab_widget)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        info = QtWidgets.QLabel(
            "Modules found in the 'modules' folder appear here.\n"
            "Toggle modules on/off. Changes persist to settings.json."
        )
        info.setStyleSheet("color: gray;")
        lay.addWidget(info)

        self.modules_list = QtWidgets.QTableWidget(0, 3)
        self.modules_list.setHorizontalHeaderLabels(["Enabled", "Module", "Description"])
        self.modules_list.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.modules_list.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.modules_list.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._compact_table(self.modules_list)

        lay.addWidget(self.modules_list, 1)

        self.save_btn = QtWidgets.QPushButton("Save")
        self.save_btn.clicked.connect(self._save_module_settings)
        lay.addWidget(self.save_btn, 0, QtCore.Qt.AlignmentFlag.AlignRight)

    # --------------- Module management ---------------
    def _discover_modules(self):
        if not os.path.isdir(MODULES_DIR):
            os.makedirs(MODULES_DIR, exist_ok=True)

        init_path = os.path.join(MODULES_DIR, "__init__.py")
        if not os.path.exists(init_path):
            open(init_path, "a", encoding="utf-8").close()

        for fname in os.listdir(MODULES_DIR):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue

            path = os.path.join(MODULES_DIR, fname)
            mod_name = f"modules.{fname[:-3]}"

            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                if not spec or not spec.loader:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                module_class = None
                for obj_name in dir(mod):
                    obj = getattr(mod, obj_name)
                    try:
                        if isinstance(obj, type) and issubclass(obj, BaseModule) and obj is not BaseModule:
                            module_class = obj
                            break
                    except Exception:
                        pass

                if not module_class:
                    continue

                temp: BaseModule = module_class()
                m_id = getattr(temp, "MODULE_ID", None)
                m_name = getattr(temp, "MODULE_NAME", None)
                m_desc = getattr(temp, "MODULE_DESC", "")

                if not m_id or not m_name:
                    continue

                self.module_specs[m_id] = {
                    "name": m_name,
                    "desc": m_desc,
                    "factory": module_class,
                }

            except Exception:
                print(f"[Module load error] {fname}\n{traceback.format_exc()}")

    def _populate_modules_tab(self):
        self.modules_list.setRowCount(0)
        enabled = set(self.settings.get("enabled_modules", []))
        for m_id, meta in sorted(self.module_specs.items(), key=lambda x: x[1]["name"].lower()):
            row = self.modules_list.rowCount()
            self.modules_list.insertRow(row)

            chk = QtWidgets.QCheckBox()
            chk.setChecked(m_id in enabled)
            chk.stateChanged.connect(lambda state, mid=m_id: self._toggle_module(mid, bool(state)))
            self.modules_list.setCellWidget(row, 0, chk)

            name_item = QtWidgets.QTableWidgetItem(meta["name"])
            name_item.setFlags(name_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.modules_list.setItem(row, 1, name_item)

            desc_item = QtWidgets.QTableWidgetItem(meta["desc"])
            desc_item.setFlags(desc_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.modules_list.setItem(row, 2, desc_item)

    def _toggle_module(self, module_id: str, enable: bool):
        enabled = set(self.settings.get("enabled_modules", []))
        if enable:
            if module_id not in enabled:
                enabled.add(module_id)
                self._instantiate_module(module_id)
        else:
            if module_id in enabled:
                enabled.remove(module_id)
                self._remove_module_widget(module_id)

        self.settings["enabled_modules"] = sorted(enabled)
        save_settings(self.settings)

    def _instantiate_module(self, module_id: str):
        if module_id in self.loaded_modules:
            return
        spec = self.module_specs.get(module_id)
        if not spec:
            return
        try:
            instance: BaseModule = spec["factory"]()
            self.loaded_modules[module_id] = instance
            # Insert above the final stretch in the scrollable column
            self.modules_vbox.insertWidget(self.modules_vbox.count() - 1, instance)
            instance.on_enable()
            if self.data_by_symbol:
                instance.on_data(self.data_by_symbol, self.tickers)
        except Exception:
            print(f"[Module instantiate error] {module_id}\n{traceback.format_exc()}")

    def _remove_module_widget(self, module_id: str):
        inst = self.loaded_modules.pop(module_id, None)
        if inst is not None:
            try:
                inst.on_disable()
            except Exception:
                pass
            inst.setParent(None)
            inst.deleteLater()

    def _apply_enabled_modules(self):
        for m_id in self.settings.get("enabled_modules", []):
            self._instantiate_module(m_id)

    def _save_module_settings(self):
        save_settings(self.settings)

    # ---------------- Fetch & render ----------------

    def on_fetch(self):
        # Parse comma-separated tickers
        raw = self.ticker_edit.text().strip()
        tickers = [t.strip() for t in raw.split(",") if t.strip()]
        # dedupe, keep order
        seen = set()
        ordered = []
        for t in tickers:
            u = t.upper()
            if u not in seen:
                seen.add(u)
                ordered.append(u)

        days_text = self.days_edit.text().strip()
        try:
            days = int(days_text)
        except ValueError:
            self.status.setText("❌ Invalid number of days. Please enter a positive integer.")
            return
        if days <= 0:
            self.status.setText("❌ Days must be positive.")
            return
        if not ordered:
            self.status.setText("❌ Please enter at least one ticker.")
            return

        self.status.setText("Fetching...")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            data_by_symbol: Dict[str, List[dict]] = {}
            failed: List[str] = []

            for sym in ordered:
                try:
                    rows = _fetch_single(sym, days)
                    if rows:
                        data_by_symbol[sym] = rows
                except Exception as e:
                    failed.append(f"{sym} ({e})")

            self.data_by_symbol = data_by_symbol
            self.tickers = [s for s in ordered if s in data_by_symbol]

            # Update UI
            self._populate_table_multi(self.data_by_symbol)
            self._plot_multi(self.data_by_symbol)

            # Status
            parts = []
            if self.tickers:
                parts.append(f"✅ Loaded {', '.join(self.tickers)}")
            if failed:
                parts.append(f"⚠️ Failed: {', '.join(failed)}")
            self.status.setText(" | ".join(parts) if parts else "⚠️ No data returned.")

            # Notify modules
            self._notify_modules()

        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _notify_modules(self):
        for m in self.loaded_modules.values():
            try:
                m.on_data(self.data_by_symbol, self.tickers)
            except Exception:
                print(f"[Module on_data error]\n{traceback.format_exc()}")

    def _populate_table_multi(self, data_by_symbol: Dict[str, List[dict]]):
        flat = []
        for sym, rows in data_by_symbol.items():
            for r in rows:
                flat.append((sym, r["date"], r["open"], r["close"]))
        flat.sort(key=lambda x: (x[0], x[1]))

        self.table.setRowCount(len(flat))
        for i, (sym, date, opn, cls) in enumerate(flat):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(sym))
            self.table.setItem(i, 1, QtWidgets.QTableWidgetItem(date))
            self.table.setItem(i, 2, QtWidgets.QTableWidgetItem(str(opn)))
            self.table.setItem(i, 3, QtWidgets.QTableWidgetItem(str(cls)))
        if flat:
            self.table.scrollToBottom()
        else:
            self.table.setRowCount(0)

    def _plot_multi(self, data_by_symbol: Dict[str, List[dict]]):
        self.chart.clear()
        for sym, rows in data_by_symbol.items():
            if not rows:
                continue
            dates = [r["date"] for r in rows]
            closes = [r["close"] for r in rows]
            self.chart.add_line(f"{sym} Close", dates, closes)

    def _compact_table(self, tbl: QtWidgets.QTableWidget):
        tbl.setAlternatingRowColors(True)
        tbl.setWordWrap(False)
        tbl.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        tbl.verticalHeader().setDefaultSectionSize(22)
        tbl.verticalHeader().setVisible(False)
        tbl.horizontalHeader().setHighlightSections(False)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        try:
            pass
        finally:
            return super().closeEvent(event)


# ---------------- Icons / Windows AUMID ----------------

def _set_app_icon_and_aumid(app: QtWidgets.QApplication, window: QtWidgets.QWidget):
    """Set window/taskbar icon and Windows AppUserModelID for correct taskbar grouping."""
    ico_path = Path(__file__).parent / "assets" / "stocktool.ico"
    if ico_path.exists():
        icon = QtGui.QIcon(str(ico_path))
        app.setWindowIcon(icon)
        window.setWindowIcon(icon)
    else:
        print(f"⚠️ Icon not found: {ico_path}")

    if sys.platform.startswith("win"):
        try:
            import ctypes
            aumid = f"{APP_NAME}.{APP_VERSION}"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(aumid)
        except Exception as e:
            print(f"⚠️ Failed to set AppUserModelID: {e}")


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(DENSITY_QSS)

    w = StockTool()
    _set_app_icon_and_aumid(app, w)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
