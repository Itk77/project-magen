#!/usr/bin/env python3
"""Run the configured LLM service provider."""

from __future__ import annotations

from config import LLM_SERVICE_CLASS, LLM_SERVICE_CONFIG
from service_loader import build_service


def main() -> None:
    service = build_service(LLM_SERVICE_CLASS, LLM_SERVICE_CONFIG)

    if hasattr(service, "serve_forever"):
        service.serve_forever()
        return
    if hasattr(service, "run"):
        service.run()
        return

    raise AttributeError("Configured LLM service must expose either 'serve_forever()' or 'run()'")


if __name__ == "__main__":
    main()
