#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "lavender-panic-symbolize.py"
spec = importlib.util.spec_from_file_location("lavender_panic_symbolize", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class SymbolizerTests(unittest.TestCase):
    def test_nearest_symbol_and_slide(self):
        symbols = [(0x1000, "_start"), (0x1100, "_panic")]
        self.assertEqual(mod.resolve(0x1124, symbols), "_panic+0x24")
        self.assertEqual(mod.resolve(0x2124, symbols, 0x1000), "_panic+0x24")
        self.assertEqual(mod.resolve(0x0FFF, symbols), "?")

    def test_extracts_panic_fields(self):
        text = "boot\npanic(cpu 0 caller 0xfffffff007001000): test\npc: 0xfffffff007001020\nlr: 0xfffffff007001000\nfar: 0x0\nesr: 0x96000004\n"
        marker, section = mod.panic_section(text)
        self.assertEqual(marker, "panic(cpu")
        parsed = mod.fields(section)
        self.assertEqual(parsed["pc"], 0xFFFFFFF007001020)
        self.assertEqual(parsed["lr"], 0xFFFFFFF007001000)
        self.assertEqual(parsed["far"], 0)
        self.assertIn("data abort", mod.decode_esr(parsed["esr"]))

    def test_falls_back_to_lavender_marker(self):
        marker, section = mod.panic_section("boot\n[PANIC] test\nTCR=0x1\n")
        self.assertEqual(marker, "[PANIC]")
        self.assertIn("TCR=0x1", section)

if __name__ == "__main__":
    unittest.main()
