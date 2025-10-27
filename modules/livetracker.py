# modules/livetracker.py
from __future__ import annotations

import math
import time
import threading
import datetime as dt
from typing import Dict, List, Optional

import requests
from PyQt6 import QtCore, QtGui, QtWidgets

# Match NewsTicker import style
from plugin_api import BaseModule

# ---------- Yahoo endpoints / headers ----------
YA_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome Safari"
)
HEADERS = {"User-Agent": YA_UA, "Accept": "application/json"}

# Daily bars for prev-close & meta
YA_CHART_DAILY = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
    "{symbol}?period1={p1}&period2={p2}&interval=1d&includePrePost=false&events=history"
)

# 1-minute bars for crypto since-local-midnight calc
YA_CHART_1M = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
    "{symbol}?period1={p1}&period2={p2}&interval=1m&includePrePost=false"
)

SETTINGS_SCOPE = "StockTool/LiveTracker"
SET_REFRESH_SEC = "refresh_seconds"


class LivePriceTrackerModule(BaseModule):
    MODULE_ID   = "live_price_tracker"
    MODULE_NAME = "Live Price Tracker"
    MODULE_DESC = "Live quotes. Stocks: change vs prev close. Crypto: % since your local midnight."

    pricesSig = QtCore.pyqtSignal(dict)  # {"AAPL": {...}, "BTC-USD": {...}}
    statusSig = QtCore.pyqtSignal(str)

    COL_SYMBOL = 0
    COL_PRICE  = 1
    COL_CHG    = 2
    COL_PCT    = 3
    COL_HIGH   = 4
    COL_LOW    = 5
    COL_VOL    = 6
    COL_TIME   = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._symbols: List[str] = []
        self._row_for_sym: Dict[str, int] = {}
        self._last: Dict[str, dict] = {}

        self._session = requests.Session()
        self._session.headers.update(HEADERS)

        self._build_ui()

        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.timeout.connect(self._kick_refresh)

        self.pricesSig.connect(self._merge_prices_ui)
        # Use a defined status slot
        self.statusSig.connect(self._on_status)

        # ---- settings (these EXIST below) ----
        self._load_settings()
        self._reset_refresh_interval()

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        hdr = QtWidgets.QHBoxLayout()
        hdr.setSpacing(6)
        title = QtWidgets.QLabel("Live Price Tracker")
        title.setStyleSheet("font-weight:600;font-size:14px;")
        self._count = QtWidgets.QLabel("0 symbols")
        self._count.setStyleSheet("color:#8892a6;font-size:12px;")
        hdr.addWidget(title)
        hdr.addStretch(1)
        hdr.addWidget(self._count)

        ctr = QtWidgets.QHBoxLayout()
        ctr.setSpacing(6)
        lb = QtWidgets.QLabel("Refresh (s):")
        lb.setStyleSheet("font-size:12px;")
        self.refresh_sb = QtWidgets.QSpinBox()
        self.refresh_sb.setRange(2, 60)
        self.refresh_sb.setValue(5)
        self.refresh_sb.setFixedWidth(60)
        self.refresh_sb.valueChanged.connect(self._reset_refresh_interval)
        now_btn = QtWidgets.QPushButton("Refresh Now")
        now_btn.clicked.connect(self._kick_refresh)
        ctr.addWidget(lb)
        ctr.addWidget(self.refresh_sb)
        ctr.addStretch(1)
        ctr.addWidget(now_btn)

        legend = QtWidgets.QLabel("Crypto change % is since your local midnight.")
        legend.setStyleSheet("color:#a2a9b6;font-size:11px;")

        self._filter = QtWidgets.QLineEdit()
        self._filter.setPlaceholderText("Filter (e.g., AAPL, BTC-USD)")
        self._filter.textChanged.connect(self._apply_filter)

        self._table = QtWidgets.QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["Symbol", "Price", "Change", "Change %", "High", "Low", "Volume", "Updated"]
        )
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(24)
        hh = self._table.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(self.COL_SYMBOL, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_PRICE,  QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_CHG,    QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_PCT,    QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_HIGH,   QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_LOW,    QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_VOL,    QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_TIME,   QtWidgets.QHeaderView.ResizeMode.Stretch)

        self._status = QtWidgets.QLabel("")
        self._status.setStyleSheet("color:#a2a9b6;font-size:12px;")

        root.addLayout(hdr)
        root.addLayout(ctr)
        root.addWidget(legend)
        root.addWidget(self._filter)
        root.addWidget(self._table, 1)
        root.addWidget(self._status)

    # ---------------- BaseModule hooks ----------------
    def on_enable(self):
        self._running = True
        self._reset_refresh_interval()
        self._kick_refresh()

    def on_disable(self):
        self._running = False
        self._refresh_timer.stop()

    def on_data(self, data_by_symbol: Dict[str, List[dict]], tickers: List[str]):
        self._symbols = [s.strip().upper() for s in tickers if s.strip()]
        self._sync_rows_to_symbols()
        self._count.setText(f"{len(self._symbols)} symbols")
        self._kick_refresh()

    # ---------------- Timers / Refresh ----------------
    def _reset_refresh_interval(self):
        self._refresh_timer.stop()
        self._refresh_timer.start(self.refresh_sb.value() * 1000)
        self._save_settings()

    def _kick_refresh(self):
        if not self._running or not self._symbols:
            return
        t = threading.Thread(target=self._refresh_worker, daemon=True)
        t.start()

    def _refresh_worker(self):
        try:
            prices = self._fetch_prices_combo(self._symbols)
            self.pricesSig.emit(prices)
            self.statusSig.emit(f"Updated {len(prices)} symbols.")
        except Exception as e:
            self.statusSig.emit(f"Refresh error: {e}")

    # ---------------- Fetch logic (stocks + crypto) ----------------
    def _fetch_prices_combo(self, symbols: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        if not symbols:
            return out

        now_utc = dt.datetime.now(dt.timezone.utc)
        now_ts = int(now_utc.timestamp())

        # Local midnight in user's timezone -> UTC seconds
        local_now = dt.datetime.now().astimezone()
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_utc = int(local_midnight.astimezone(dt.timezone.utc).timestamp())

        p2 = now_ts
        p1 = now_ts - 14 * 24 * 3600  # 14 days daily window ensures a prev bar exists

        daily_meta: Dict[str, dict] = {}
        for sym in symbols:
            js = self._safe_chart_json(YA_CHART_DAILY.format(symbol=sym, p1=p1, p2=p2))
            if not js:
                continue
            res = (js.get("chart") or {}).get("result") or []
            if not res:
                continue
            node = res[0]
            meta = node.get("meta") or {}
            qlist = (node.get("indicators") or {}).get("quote") or []
            highs = lows = vols = closes = []
            if qlist:
                q = qlist[0]
                highs  = q.get("high") or []
                lows   = q.get("low")  or []
                vols   = q.get("volume") or []
                closes = q.get("close") or []

            price = meta.get("regularMarketPrice")
            if price is None and closes:
                last = closes[-1]
                if last is not None:
                    price = float(last)

            prev_close = meta.get("previousClose")
            if prev_close is None and len(closes) >= 2 and closes[-2] is not None:
                prev_close = float(closes[-2])

            market_time = meta.get("regularMarketTime") or 0
            tstr = self._fmt_time(market_time)

            daily_meta[sym] = {
                "inst": (meta.get("instrumentType") or "").upper(),
                "currency": meta.get("currency") or "",
                "price": float(price) if price is not None else None,
                "prev_close": float(prev_close) if prev_close is not None else None,
                "high": float(highs[-1]) if highs else None,
                "low":  float(lows[-1]) if lows else None,
                "vol":  int(vols[-1]) if vols else None,
                "tstr": tstr,
            }

        for sym in symbols:
            base = daily_meta.get(sym)
            if not base:
                continue

            inst = base["inst"]
            is_crypto = (inst == "CRYPTOCURRENCY") or sym.endswith("-USD")

            if is_crypto:
                # 1m since local midnight (with 5m buffer)
                js = self._safe_chart_json(YA_CHART_1M.format(symbol=sym, p1=midnight_utc - 300, p2=now_ts))
                midnight_px = None
                latest_px = base.get("price")

                if js:
                    res = (js.get("chart") or {}).get("result") or []
                    if res:
                        node = res[0]
                        ts = node.get("timestamp") or []
                        ql = (node.get("indicators") or {}).get("quote") or []
                        closes = (ql[0].get("close") or []) if ql else []
                        for tval, cval in zip(ts, closes):
                            if cval is None:
                                continue
                            if int(tval) >= midnight_utc:
                                midnight_px = float(cval)
                                break
                        if latest_px is None and closes:
                            last = closes[-1]
                            if last is not None:
                                latest_px = float(last)

                chg = pct = None
                if midnight_px is not None and latest_px is not None and midnight_px != 0:
                    chg = latest_px - midnight_px
                    pct = (chg / midnight_px) * 100.0

                out[sym] = {
                    "symbol": sym,
                    "is_crypto": True,
                    "price": latest_px,
                    "chg": chg,
                    "pct": pct,
                    "chg_basis": "since_local_midnight",
                    "high": base.get("high"),
                    "low":  base.get("low"),
                    "vol":  base.get("vol"),
                    "currency": base.get("currency"),
                    "tstr": base.get("tstr"),
                }

            else:
                price = base.get("price")
                prev_close = base.get("prev_close")
                chg = pct = None
                if price is not None and prev_close not in (None, 0):
                    chg = float(price) - float(prev_close)
                    pct = (chg / float(prev_close)) * 100.0

                out[sym] = {
                    "symbol": sym,
                    "is_crypto": False,
                    "price": price,
                    "chg": chg,
                    "pct": pct,
                    "chg_basis": "prev_close",
                    "high": base.get("high"),
                    "low":  base.get("low"),
                    "vol":  base.get("vol"),
                    "currency": base.get("currency"),
                    "tstr": base.get("tstr"),
                }

        return out

    # ---------------- HTTP helpers ----------------
    def _safe_chart_json(self, url: str, retries: int = 2, timeout: int = 12) -> Optional[dict]:
        last = None
        for _ in range(max(1, retries)):
            try:
                r = self._session.get(url, timeout=timeout)
                if r.status_code == 401:
                    # retry once; Yahoo can 401 transiently
                    r = self._session.get(url, timeout=timeout)
                r.raise_for_status()
                js = r.json()
                if (js.get("chart") or {}).get("error"):
                    return None
                return js
            except Exception as e:
                last = e
                time.sleep(0.2)
        return None

    # ---------------- UI merge & rendering ----------------
    @QtCore.pyqtSlot(dict)
    def _merge_prices_ui(self, fresh: Dict[str, dict]):
        if not fresh:
            return
        self._sync_rows_to_symbols()

        any_crypto = False
        for sym in self._symbols:
            info = fresh.get(sym)
            if not info:
                self._set_text(sym, self.COL_TIME, "—")
                continue
            self._last[sym] = info
            if info.get("is_crypto"):
                any_crypto = True

            self._set_text(sym, self.COL_PRICE, self._fmt_price(info.get("price")))
            self._paint_change(sym, info.get("chg"), info.get("pct"))
            self._set_text(sym, self.COL_HIGH, self._fmt_price(info.get("high")))
            self._set_text(sym, self.COL_LOW,  self._fmt_price(info.get("low")))
            self._set_text(sym, self.COL_VOL,  self._fmt_int(info.get("vol")))
            basis = "since midnight" if info.get("chg_basis") == "since_local_midnight" else "vs prev close"
            self._set_text(sym, self.COL_TIME, f"{info.get('tstr') or '—'} • {basis}")

        labels = ["Symbol", "Price", "Change", "Change %", "High", "Low", "Volume", "Updated"]
        if any_crypto:
            labels[3] = "Change %*"
        self._table.setHorizontalHeaderLabels(labels)

    # --- status label slots ---
    @QtCore.pyqtSlot(str)
    def _on_status(self, msg: str):
        self._status.setText(msg)

    # (kept for back-compat if you wire to _set_status elsewhere)
    @QtCore.pyqtSlot(str)
    def _set_status(self, msg: str):
        self._status.setText(msg)

    # ---------------- Rows & filtering ----------------
    def _sync_rows_to_symbols(self):
        current = set(self._symbols)
        for sym in list(self._row_for_sym.keys()):
            if sym not in current:
                row = self._row_for_sym.pop(sym)
                self._table.removeRow(row)
                self._reindex_rows()
        for sym in self._symbols:
            self._ensure_row(sym)
        self._count.setText(f"{len(self._symbols)} symbols")
        self._apply_filter(self._filter.text())

    def _ensure_row(self, sym: str):
        if sym in self._row_for_sym:
            return
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._row_for_sym[sym] = row
        for col in range(self._table.columnCount()):
            it = QtWidgets.QTableWidgetItem("")
            if col in (self.COL_PRICE, self.COL_CHG, self.COL_PCT, self.COL_HIGH, self.COL_LOW, self.COL_VOL):
                it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            else:
                it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
            f = it.font()
            f.setPointSizeF(max(9.0, f.pointSizeF()))
            it.setFont(f)
            self._table.setItem(row, col, it)
        sym_item = self._table.item(row, self.COL_SYMBOL)
        f = sym_item.font()
        f.setBold(True)
        sym_item.setFont(f)
        sym_item.setText(sym)

    def _reindex_rows(self):
        remap: Dict[str, int] = {}
        for r in range(self._table.rowCount()):
            it = self._table.item(r, self.COL_SYMBOL)
            if it:
                remap[it.text()] = r
        self._row_for_sym = remap

    def _row(self, sym: str) -> Optional[int]:
        return self._row_for_sym.get(sym)

    def _set_text(self, sym: str, col: int, txt: str):
        row = self._row(sym)
        if row is None:
            return
        it = self._table.item(row, col)
        if it is None:
            it = QtWidgets.QTableWidgetItem(txt)
            self._table.setItem(row, col, it)
        else:
            it.setText(txt)

    def _paint_change(self, sym: str, chg: Optional[float], pct: Optional[float]):
        row = self._row(sym)
        if row is None:
            return
        ch_it = self._table.item(row, self.COL_CHG)
        pc_it = self._table.item(row, self.COL_PCT)

        def paint(item: QtWidgets.QTableWidgetItem, val: Optional[float], suffix: str):
            if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
                item.setText("—")
                item.setForeground(QtGui.QBrush(QtGui.QColor("#c0c4cf")))
                return
            up = val >= 0
            arrow = "▲" if up else "▼"
            col = QtGui.QColor("#16a34a" if up else "#dc2626")
            if suffix == "%":
                item.setText(f"{arrow} {abs(val):,.2f}{suffix}")
            else:
                item.setText(f"{arrow} {abs(val):,.2f}")
            item.setForeground(QtGui.QBrush(col))

        paint(ch_it, chg, "")
        paint(pc_it, pct, "%")

    def _apply_filter(self, text: str):
        q = (text or "").strip().upper()
        for sym, row in self._row_for_sym.items():
            show = (q in sym.upper()) if q else True
            self._table.setRowHidden(row, not show)

    # ---------------- Settings (these are what the error said were missing) ----------------
    def _load_settings(self):
        s = QtCore.QSettings(SETTINGS_SCOPE, SETTINGS_SCOPE)
        self.refresh_sb.setValue(s.value(SET_REFRESH_SEC, self.refresh_sb.value(), type=int))

    def _save_settings(self):
        s = QtCore.QSettings(SETTINGS_SCOPE, SETTINGS_SCOPE)
        s.setValue(SET_REFRESH_SEC, self.refresh_sb.value())

    # ---------------- Formatting helpers ----------------
    @staticmethod
    def _fmt_price(v: Optional[float]) -> str:
        if v is None:
            return "—"
        try:
            return f"{v:,.2f}" if v >= 1 else f"{v:,.4f}"
        except Exception:
            return "—"

    @staticmethod
    def _fmt_int(v: Optional[int | float]) -> str:
        if v is None:
            return "—"
        try:
            return f"{int(v):,}"
        except Exception:
            return "—"

    @staticmethod
    def _fmt_time(ts: Optional[int]) -> str:
        if not ts:
            return "—"
        try:
            return QtCore.QDateTime.fromSecsSinceEpoch(int(ts)).toString("yyyy-MM-dd hh:mm:ss")
        except Exception:
            return "—"


MODULE_CLASS = LivePriceTrackerModule
