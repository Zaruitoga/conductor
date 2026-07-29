"""
tests/run.py — Run every test module.

    python3 -m tests.run

Exits non-zero on the first failure, with the traceback, so it is usable as a
pre-commit check without any tooling.
"""

import importlib
import pkgutil
import sys
import traceback

import tests


def main() -> int:
    failures = 0
    for mod in sorted(pkgutil.iter_modules(tests.__path__)):
        if not mod.name.startswith("test_"):
            continue
        print(f"\n{mod.name}")
        module = importlib.import_module(f"tests.{mod.name}")
        try:
            module.main()
        except Exception:
            failures += 1
            traceback.print_exc()

    print()
    if failures:
        print(f"FAILED — {failures} module(s)")
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    sys.exit(main())
