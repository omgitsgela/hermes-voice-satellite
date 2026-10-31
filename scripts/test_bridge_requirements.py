#!/usr/bin/env python3
"""Keep the Gateway install independent of the separately deployed Hermes gateway."""
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
requirements = (ROOT / "requirements.txt").read_text().splitlines()
names = {
    re.split(r"[<>=!~\[;\s]", line.strip(), maxsplit=1)[0].lower().replace("_", "-")
    for line in requirements if line.strip() and not line.lstrip().startswith("#")
}
assert "hermes-agent" not in names, (
    "The Gateway uses the Hermes HTTP API, not the hermes-agent Python distribution; "
    "install the gateway separately."
)
for path in sorted((ROOT / "satellite_gateway").glob("*.py")):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        imported = []
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported = [node.module]
        assert not any(name.split(".")[0] in {"hermes_cli", "run_agent", "agent"}
                       for name in imported), f"{path.name} imports Hermes internals"
print("Gateway requirements and implementation are independent of Hermes internals: passed")
