"""Copy the pinned, independently replaceable STEP runtime after npm ci."""

import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
package = ROOT / "node_modules/occt-import-js"
expected = json.loads((ROOT / "package.json").read_text())["devDependencies"]["occt-import-js"]
actual = json.loads((package / "package.json").read_text())["version"]
if actual != expected:
    raise SystemExit(f"Expected occt-import-js {expected}, got {actual}; run npm ci")
for source, target in (
    ("occt-import-js.js", "occt-import-js.js"),
    ("occt-import-js.wasm", "occt-import-js.wasm"),
    ("license.occt-import-js.txt", "OCCT-IMPORT-LICENSE.txt"),
    ("license.occt.txt", "OCCT-LICENSE.txt"),
):
    shutil.copyfile(package / "dist" / source, ROOT / "cadview/static" / target)
print(f"Bundled occt-import-js {actual}")
