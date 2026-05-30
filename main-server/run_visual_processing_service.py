#!/usr/bin/env python3
"""Run the configured visual processing service provider."""

from __future__ import annotations

from config import VISUAL_PROCESSING_SERVICE_CLASS, VISUAL_PROCESSING_SERVICE_CONFIG
from service_loader import build_service


def main() -> None:
    service = build_service(VISUAL_PROCESSING_SERVICE_CLASS, VISUAL_PROCESSING_SERVICE_CONFIG)

    if hasattr(service, "serve_forever"):
        service.serve_forever()
        return
    if hasattr(service, "run"):
        service.run()
        return

    raise AttributeError(
        "Configured visual processing service must expose either 'serve_forever()' or 'run()'"
    )


if __name__ == "__main__":
    main()
