# StockTool

<img src="assets/stocktool.png" alt="StockTool Icon" width="472" height="472">

StockTool is a modular, extensible desktop application built with Python and PyQt6 that allows users to fetch, visualize, and analyze historical stock data from Yahoo Finance. It provides an interactive chart, data table, and plugin module system for additional functionality.

## Features

- Multi-ticker support: Compare multiple tickers (e.g., AAPL, MSFT, NVDA) at once.
- Adjustable history range: Pull any number of days (e.g., 7, 365, or 5000).
- Interactive graphing: Hardware-accelerated charts with zoom, pan, and hover.
- Modular architecture: Add or remove user-created modules in the 'modules' directory.
- Persistent settings: Module states are saved in settings.json.
- Cross-platform compatibility COMING SOON.
  - Windows is tested and working, Mac is in Testing, Linux has not been tested yet.
- Custom icon: Located in assets/stocktool.ico and used for the application window and executables.

## Directory Structure

```
StockTool/
├── assets/
│   └── stocktool.ico
├── modules/
│   ├── __init__.py
│   └── winnerloser.py
├── plugin_api.py
├── stocktool.py
├── settings.json
└── README.md
```

## Requirements

- Python 3.10+
- Required libraries:
  ```bash
  pip install PyQt6 pyqtgraph requests
  ```

## Running from Source

```bash
python stocktool.py
```

## Building Executables

### Windows

```powershell
pyinstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onefile `
  --name StockTool `
  --icon assets\stocktool.ico `
  --add-data "assets;assets" `
  --add-data "modules;modules" `
  --collect-data pyqtgraph `
  --collect-submodules pyqtgraph `
  --distpath "./build/StockTool" `
  stocktool.py
```


## License

This project is licensed under the **Business Source License 1.1 (BSL 1.1)**.  
The licensor of this software is **Anastasios Psaltakis**.

Use of this software is permitted under the terms of the BSL 1.1.  
You may use, modify, and distribute this software for non-commercial purposes.  
Commercial use, production deployment, or redistribution as part of a commercial product requires explicit written permission from the licensor.

For the full license text, see the included `LICENSE` file or visit:  
[https://mariadb.com/bsl11/](https://mariadb.com/bsl11/)


## Author

Anastasios Psaltakis 
Version 1.0
