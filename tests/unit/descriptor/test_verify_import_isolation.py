"""Assert that verify.py has zero imports from mthydra.controller (spec B §7 B-D6)."""
import ast
import pathlib


def test_verify_has_no_controller_imports():
    src_path = (
        pathlib.Path(__file__).parent.parent.parent.parent
        / "src/mthydra/descriptor/verify.py"
    )
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "controller" not in alias.name, (
                    f"verify.py must not import from mthydra.controller, "
                    f"found: import {alias.name!r}"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "controller" not in module, (
                f"verify.py must not import from mthydra.controller, "
                f"found: from {module!r} import ..."
            )
