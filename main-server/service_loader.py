"""Utility for loading pluggable services from a file path spec."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any


def _parse_class_spec(class_spec: str) -> tuple[Path, str]:
    if ":" not in class_spec:
        raise ValueError("class_spec must use format '/path/to/file.py:ClassName'")

    file_str, class_name = class_spec.rsplit(":", 1)
    file_path = Path(file_str).expanduser().resolve()
    if not class_name:
        raise ValueError("class name must not be empty")
    return file_path, class_name


def load_class_from_path(class_spec: str) -> type[Any]:
    """Load class from spec like '/path/to/provider.py:ProviderClass'."""
    file_path, class_name = _parse_class_spec(class_spec)
    if not file_path.exists():
        raise FileNotFoundError(f"provider file not found: {file_path}")

    module_tag = hashlib.sha1(str(file_path).encode("utf-8")).hexdigest()[:12]
    module_name = f"dynamic_provider_{module_tag}"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"unable to load module from file: {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise

    try:
        loaded = getattr(module, class_name)
    except AttributeError as exc:
        raise AttributeError(f"class '{class_name}' not found in {file_path}") from exc

    if not isinstance(loaded, type):
        raise TypeError(f"'{class_name}' in {file_path} is not a class")
    return loaded


def build_service(class_spec: str, init_kwargs: dict[str, Any]) -> Any:
    service_cls = load_class_from_path(class_spec)
    return service_cls(**init_kwargs)
