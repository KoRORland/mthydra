import ast
import pathlib


def test_ru_agent_has_zero_controller_imports():
    """Spec E-D1 contract: mthydra.ru_agent.* must run on the RU box where
    mthydra.controller is not present. AST-walk every .py file in the
    ru_agent package and assert no `from mthydra.controller` or
    `import mthydra.controller`.
    """
    root = pathlib.Path("src/mthydra/ru_agent")
    bad: list[str] = []
    for py in root.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.startswith("mthydra.controller"):
                    bad.append(f"{py}:{node.lineno}: from {mod}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("mthydra.controller"):
                        bad.append(f"{py}:{node.lineno}: import {alias.name}")
    assert not bad, (
        "ru_agent must not import from mthydra.controller.*:\n  "
        + "\n  ".join(bad)
    )
