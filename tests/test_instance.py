import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vcad.documents import load_documents
from vcad.instance import Instance, forward


class InstanceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        variable = "LOCALAPPDATA" if os.name == "nt" else "XDG_RUNTIME_DIR"
        self.environment = mock.patch.dict(os.environ, {variable: self.directory.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_only_one_viewer_holds_the_lock(self):
        first, second = Instance(), Instance()
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first._handle.close()  # as when the first viewer exits
        self.assertTrue(second.acquire())
        second._handle.close()

    def test_forward_without_a_viewer_fails(self):
        self.assertFalse(forward(["board.PcbDoc"], timeout=0.2))

    def test_withdraw_removes_only_own_info(self):
        instance = Instance()
        instance.publish(1234)
        self.assertTrue(instance.info_path.exists())
        instance.withdraw()
        self.assertFalse(instance.info_path.exists())


class LoadDocumentsTest(unittest.TestCase):
    def test_gerbers_from_one_folder_form_one_board(self):
        header = "%FSLAX33Y33*%\n%MOMM*%\n%ADD10R,1X1*%\nD10*\n"
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "board.GTL").write_text(header + "X0Y0D03*\nM02*\n")
            (folder / "board.GBL").write_text(header + "X1000Y0D03*\nM02*\n")
            (folder / "board.TXT").write_text("M48\nMETRIC,LZ\nT1C1.0\n%\nT1\nX000000Y000000\nM30\n")
            documents, errors = load_documents(sorted(folder.iterdir()) + [folder / "missing.PcbDoc"])
        self.assertEqual([document["name"] for document in documents], ["board"])
        self.assertIn('data-layer-token="DRILLS"', documents[0]["svg"])
        self.assertEqual(errors, ["missing.PcbDoc: file not found"])


if __name__ == "__main__":
    unittest.main()
