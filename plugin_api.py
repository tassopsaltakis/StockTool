from PyQt6 import QtWidgets
from typing import Dict, List

class BaseModule(QtWidgets.QWidget):
    """
    Modules must subclass this and define:
      MODULE_ID (str), MODULE_NAME (str), MODULE_DESC (str)

    Optional hooks:
      on_enable(self)
      on_disable(self)
      on_data(self, data_by_symbol: Dict[str, List[dict]], tickers: List[str])
        - data_by_symbol: {"AAPL": [{"date": "...", "open": ..., "close": ...}, ...], ...}
        - tickers: the ordered list of tickers the user requested (may be subset if some failed)
    """
    MODULE_ID = "base"
    MODULE_NAME = "Base Module"
    MODULE_DESC = "Base module"

    def on_enable(self): pass
    def on_disable(self): pass
    def on_data(self, data_by_symbol, tickers): pass
