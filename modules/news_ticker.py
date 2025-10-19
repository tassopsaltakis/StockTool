import re
import time
import threading
import requests
from typing import Dict, List, Optional, Tuple

from PyQt6 import QtCore, QtGui, QtWidgets

try:
    import feedparser  # optional
    FEEDPARSER_OK = True
except Exception:
    FEEDPARSER_OK = False

from plugin_api import BaseModule

YA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome Safari"
)
YA_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}"

SETTINGS_SCOPE   = "StockTool/NewsTicker"
SET_FEEDS        = "feeds"
SET_SPEED        = "speed"
SET_REFRESH_SEC  = "refresh_seconds"

class NewsTickerModule(BaseModule):
    MODULE_ID   = "news_ticker"
    MODULE_NAME = "News Ticker"
    MODULE_DESC = "Scrolling headlines with detected tickers, live price, and ▲/▼ change."

    # Worker → UI
    itemsSig  = QtCore.pyqtSignal(list)
    statusSig = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False

        # Data/state (UI-thread)
        self._items: List[dict] = []
        self._seen_keys: set[Tuple[str,str,str]] = set()   # (title, link, sym)
        self._prices: Dict[str, dict] = {}
        self._price_cache_ts: float = 0.0

        # Marquee state
        self._x_offset = 0
        self._hover_pause = False
        self._sep_html = " &nbsp;&nbsp;<span style='color:#3b4154'>•</span>&nbsp;&nbsp; "

        # Feeds
        self._default_feeds = [
            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
            "https://www.investing.com/rss/news.rss",
            "https://www.marketwatch.com/feeds/topstories",
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        ]

        self._build_ui()
        self._load_settings()

        # Timers
        self._scroll_timer = QtCore.QTimer(self)
        self._scroll_timer.timeout.connect(self._tick_scroll)
        self._scroll_timer.start(16)  # ~60fps

        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh_now)
        self._reset_refresh_interval()

        # Signals
        self.itemsSig.connect(self._merge_items_ui)
        self.statusSig.connect(self._set_status)

        # First prime
        QtCore.QTimer.singleShot(150, self.refresh_now)

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(8)

        self.feeds_edit = QtWidgets.QPlainTextEdit()
        self.feeds_edit.setPlaceholderText("One RSS/Atom URL per line")
        self.feeds_edit.setFixedHeight(80)
        self.feeds_edit.setPlainText("\n".join(self._default_feeds))

        # Refresh (seconds) — keep
        refresh_every_lbl = QtWidgets.QLabel("Refresh (s):")
        self.refresh_every_sb = QtWidgets.QSpinBox()
        self.refresh_every_sb.setRange(3, 60)
        self.refresh_every_sb.setValue(8)
        self.refresh_every_sb.valueChanged.connect(self._reset_refresh_interval)

        # Speed — keep (default to the LOWEST always)
        speed_label = QtWidgets.QLabel("Speed:")
        self.speed_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.speed_slider.setMinimum(1)
        self.speed_slider.setMaximum(40)
        self.speed_slider.setValue(1)  # lowest by default

        refresh_btn = QtWidgets.QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_now)

        controls.addWidget(QtWidgets.QLabel("Feeds:"))
        controls.addWidget(self.feeds_edit, 3)
        controls.addSpacing(6)
        controls.addWidget(refresh_every_lbl)
        controls.addWidget(self.refresh_every_sb)
        controls.addSpacing(6)
        controls.addWidget(speed_label)
        controls.addWidget(self.speed_slider, 1)
        controls.addWidget(refresh_btn)

        # Ticker container with fades
        self.scroll_area = QtWidgets.QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.ticker_container = HoverPauseWidget(self)
        self.ticker_container.entered.connect(self._on_hover(True))
        self.ticker_container.exited.connect(self._on_hover(False))
        self.ticker_container.setStyleSheet(
            "QWidget{background:#0f1115;border-radius:12px;border:1px solid #1e2230;}"
        )

        lay = QtWidgets.QHBoxLayout(self.ticker_container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.ticker_label = QtWidgets.QLabel("")
        self.ticker_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.ticker_label.setOpenExternalLinks(True)
        self.ticker_label.setStyleSheet('QLabel{font:14px "Inter","Segoe UI",Arial;color:#f5f7fa;}')

        lay.addSpacing(16)
        lay.addWidget(self.ticker_label)
        lay.addSpacing(16)
        self.scroll_area.setWidget(self.ticker_container)

        # Fades
        self.left_fade = GradientFade(direction="left", parent=self.ticker_container)
        self.right_fade = GradientFade(direction="right", parent=self.ticker_container)
        self.scroll_area.viewport().installEventFilter(self)

        # Status
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color:#a2a9b6;font-size:12px;")

        root.addLayout(controls)
        root.addWidget(self.scroll_area, 1)
        root.addWidget(self.status)

    def eventFilter(self, obj, ev):
        if obj == self.scroll_area.viewport() and ev.type() == QtCore.QEvent.Type.Resize:
            self._position_fades()
        return super().eventFilter(obj, ev)

    def _position_fades(self):
        vp = self.scroll_area.viewport()
        h = vp.height()
        self.left_fade.setGeometry(0, 0, 28, h)
        self.right_fade.setGeometry(vp.width() - 28, 0, 28, h)
        self.left_fade.raise_()
        self.right_fade.raise_()

    # ---------------- BaseModule ----------------
    def on_enable(self):
        self._running = True

    def on_disable(self):
        self._running = False

    def on_data(self, data_by_symbol: Dict[str, List[dict]], tickers: List[str]):
        # Keeping this hook in case you later want to limit by current chart symbols.
        pass

    # ---------------- Mechanics ----------------
    def _on_hover(self, entering: bool):
        def _fn():
            self._hover_pause = entering
        return _fn

    def _tick_scroll(self):
        if not self._running or self._hover_pause:
            return
        px = self.speed_slider.value()
        self._x_offset -= px

        view_w = self.scroll_area.viewport().width()
        text_w = self.ticker_label.sizeHint().width()

        if -self._x_offset > text_w + 96:
            self._x_offset = view_w

        self.ticker_label.move(self._x_offset, 0)
        self.ticker_container.setMinimumHeight(self.ticker_label.sizeHint().height() + 12)

    def _reset_refresh_interval(self):
        self._refresh_timer.stop()
        self._refresh_timer.start(self.refresh_every_sb.value() * 1000)

    # ---------------- Fetching ----------------
    def refresh_now(self):
        if not self._running:
            return
        self._save_settings()
        t = threading.Thread(target=self._refresh_worker, daemon=True)
        t.start()

    def _refresh_worker(self):
        try:
            feeds = [ln.strip() for ln in self.feeds_edit.toPlainText().splitlines() if ln.strip()]
            if not feeds:
                self.statusSig.emit("No feeds configured.")
                return

            articles = []
            for url in feeds:
                try:
                    items = self._fetch_feed(url)
                    articles.extend(items)
                except Exception as e:
                    self.statusSig.emit(f"Feed error: {url} ({e})")

            # Deduplicate by (title, link)
            seen = set()
            unique = []
            for it in articles:
                key = (it.get("title","").strip(), it.get("link","").strip())
                if key in seen:
                    continue
                seen.add(key)
                unique.append(it)

            enriched = self._attach_symbols(unique)

            # Price refresh (short cache)
            now = time.time()
            symbols = sorted({it["symbol"] for it in enriched if it.get("symbol")})
            if symbols and (now - self._price_cache_ts > 15):
                got = self._yahoo_prices(symbols)
                if got:
                    self._prices.update(got)
                    self._price_cache_ts = now

            # Merge price data into enriched
            for it in enriched:
                sym = it.get("symbol") or ""
                if sym and sym in self._prices:
                    it.update(self._prices[sym])

            # Incremental merge (no wipe)
            self.itemsSig.emit(enriched)
            self.statusSig.emit(f"Updated ({len(enriched)} headlines scanned).")
        except Exception as e:
            self.statusSig.emit(f"Refresh error: {e}")

    def _fetch_feed(self, url: str) -> List[dict]:
        out = []
        if FEEDPARSER_OK:
            d = feedparser.parse(url)
            for e in d.entries[:60]:
                title = getattr(e, "title", "").strip()
                link  = getattr(e, "link", "").strip()
                if title:
                    out.append({"title": title, "link": link})
            return out

        # Fallback: simple XML
        import xml.etree.ElementTree as ET
        resp = requests.get(url, headers={"User-Agent": YA_USER_AGENT}, timeout=12)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item"):
            t = (item.findtext("title") or "").strip()
            l = (item.findtext("link") or "").strip()
            if t:
                out.append({"title": t, "link": l})
        return out

    # ---------------- Symbol detection (always on) ----------------
    _SYM_PATTERNS = [
        re.compile(r"\$([A-Z]{1,5})(?![A-Za-z])"),                # $TSLA
        re.compile(r"\(([A-Z]{1,5})\)"),                          # (AAPL)
        re.compile(r"\b(?:NASDAQ|NYSE|AMEX|LSE|TSX)[:\s\-]+([A-Z]{1,5})\b"),
        re.compile(r"\b([A-Z]{1,5})\b"),                          # fallback caps
    ]
    _STOPWORDS = {
        "THE","AND","FOR","WITH","FROM","THIS","WALL","STREET","CNBC","MARKET","NEWS",
        "FED","ECB","BOE","OPEC","GDP","CPI","PPI","EPS","ETF","IPO","AI","USA","US",
        "MORE","LIVE","DAILY","TODAY","BREAKING","UPDATE","UPDATES","TOP","OF","IN"
    }

    def _guess_symbol(self, title: str) -> Optional[str]:
        for pat in self._SYM_PATTERNS[:3]:
            m = pat.search(title)
            if m:
                return m.group(1).upper()
        # all-caps fallback always enabled
        words = re.findall(self._SYM_PATTERNS[3], title)
        words = [w for w in words if w.upper() not in self._STOPWORDS]
        return words[0].upper() if words else None

    def _attach_symbols(self, items: List[dict]) -> List[dict]:
        out = []
        for it in items:
            title = it.get("title","")
            sym = self._guess_symbol(title)
            n = dict(it)
            n["symbol"] = sym
            out.append(n)
        return out

    # ---------------- Prices ----------------
    def _yahoo_prices(self, symbols: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        if not symbols:
            return out
        headers = {"User-Agent": YA_USER_AGENT, "Accept": "application/json"}
        chunk = 45
        for i in range(0, len(symbols), chunk):
            group = symbols[i:i+chunk]
            url = YA_QUOTE_URL.format(symbols=",".join(group))
            try:
                r = requests.get(url, headers=headers, timeout=12)
                r.raise_for_status()
                data = r.json()
                results = (data.get("quoteResponse", {}) or {}).get("result", []) or []
                for q in results:
                    sym = (q.get("symbol") or "").upper()
                    price = q.get("regularMarketPrice")
                    opn   = q.get("regularMarketOpen")
                    chg   = q.get("regularMarketChange")
                    chgPct= q.get("regularMarketChangePercent")
                    curr  = q.get("currency") or ""
                    if not sym or price is None or opn is None:
                        continue
                    if chg is None:
                        chg = float(price) - float(opn)
                    if chgPct is None and opn:
                        chgPct = (float(price)-float(opn)) / float(opn) * 100.0
                    up = float(chg) >= 0
                    out[sym] = {
                        "price": float(price),
                        "open": float(opn),
                        "up": bool(up),
                        "chg": float(chg),
                        "chgPct": float(chgPct if chgPct is not None else 0.0),
                        "currency": curr
                    }
            except Exception:
                pass
        return out

    # ---------------- UI merge (no wipe) ----------------
    @QtCore.pyqtSlot(list)
    def _merge_items_ui(self, fresh: List[dict]):
        if not fresh:
            return

        new_segments: List[str] = []
        for it in fresh:
            title = it.get("title","").strip()
            link  = it.get("link","").strip()
            sym   = (it.get("symbol") or "").strip()
            key = (title, link, sym)
            if key in self._seen_keys:
                continue

            self._seen_keys.add(key)
            self._items.append(it)
            seg = self._segment_html(it)
            if seg:
                new_segments.append(seg)

        if not new_segments:
            return

        current_html = self.ticker_label.text().strip()
        add_html = self._sep_html.join(new_segments)
        new = (current_html + self._sep_html + add_html) if current_html else add_html

        # Preserve marquee offset
        self.ticker_label.setText(new)
        self._position_fades()

    def _segment_html(self, it: dict) -> str:
        title = it.get("title","").strip()
        link  = it.get("link","").strip()
        sym   = (it.get("symbol") or "").strip()
        pinfo = self._prices.get(sym) if sym else None

        sym_html = f"<span style='color:#86c5ff;font-weight:600'>[{sym}]</span> " if sym else ""
        title_esc = self._escape(title)
        title_html = (
            f"<a href='{self._escape_attr(link)}' style='color:#f5f7fa;text-decoration:none'>{title_esc}</a>"
            if link else f"<span style='color:#f5f7fa'>{title_esc}</span>"
        )

        price_html = ""
        if pinfo:
            up    = pinfo.get("up")
            chgPct= pinfo.get("chgPct", 0.0)
            price = pinfo.get("price")
            arrow = "▲" if up else "▼"
            color = "#33d17a" if up else "#ff6b6b"
            price_html = f"<span style='color:{color};font-weight:600'> {price:,.2f} · {arrow} {chgPct:+.2f}%</span>"

        tooltip = self._escape_attr(
            f"{title}\n" + (f"Symbol: {sym}\n" if sym else "") +
            (f"Price: {pinfo.get('price'):,.2f} ({pinfo.get('chgPct',0.0):+.2f}%)\n" if pinfo else "")
        )
        return f"<span title='{tooltip}'>{sym_html}{title_html}{price_html}</span>"

    @QtCore.pyqtSlot(str)
    def _set_status(self, msg: str):
        self.status.setText(msg)

    # ---------------- Settings ----------------
    def _load_settings(self):
        s = QtCore.QSettings(SETTINGS_SCOPE, SETTINGS_SCOPE)
        feeds = s.value(SET_FEEDS, "", type=str)
        if feeds:
            self.feeds_edit.setPlainText(feeds)
        # Speed defaults to LOWEST always (1) unless user previously set something
        self.speed_slider.setValue(s.value(SET_SPEED, 1, type=int))
        self.refresh_every_sb.setValue(s.value(SET_REFRESH_SEC, self.refresh_every_sb.value(), type=int))

    def _save_settings(self):
        s = QtCore.QSettings(SETTINGS_SCOPE, SETTINGS_SCOPE)
        s.setValue(SET_FEEDS, self.feeds_edit.toPlainText())
        s.setValue(SET_SPEED, self.speed_slider.value())
        s.setValue(SET_REFRESH_SEC, self.refresh_every_sb.value())

    # ---------------- Utils ----------------
    @staticmethod
    def _escape(s: str) -> str:
        return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    @staticmethod
    def _escape_attr(s: str) -> str:
        return (s.replace("&","&amp;")
                 .replace("<","&lt;")
                 .replace(">","&gt;")
                 .replace('"',"&quot;")
                 .replace("'","&#39;"))


# ---- Helpers ----
class HoverPauseWidget(QtWidgets.QWidget):
    entered = QtCore.pyqtSignal()
    exited  = QtCore.pyqtSignal()
    def enterEvent(self, e: QtGui.QEnterEvent):
        self.entered.emit()
        super().enterEvent(e)
    def leaveEvent(self, e: QtGui.QEnterEvent):
        self.exited.emit()
        super().leaveEvent(e)

class GradientFade(QtWidgets.QWidget):
    def __init__(self, direction: str, parent=None):
        super().__init__(parent)
        self._dir = direction
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    def paintEvent(self, ev: QtGui.QPaintEvent):
        p = QtGui.QPainter(self)
        grad = QtGui.QLinearGradient()
        if self._dir == "left":
            grad.setStart(self.width(), 0); grad.setFinalStop(0, 0)
        else:
            grad.setStart(0, 0); grad.setFinalStop(self.width(), 0)
        start = QtGui.QColor(15,17,21, 0)
        end   = QtGui.QColor(15,17,21, 255)
        grad.setColorAt(0.0, start); grad.setColorAt(1.0, end)
        p.fillRect(self.rect(), QtGui.QBrush(grad))
        p.end()
