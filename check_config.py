#!/usr/bin/env python3
"""Project-level wrapper for strict config validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def main() -> int:
    path = Path(__file__).resolve().parent / "main-server" / "check_config.py"
    spec = importlib.util.spec_from_file_location("magen_check_config", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return int(mod.main())


if __name__ == "__main__":
    raise SystemExit(main())
