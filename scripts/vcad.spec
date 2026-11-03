from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

root = Path(SPECPATH).parent
datas = collect_data_files("vcad") + collect_data_files("altium_monkey")
binaries, hiddenimports = [], collect_submodules("altium_monkey") + collect_submodules("vcad")
# sldkit ships a native extension (SolidWorks display meshes).
sldkit_datas, sldkit_binaries, sldkit_imports = collect_all("sldkit")
datas += sldkit_datas; binaries += sldkit_binaries; hiddenimports += sldkit_imports

analysis = Analysis(
    [str(root / "vcad.py")], pathex=[str(root)],
    datas=datas, binaries=binaries, hiddenimports=hiddenimports,
    excludes=["PySide6.QtWidgets", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
              "PySide6.QtTest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
              "tkinter", "unittest", "pydoc"],
)
archive = PYZ(analysis.pure)
windows = sys.platform == "win32"
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True, name="vcad", console=False,
                 icon=str(root / "scripts" / "vcad.ico") if windows else None,
                 version=str(root / "build" / "version_info.txt") if windows else None)
collection = COLLECT(executable, analysis.binaries, analysis.datas, name="vcad")
if sys.platform == "darwin":
    app = BUNDLE(collection, name="vcad.app", bundle_identifier="org.vcad.desktop",
        info_plist={"CFBundleDisplayName": "Vcad"})
