from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
analysis = Analysis(
    [str(root / "cadview.py")], pathex=[str(root)],
    datas=collect_data_files("cadview") + collect_data_files("altium_monkey"),
    hiddenimports=collect_submodules("altium_monkey"),
    excludes=["PySide6.QtWidgets", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
              "PySide6.QtTest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick"],
)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True, name="cadview", console=False)
collection = COLLECT(executable, analysis.binaries, analysis.datas, name="cadview")
if sys.platform == "darwin":
    app = BUNDLE(collection, name="cadview.app", bundle_identifier="org.cadview.desktop",
        info_plist={"CFBundleDisplayName": "cadview"})
