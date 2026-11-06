"""ObservabilityService.log takes (event_type, message, level=...).

Calls that passed a level word first put the message text in ``level`` and
the level in ``eventType``, so run logs could not be filtered by level.
This scans the service's code for that shape so it cannot come back.
"""

import ast
from pathlib import Path

LEVELS = {"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"}
ROOT = Path(__file__).resolve().parents[2]


def _offending_calls():
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {"tests", ".venv", "venv"} or "site-packages" in rel.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(rel))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "log"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in LEVELS
            ):
                found.append(f"{rel}:{node.lineno}")
    return found


def test_no_log_call_passes_a_level_as_event_type():
    assert _offending_calls() == []
