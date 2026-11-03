"""Build the distribution: python scripts/build_app.py [--no-installer].

1. PyInstaller bundles Vcad (vcad.exe and its runtime) in a staging
   folder outside the workspace, where editors and indexers don't lock files.
2. dist/vcad-<version>-portable.zip: the app folder, runs without installing.
3. On Windows, Inno Setup compiles dist/vcad-setup-<version>.exe, which
   installs per user and adds "Open with Vcad" for the file types below.
"""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

from PyInstaller.__main__ import run

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DIST = ROOT / "dist"
STAGE = Path(tempfile.gettempdir()) / "vcad-build"

# Extensions offered "Open with Vcad" (and listed in Windows' "Open with" menu).
# The default app for each type is left unchanged.
FILE_TYPES = [
    "PcbDoc", "SchDoc", "kicad_pcb", "kicad_sch", "sch",
    "step", "stp", "sldprt", "sldasm", "plecs",
    "gbr", "ger", "pho", "art", "gtl", "gbl", "gto", "gbo", "gts", "gbs", "gtp", "gbp", "gko", "gml",
    "g1", "g2", "g3", "g4", "gp1", "gp2", "gm1", "gm2", "drl", "xln",
]
# Context-menu verb only: too generic to list Vcad in their "Open with" menus.
VERB_ONLY = ["zip"]


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def write_version_info(text: str) -> None:
    numbers = tuple(int(part) for part in (text.split(".") + ["0"] * 4)[:4])
    (BUILD / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'Vcad'),
    StringStruct('ProductName', 'Vcad'),
    StringStruct('FileVersion', '{text}'),
    StringStruct('ProductVersion', '{text}'),
    StringStruct('OriginalFilename', 'vcad.exe')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")


def registry_lines() -> str:
    command = '"""{app}\\vcad.exe"" ""%1"""'
    lines = []
    for extension in FILE_TYPES + VERB_ONLY:
        classes = f"Software\\Classes"
        verb = f"{classes}\\SystemFileAssociations\\.{extension}\\shell\\vcad"
        lines += [  # entries of cadview, the app's former name
            f'Root: HKA; Subkey: "{classes}\\SystemFileAssociations\\.{extension}\\shell\\cadview"; ValueType: none; '
            f'Flags: deletekey',
            f'Root: HKA; Subkey: "{classes}\\.{extension}\\OpenWithProgids"; ValueName: "cadview.document"; '
            f'ValueType: none; Flags: deletevalue',
        ]
        if extension in FILE_TYPES:
            lines += [
                f'Root: HKA; Subkey: "{classes}\\.{extension}\\OpenWithProgids"; ValueName: "vcad.document"; '
                f'ValueType: string; ValueData: ""; Flags: uninsdeletevalue',
                f'Root: HKA; Subkey: "{classes}\\Applications\\vcad.exe\\SupportedTypes"; ValueName: ".{extension}"; '
                f'ValueType: string; ValueData: ""',
            ]
        lines += [
            f'Root: HKA; Subkey: "{verb}"; ValueName: "MUIVerb"; ValueType: string; ValueData: "Open with Vcad"; '
            f'Flags: uninsdeletekey',
            f'Root: HKA; Subkey: "{verb}"; ValueName: "Icon"; ValueType: string; ValueData: "{{app}}\\vcad.exe,0"',
            f'Root: HKA; Subkey: "{verb}\\command"; ValueType: string; ValueData: {command}',
        ]
    return "\n".join(lines) + "\n"


def find_iscc():
    candidates = [shutil.which("ISCC")] + [
        os.path.expandvars(path) for path in (
            r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe",
            r"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe",
            r"%ProgramFiles%\Inno Setup 6\ISCC.exe")]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    text = version()
    write_version_info(text)
    shutil.rmtree(STAGE, ignore_errors=True)
    run([str(ROOT / "scripts" / "vcad.spec"), "--noconfirm", "--clean",
         "--distpath", str(STAGE / "dist"), "--workpath", str(STAGE / "work")])
    app = STAGE / "dist" / "vcad"
    artifacts = [Path(shutil.make_archive(str(DIST / f"vcad-{text}-portable"), "zip", app.parent, app.name))]
    if sys.platform == "win32" and "--no-installer" not in sys.argv:
        (BUILD / "version.iss").write_text(f'#define AppVersion "{text}"\n')
        (BUILD / "file_types.iss").write_text(registry_lines())
        iscc = find_iscc()
        if not iscc:
            sys.exit("Inno Setup 6 was not found; install it (winget install JRSoftware.InnoSetup) to build the installer.")
        subprocess.run([iscc, "/Q", f"/DSourceDir={app}", f"/O{DIST}", str(ROOT / "scripts" / "vcad.iss")], check=True)
        artifacts.insert(0, DIST / f"vcad-setup-{text}.exe")
    (DIST / "SHA256SUMS.txt").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in artifacts))
    for path in artifacts:
        print(f"{path}  ({path.stat().st_size / 1e6:.1f} MB)")
