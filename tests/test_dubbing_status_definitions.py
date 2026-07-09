import ast
import unittest
from pathlib import Path

from pandrator.logic.state_db_handler import DUBBING_STEPS


class DubbingStatusDefinitionsTests(unittest.TestCase):
    def test_task_status_panel_stage_definitions_match_persisted_steps(self):
        panel_step_keys = self._task_status_panel_stage_keys()

        self.assertEqual(list(DUBBING_STEPS), panel_step_keys)

    def test_source_tab_routes_zoom_vtt_mode_to_background_import(self):
        tree = self._parse_repo_module("pandrator", "gui", "widgets", "session_tab.py")
        session_tab = self._find_class(tree, "SessionTab")
        on_select_file = self._find_method(session_tab, "_on_select_file")
        start_zoom_import = self._find_method(session_tab, "_start_zoom_vtt_source_import")

        self.assertTrue(self._contains_string(on_select_file, "zoom_vtt"))
        self.assertTrue(self._contains_name(on_select_file, "_apply_zoom_vtt_source_selection"))
        self.assertTrue(self._contains_name(start_zoom_import, "import_zoom_vtt_transcript_source"))

    def test_app_logic_zoom_vtt_import_uses_native_zoom_service(self):
        tree = self._parse_repo_module("pandrator", "app_logic.py")
        app_logic = self._find_class(tree, "AppLogic")
        import_method = self._find_method(app_logic, "import_zoom_vtt_transcript_source")

        self.assertTrue(self._contains_name(import_method, "correct_zoom_vtt_file"))
        self.assertTrue(self._contains_string(import_method, "Zoom transcript ready"))

    def _task_status_panel_stage_keys(self) -> list[str]:
        tree = self._parse_repo_module("pandrator", "gui", "widgets", "session_sections.py")

        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "TaskStatusPanel":
                for statement in node.body:
                    if not isinstance(statement, ast.Assign):
                        continue
                    if any(
                        isinstance(target, ast.Name) and target.id == "DUBBING_STAGE_DEFINITIONS"
                        for target in statement.targets
                    ):
                        definitions = ast.literal_eval(statement.value)
                        return [step_key for step_key, _label in definitions]

        self.fail("TaskStatusPanel.DUBBING_STAGE_DEFINITIONS was not found")

    def _parse_repo_module(self, *parts: str) -> ast.Module:
        repo_root = Path(__file__).resolve().parents[1]
        source_path = repo_root.joinpath(*parts)
        return ast.parse(source_path.read_text(encoding="utf-8"))

    def _find_class(self, tree: ast.Module, class_name: str) -> ast.ClassDef:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return node
        self.fail(f"{class_name} was not found")

    def _find_method(self, class_node: ast.ClassDef, method_name: str) -> ast.FunctionDef:
        for node in class_node.body:
            if isinstance(node, ast.FunctionDef) and node.name == method_name:
                return node
        self.fail(f"{class_node.name}.{method_name} was not found")

    def _contains_string(self, node: ast.AST, value: str) -> bool:
        return any(isinstance(child, ast.Constant) and child.value == value for child in ast.walk(node))

    def _contains_name(self, node: ast.AST, name: str) -> bool:
        return any(
            (isinstance(child, ast.Name) and child.id == name)
            or (isinstance(child, ast.Attribute) and child.attr == name)
            for child in ast.walk(node)
        )
