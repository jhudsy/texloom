#!/usr/bin/env python3
"""Every GitHub Actions workflow and action file is valid YAML with the shape Actions needs.

A workflow file that doesn't parse is not reported as a failing test: its workflow just doesn't run, so
the checks it holds quietly stop (an unquoted step name with ": " in it did that once: the quick
Checks stopped running for three pushes). This runs in its own workflow (workflows.yml) so that it
still runs when checks.yml is the one that is broken.

    uv run --with pyyaml python scripts/check_workflows.py
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import ROOT, check, summary  # noqa: E402


def main() -> int:
    files = sorted((ROOT / ".github" / "workflows").glob("*.yml")) + sorted((ROOT / ".github" / "actions").glob("*/action.yml"))
    for f in files:
        rel = f.relative_to(ROOT)
        try:
            doc = yaml.safe_load(f.read_text())
        except yaml.YAMLError as e:
            check(f"{rel} is valid YAML", False, str(e).splitlines()[-1])
            continue
        if "workflows" in f.parts:
            # (YAML reads a bare `on:` as the boolean True)
            ok = isinstance(doc, dict) and "jobs" in doc and ("on" in doc or True in doc)
            check(f"{rel} parses, with `on` and `jobs`", ok)
            names = [str(s.get("name", "")) for j in (doc.get("jobs") or {}).values() for s in (j.get("steps") or []) if isinstance(s, dict)]
            check(f"{rel}: every step name is a string", all(isinstance(n, str) for n in names))
        else:
            check(f"{rel} parses, with `runs`", isinstance(doc, dict) and "runs" in doc)
    return summary("workflow files")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
