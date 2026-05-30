#!/usr/bin/env python3
"""Run the configured main website/API server provider."""

from __future__ import annotations

from config import MAIN_SERVER_SERVICE_CLASS, MAIN_SERVER_SERVICE_CONFIG
from check_config import validate_current_config
from service_loader import build_service


def main() -> None:
    ok, errors = validate_current_config()
    if not ok:
        raise SystemExit(
            "Configuration validation failed:\n"
            + "\n".join(f"- {err}" for err in errors)
        )

    service = build_service(MAIN_SERVER_SERVICE_CLASS, MAIN_SERVER_SERVICE_CONFIG)

    if hasattr(service, "serve_forever"):
        service.serve_forever()
        return
    if hasattr(service, "run"):
        service.run()
        return

    raise AttributeError("Configured main server must expose either 'serve_forever()' or 'run()'")


if __name__ == "__main__":
    main()
