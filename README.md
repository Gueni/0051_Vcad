# cadview

Local browser-based viewer for Altium and KiCad PCB and schematic files.
Documents and rendering stay on your computer; the server listens only on
`127.0.0.1`.

Open `.step` and `.stp` files directly for standalone 3D viewing, or add STEP
files as external models to a PCB's 3D view. Standalone models use the local
OpenCascade WebAssembly importer and are limited to 32 MB and 250,000 triangles.
Native SolidWorks files and 3D PDF files are not currently supported; export
SolidWorks geometry to STEP to view it in cadview.

## Requirements

- Python 3.12 or newer
- A modern browser with WebGL 2 for accelerated PCB rendering

## Install and run

From the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\cadview.exe
```

Open documents directly from the command line:

```powershell
.\.venv\Scripts\cadview.exe board.PcbDoc sheet.SchDoc
.\.venv\Scripts\cadview.exe board.kicad_pcb sheet.kicad_sch
```

Use `--no-open` to start the local server without opening a browser, or
`--browser default` to use the system browser.

## Tests

The Python test suite can be run with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Browser tests, when present in the checkout, require Node.js 20 or newer and
Playwright. Node.js is not required to run cadview.
