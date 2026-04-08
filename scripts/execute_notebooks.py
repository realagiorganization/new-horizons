#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


def notebook_root() -> Path:
    return Path(__file__).resolve().parent.parent / "notebooks"


def execute_notebook(path: Path) -> None:
    namespace = {"__name__": "__main__"}
    payload = json.loads(path.read_text())
    previous_cwd = Path.cwd()
    os.chdir(path.parent)
    try:
        for idx, cell in enumerate(payload.get("cells", [])):
            if cell.get("cell_type") != "code":
                continue
            source = "".join(cell.get("source", []))
            code = compile(source, f"{path.name}::cell{idx}", "exec")
            exec(code, namespace, namespace)
    finally:
        os.chdir(previous_cwd)


def main() -> int:
    failures = []
    for path in sorted(notebook_root().glob("*.ipynb")):
        print(f"== Executing {path.name}")
        try:
            execute_notebook(path)
        except Exception as exc:  # pragma: no cover - CLI surface
            failures.append((path.name, str(exc)))
            print(f"FAILED: {path.name}: {exc}", file=sys.stderr)
        else:
            print(f"OK: {path.name}\n")

    if failures:
        print("Notebook execution failed:", file=sys.stderr)
        for name, error in failures:
            print(f"- {name}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
