# Vcad

Local browser-based viewer for Altium and KiCad PCB and schematic files, Gerber
and Excellon drill sets, PLECS circuit models, and STEP and SolidWorks models. Documents and rendering
stay on your computer; the server listens only on `127.0.0.1`.

Open `.step` and `.stp` files directly for standalone 3D viewing, or add STEP
files as external models to a PCB's 3D view. Standalone models use the local
OpenCascade WebAssembly importer and are limited to 32 MB and 250,000 triangles.

SolidWorks parts and assemblies (`.sldprt`, `.sldasm`) open from the display
meshes SolidWorks saves in the file, decoded by
[sldkit](https://github.com/monozukuri-ai/sldkit); SolidWorks does not need to
be installed and Vcad never starts other software. Files saved without
display data, or in layouts sldkit does not recognise, cannot be opened; export
those as STEP. sldkit is licensed PolyForm-Noncommercial-1.0.0, so this
dependency limits Vcad to noncommercial use.

Gerber (RS-274X/X2) and Excellon drill files open as one layered board: select
the whole set together (report and other non-Gerber files in the folder are
skipped), open a `.zip` of it, or pass a folder on the command line. Layers are
identified from X2 file attributes or common Altium, KiCad and Eagle file
names. When a closed outline (profile or keep-out layer) is present, the 3D view
shows the board with drills at a nominal 1.6 mm thickness.

PLECS models (`.plecs`) open as schematics, with each subsystem and
configuration drawn as its own panel. PLECS files do not contain library
symbols, so common blocks use simplified symbols and other components are drawn
as labelled boxes.

## Requirements

- Python 3.12 or newer
- A modern browser with WebGL 2 for accelerated PCB rendering

## Install and run

From the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\vcad.exe
```

Open documents directly from the command line:

```powershell
.\.venv\Scripts\vcad.exe board.PcbDoc sheet.SchDoc
.\.venv\Scripts\vcad.exe board.kicad_pcb sheet.kicad_sch
.\.venv\Scripts\vcad.exe "Project Outputs\Gerber"
```

Use `--no-open` to start the local server without opening a browser, or
`--browser default` to use the system browser.

Only one viewer runs per user: launching Vcad again (for example from
Explorer) hands the documents to the open viewer, which shows them as new tabs.
Gerber files from one folder that arrive together become one board. Use
`--new-instance` to start a separate viewer.

## Windows app

Build the installer (needs [Inno Setup 6](https://jrsoftware.org/isinfo.php),
e.g. `winget install JRSoftware.InnoSetup`):

```powershell
.\.venv\Scripts\python.exe -m pip install "pyinstaller>=6,<7"
.\.venv\Scripts\python.exe scripts\build_app.py
```

This writes to `dist\`: `vcad-setup-<version>.exe` (the installer),
`vcad-<version>-portable.zip` (unzip and run `vcad\vcad.exe`; no
right-click integration) and `SHA256SUMS.txt`. The build itself is staged in
`%TEMP%\vcad-build`. The installer needs no
administrator rights; it installs for the current user and adds a Start menu
entry. The app opens in a Chrome or Edge app window.

It adds **Open with Vcad** to the right-click menu for supported files and
for folders (Gerber output folders), and lists Vcad under **Open with**.
On Windows 11 the *Open with* submenu is in the main menu; the **Open with
Vcad** item is under *Show more options*. Default apps are not changed.
The file types are listed in `FILE_TYPES` in `scripts/build_app.py`. Uninstall
from Settings > Apps.

## Tests

The Python test suite can be run with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Browser tests, when present in the checkout, require Node.js 20 or newer and
Playwright. Node.js is not required to run Vcad.
