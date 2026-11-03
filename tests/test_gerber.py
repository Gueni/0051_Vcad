import math
import tempfile
import unittest
import zipfile
from pathlib import Path

from vcad.gerber import DrillParser, GerberParser, evaluate, layer_identity, render_gerbers


def gerber(body, header="%FSLAX24Y24*%\n%MOIN*%\n"):
    return GerberParser(header + body + "\nM02*\n").parse()


class GerberParserTest(unittest.TestCase):
    def test_inch_flash_and_draw(self):
        parser = gerber("%ADD10C,0.1*%\nD10*\nX0Y0D03*\nX10000Y0D02*\nX20000Y0D01*")
        geometry = parser.layer.geometry()
        self.assertEqual(parser.stats, {"flashes": 1, "draws": 1, "regions": 0})
        # 0.1 in circle flash at the origin and a 1 in long, 0.1 in wide draw.
        self.assertAlmostEqual(geometry.bounds[0], -1.27, places=2)
        self.assertAlmostEqual(geometry.bounds[2], 2 * 25.4 + 1.27, places=2)
        self.assertAlmostEqual(geometry.bounds[3], 1.27, places=2)

    def test_trailing_zero_format(self):
        parser = gerber("%ADD10R,1X1*%\nD10*\nX15Y25D03*", header="%FSTAX33Y33*%\n%MOMM*%\n")
        x0, y0, x1, y1 = parser.layer.geometry().bounds
        self.assertAlmostEqual((x0 + x1) / 2, 150.0)  # "15" padded to 150000
        self.assertAlmostEqual((y0 + y1) / 2, 250.0)

    def test_clear_polarity_subtracts(self):
        parser = gerber("%ADD10R,10X10*%\n%ADD11C,2*%\nD10*\nX0Y0D03*\n%LPC*%\nD11*\nX0Y0D03*\n%LPD*%",
                        header="%FSLAX33Y33*%\n%MOMM*%\n")
        self.assertAlmostEqual(parser.layer.geometry().area, 100 - math.pi, places=1)

    def test_region_with_arc_multi_quadrant(self):
        # A 10 mm disc drawn as one full-circle G75 arc inside a region.
        parser = gerber("G75*\nG36*\nX5000Y0D02*\nG03*\nX5000Y0I-5000J0D01*\nG01*\nG37*",
                        header="%FSLAX33Y33*%\n%MOMM*%\n")
        self.assertEqual(parser.stats["regions"], 1)
        self.assertAlmostEqual(parser.layer.geometry().area, math.pi * 25, delta=0.2)

    def test_single_quadrant_arc(self):
        parser = gerber("%ADD10C,0.01*%\nD10*\nG74*\nX1000Y0D02*\nG03*\nX0Y1000I1000J0D01*",
                        header="%FSLAX33Y33*%\n%MOMM*%\n")
        line = parser.layer.lines[0]
        self.assertTrue(all(abs(math.hypot(x, y) - 1) < 1e-6 for x, y in line.coords))

    def test_aperture_macro(self):
        macro = "%AMDONUT*1,1,$1,0,0*1,0,$1/2,0,0*%\n"
        parser = gerber(macro + "%ADD10DONUT,4*%\nD10*\nX0Y0D03*", header="%FSLAX33Y33*%\n%MOMM*%\n")
        # Circles are 32-gons: 0.64% under the true area.
        self.assertAlmostEqual(parser.layer.geometry().area, math.pi * (4 - 1), delta=0.1)

    def test_macro_expressions(self):
        self.assertEqual(evaluate("$1x2+$2/4-1", {1: 3.0, 2: 8.0}), 7.0)
        with self.assertRaises(ValueError):
            evaluate("__import__('os')", {})

    def test_step_and_repeat(self):
        parser = gerber("%SRX3Y2I10J5*%\n%ADD10R,1X1*%\nD10*\nX0Y0D03*\n%SR*%", header="%FSLAX33Y33*%\n%MOMM*%\n")
        geometry = parser.layer.geometry()
        self.assertAlmostEqual(geometry.area, 6.0)
        self.assertAlmostEqual(geometry.bounds[2], 20.5)
        self.assertAlmostEqual(geometry.bounds[3], 5.5)


class DrillParserTest(unittest.TestCase):
    def test_inch_trailing_zero_holes_and_slot(self):
        text = "M48\nINCH,TZ\nT1C0.0394\n%\nT1\nX01Y01\nX02Y01\nX03Y01G85X04Y01\nM30\n"
        parser = DrillParser(text).parse()
        self.assertEqual(len(parser.holes), 2)
        self.assertAlmostEqual(parser.holes[0][0], 25.4)  # "01" padded to 2.4 format: 1.0000 in
        self.assertEqual(len(parser.routes), 1)

    def test_routing(self):
        text = "M48\nMETRIC,LZ\nT1C2.0\n%\nT1\nG00X000000Y000000\nM15\nG01X010000Y000000\nY010000\nM16\nM30\n"
        parser = DrillParser(text).parse()
        self.assertEqual(len(parser.routes), 1)
        self.assertEqual(list(parser.routes[0][0].coords), [(0, 0), (10, 0), (10, 10)])


class LayerIdentityTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(layer_identity("board.GTL", ""), "TOP")
        self.assertEqual(layer_identity("board.G2", ""), "MID2")
        self.assertEqual(layer_identity("board-B_Silkscreen.gbr", ""), "BOTTOMOVERLAY")
        self.assertEqual(layer_identity("board-In1_Cu.gbr", ""), "MID1")
        self.assertEqual(layer_identity("x.gbr", "%TF.FileFunction,Soldermask,Bot*%"), "BOTTOMSOLDER")
        self.assertEqual(layer_identity("x.gbr", "%TF.FileFunction,Copper,L2,Inr*%"), "MID1")
        self.assertIsNone(layer_identity("notes.gbr", ""))


class RenderTest(unittest.TestCase):
    def test_zip_set_renders_layers_outline_and_drills(self):
        header = "%FSLAX33Y33*%\n%MOMM*%\n"
        outline = header + "%ADD10C,0.25*%\nD10*\nX0Y0D02*\nX50000Y0D01*\nX50000Y30000D01*\nX0Y30000D01*\nX0Y0D01*\nM02*\n"
        copper = header + "%ADD11R,2X2*%\nD11*\nX10000Y10000D03*\nM02*\n"
        drill = "M48\nMETRIC,LZ\nT1C1.0\n%\nT1\nX010000Y010000\nM30\n"
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "board.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("board.GKO", outline)
                handle.writestr("board.GTL", copper)
                handle.writestr("board.TXT", drill)
                handle.writestr("readme.txt", "not gerber")
            result = render_gerbers(archive)
        self.assertEqual(result["kind"], "pcb")
        self.assertEqual(result["name"], "board")
        self.assertEqual(result["copper_layer_order"], ["TOP"])
        for token in ("TOP", "BOARD_OUTLINE", "DRILLS"):
            self.assertIn(f'data-layer-token="{token}"', result["svg"])
        self.assertEqual(len(result["scene3d"]["board"]), 1)
        self.assertEqual(len(result["scene3d"]["board"][0]["holes"]), 1)
        self.assertIn("Skipped 1 file that are not Gerber or drill data.", result["warnings"])


if __name__ == "__main__":
    unittest.main()
