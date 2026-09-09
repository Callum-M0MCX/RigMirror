import ast
from pathlib import Path
import unittest


SOURCE_PATH = Path(__file__).parents[1] / "rigmirror.py"


class DisplaySchemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_seven_complete_display_schemes_are_defined(self):
        assignment = next(
            node for node in self.tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "DISPLAY_SCHEMES"
                    for target in node.targets)
        )
        schemes = ast.literal_eval(assignment.value)
        self.assertEqual(
            tuple(schemes),
            ("green_black", "cyan_black", "amber_black",
             "green_backlit", "amber_backlit", "cyan_backlit", "white_black"),
        )
        for scheme in schemes.values():
            self.assertEqual(set(scheme), {"master", "listener"})
            for foreground, background in scheme.values():
                self.assertRegex(foreground, r"^#[0-9a-fA-F]{6}$")
                self.assertRegex(background, r"^#[0-9a-fA-F]{6}$")

    def test_amber_master_matches_memory_button_and_white_black_exists(self):
        namespace = {}
        prefix = self.source.split("# These are the Kenwood antenna-memory", 1)[0]
        exec(compile(prefix, str(SOURCE_PATH), "exec"), namespace)
        self.assertEqual(namespace["DISPLAY_SCHEMES"]["amber_backlit"]["master"][1], namespace["AMBER"])
        self.assertEqual(namespace["DISPLAY_SCHEME_LABELS"]["White on black"], "white_black")

    def test_right_click_cycle_is_saved_and_applied_to_both_skins(self):
        methods = {
            node.name: ast.get_source_segment(self.source, node) or ""
            for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef)
        }
        cycle = methods["_cycle_display_scheme"]
        apply_scheme = methods["_apply_display_scheme"]
        self.assertIn("_apply_display_scheme", cycle)
        self.assertIn("_save_config", cycle)
        self.assertIn('self.cards[0]["frequency"]', apply_scheme)
        self.assertIn('self.cards[1]["frequency"]', apply_scheme)
        self.assertIn("self.micro_frequencies[0]", apply_scheme)
        self.assertIn("self.micro_frequencies[1]", apply_scheme)


if __name__ == "__main__":
    unittest.main()
