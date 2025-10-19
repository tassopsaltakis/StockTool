from typing import List, Dict
from PyQt6 import QtWidgets
from plugin_api import BaseModule

class WinnerLoserModule(BaseModule):
    MODULE_ID = "winnerloser"
    MODULE_NAME = "Winner / Loser Counter"
    MODULE_DESC = "Counts days that closed above/below open for each fetched asset and totals."

    def __init__(self):
        super().__init__()
        self._build_ui()

    def _build_ui(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(8)

        # Totals line
        self.totals_line = QtWidgets.QLabel("Totals — Total: 0 | Winners: 0 | Losers: 0 | Unchanged: 0")
        self.totals_line.setStyleSheet("font-weight: 600;")
        outer.addWidget(self.totals_line)

        # Per-asset table
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Ticker", "Total", "Winners", "Losers", "Unchanged"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        outer.addWidget(self.table)

    def on_data(self, data_by_symbol: Dict[str, List[dict]], tickers: List[str]):
        # Compute totals
        grand_total = grand_w = grand_l = grand_t = 0

        # Fill per-asset
        self.table.setRowCount(0)
        for sym in tickers:
            rows = data_by_symbol.get(sym, [])
            w = l = t = 0
            for r in rows:
                o, c = r.get("open"), r.get("close")
                if o is None or c is None:
                    continue
                if c > o: w += 1
                elif c < o: l += 1
                else: t += 1
            total = w + l + t
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(sym))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(total)))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(str(w)))
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(str(l)))
            self.table.setItem(row, 4, QtWidgets.QTableWidgetItem(str(t)))

            grand_total += total
            grand_w += w
            grand_l += l
            grand_t += t

        self.totals_line.setText(
            f"Totals — Total: {grand_total} | Winners: {grand_w} | Losers: {grand_l} | Unchanged: {grand_t}"
        )
