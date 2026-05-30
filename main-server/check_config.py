#!/usr/bin/env python3
"""Strict runtime config validation for Magen services."""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent

if str(_THIS_DIR) not in sys.path:
    sys.path.append(str(_THIS_DIR))

from service_loader import load_class_from_path


def _load_config_module() -> Any:
    cfg_path = _THIS_DIR / "config.py"
    spec = importlib.util.spec_from_file_location("magen_config", cfg_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load config module from {cfg_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _validate_constructor_coverage(
    *,
    class_spec: str,
    config_dict: dict[str, Any],
    label: str,
    errors: list[str],
) -> None:
    try:
        cls = load_class_from_path(class_spec)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{label}: failed loading class '{class_spec}': {exc}")
        return

    sig = inspect.signature(cls.__init__)
    for p in sig.parameters.values():
        if p.name == "self":
            continue
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if p.name in config_dict:
            continue
        if p.default is inspect._empty:
            errors.append(f"{label}: missing required constructor arg in config: '{p.name}'")
        else:
            errors.append(
                f"{label}: config missing '{p.name}', currently relying on class default "
                "(disallowed in strict mode)"
            )


def _validate_file_exists(path_value: str, label: str, errors: list[str]) -> None:
    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        errors.append(f"{label}: file does not exist: {path}")


def _validate_dir_creatable(path_value: str, label: str, errors: list[str]) -> None:
    path = Path(path_value).expanduser().resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{label}: cannot create directory '{path}': {exc}")


def _validate_positive_int(value: Any, label: str, errors: list[str]) -> None:
    try:
        iv = int(value)
    except Exception:  # noqa: BLE001
        errors.append(f"{label}: expected int, got '{value}'")
        return
    if iv <= 0:
        errors.append(f"{label}: must be > 0, got {iv}")


def _validate_positive_float(value: Any, label: str, errors: list[str]) -> None:
    try:
        fv = float(value)
    except Exception:  # noqa: BLE001
        errors.append(f"{label}: expected float, got '{value}'")
        return
    if fv <= 0:
        errors.append(f"{label}: must be > 0, got {fv}")


def validate_current_config() -> tuple[bool, list[str]]:
    cfg = _load_config_module()
    errors: list[str] = []

    services = [
        ("camera", cfg.CAMERA_SERVICE_CLASS, cfg.CAMERA_SERVICE_CONFIG),
        ("visual_processing", cfg.VISUAL_PROCESSING_SERVICE_CLASS, cfg.VISUAL_PROCESSING_SERVICE_CONFIG),
        ("audio_io", cfg.AUDIO_IO_SERVICE_CLASS, cfg.AUDIO_IO_SERVICE_CONFIG),
        ("tts", cfg.TTS_SERVICE_CLASS, cfg.TTS_SERVICE_CONFIG),
        ("llm", cfg.LLM_SERVICE_CLASS, cfg.LLM_SERVICE_CONFIG),
        ("main_server", cfg.MAIN_SERVER_SERVICE_CLASS, cfg.MAIN_SERVER_SERVICE_CONFIG),
    ]

    for label, class_spec, conf in services:
        if not isinstance(class_spec, str) or ":" not in class_spec:
            errors.append(f"{label}: invalid class spec: {class_spec}")
            continue
        if not isinstance(conf, dict):
            errors.append(f"{label}: config is not a dict")
            continue
        _validate_constructor_coverage(
            class_spec=class_spec,
            config_dict=conf,
            label=label,
            errors=errors,
        )

    # LLM critical checks
    llm_cfg = cfg.LLM_SERVICE_CONFIG
    api_key = str(llm_cfg.get("api_key", "")).strip()
    if not api_key:
        errors.append("llm: api_key is empty (set LLM_API_KEY or GEMINI_API_KEY)")

    model_cfg_path = str(llm_cfg.get("model_config_path", "")).strip()
    if not model_cfg_path:
        errors.append("llm: model_config_path is empty")
    else:
        _validate_file_exists(model_cfg_path, "llm.model_config_path", errors)
        try:
            with Path(model_cfg_path).expanduser().resolve().open("r", encoding="utf-8") as f:
                parsed = json.load(f)
            if not isinstance(parsed, dict):
                errors.append("llm.model_config_path: JSON root must be an object")
            else:
                required_llm_model_keys = {
                    "audio_text_model",
                    "live_model",
                    "temperature",
                    "top_p",
                    "max_output_tokens",
                    "thinking_level",
                    "include_thoughts",
                    "system_instruction",
                    "assistant_instruction",
                    "default_prompt",
                    "input_audio_mime_type",
                    "response_modalities",
                    "audio_response",
                    "live",
                }
                missing = sorted([k for k in required_llm_model_keys if k not in parsed])
                if missing:
                    errors.append(
                        "llm.model_config_path: missing required keys: " + ", ".join(missing)
                    )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"llm.model_config_path: invalid JSON/config: {exc}")

    arm_password = str(llm_cfg.get("system_arm_password", "")).strip()
    if not arm_password:
        errors.append("llm: system_arm_password is empty")

    # Model file checks
    visual_cfg = cfg.VISUAL_PROCESSING_SERVICE_CONFIG
    detector = str(visual_cfg.get("detector", "")).strip().lower()
    if detector == "yolo":
        _validate_file_exists(str(visual_cfg.get("yolo_model", "")), "visual_processing.yolo_model", errors)
    # URL checks
    for name, url in (cfg.MAIN_SERVER_SERVICE_CONFIG.get("health_urls", {}) or {}).items():
        if not _is_http_url(str(url)):
            errors.append(f"main_server.health_urls[{name}]: invalid URL '{url}'")

    if not _is_http_url(str(cfg.MAIN_SERVER_SERVICE_CONFIG.get("camera_stream_url", ""))):
        errors.append("main_server.camera_stream_url is invalid")
    if not _is_http_url(str(cfg.MAIN_SERVER_SERVICE_CONFIG.get("camera_snapshot_url", ""))):
        errors.append("main_server.camera_snapshot_url is invalid")
    if not _is_http_url(str(cfg.MAIN_SERVER_SERVICE_CONFIG.get("visual_stream_url", ""))):
        errors.append("main_server.visual_stream_url is invalid")

    # Website + state dir checks
    website_root = Path(str(cfg.MAIN_SERVER_SERVICE_CONFIG.get("website_root", ""))).expanduser().resolve()
    templates_dir = website_root / "templates"
    if not templates_dir.exists():
        errors.append(f"main_server.website_root/templates missing: {templates_dir}")
    state_dir = str(cfg.MAIN_SERVER_SERVICE_CONFIG.get("state_dir", ""))
    if not state_dir:
        errors.append("main_server.state_dir is empty")
    else:
        _validate_dir_creatable(state_dir, "main_server.state_dir", errors)

    # Port and timeout checks
    _validate_positive_int(cfg.CAMERA_SERVICE_CONFIG.get("port"), "camera.port", errors)
    _validate_positive_int(cfg.VISUAL_PROCESSING_SERVICE_CONFIG.get("port"), "visual_processing.port", errors)
    _validate_positive_int(cfg.AUDIO_IO_SERVICE_CONFIG.get("port"), "audio_io.port", errors)
    _validate_positive_int(cfg.TTS_SERVICE_CONFIG.get("port"), "tts.port", errors)
    _validate_positive_int(cfg.LLM_SERVICE_CONFIG.get("port"), "llm.port", errors)
    _validate_positive_int(cfg.MAIN_SERVER_SERVICE_CONFIG.get("port"), "main_server.port", errors)
    _validate_positive_float(
        cfg.MAIN_SERVER_SERVICE_CONFIG.get("llm_request_timeout_sec"),
        "main_server.llm_request_timeout_sec",
        errors,
    )
    _validate_positive_int(
        cfg.MAIN_SERVER_SERVICE_CONFIG.get("llm_max_workers"),
        "main_server.llm_max_workers",
        errors,
    )

    # MQTT checks
    main_cfg = cfg.MAIN_SERVER_SERVICE_CONFIG
    if bool(main_cfg.get("mqtt_enabled", False)):
        mqtt_host = str(main_cfg.get("mqtt_host", "")).strip()
        if not mqtt_host:
            errors.append("main_server.mqtt_host is empty while mqtt_enabled=true")
        _validate_positive_int(main_cfg.get("mqtt_port"), "main_server.mqtt_port", errors)
        try:
            mqtt_qos = int(main_cfg.get("mqtt_qos"))
        except Exception:  # noqa: BLE001
            errors.append(f"main_server.mqtt_qos is not an integer: {main_cfg.get('mqtt_qos')}")
        else:
            if mqtt_qos not in {0, 1, 2}:
                errors.append(f"main_server.mqtt_qos must be 0, 1, or 2 (got {mqtt_qos})")

        if not str(main_cfg.get("mqtt_command_topic", "")).strip():
            errors.append("main_server.mqtt_command_topic is empty while mqtt_enabled=true")
        if not str(main_cfg.get("mqtt_sub_topics", "")).strip():
            errors.append("main_server.mqtt_sub_topics is empty while mqtt_enabled=true")
        if bool(main_cfg.get("mqtt_use_tls", False)):
            mqtt_cafile = str(main_cfg.get("mqtt_cafile", "")).strip()
            if not mqtt_cafile:
                errors.append("main_server.mqtt_cafile is empty while mqtt_use_tls=true")
            elif not os.path.isfile(mqtt_cafile):
                errors.append(
                    "main_server.mqtt_cafile does not exist, is not a file, "
                    f"or is inaccessible to the current user: {mqtt_cafile}"
                )
            elif not os.access(mqtt_cafile, os.R_OK):
                errors.append(
                    f"main_server.mqtt_cafile is not readable by the current user: {mqtt_cafile}"
                )

    return (len(errors) == 0), errors


def main() -> int:
    ok, errors = validate_current_config()
    if ok:
        print("[check_config] OK")
        return 0

    print("[check_config] FAILED")
    for idx, err in enumerate(errors, start=1):
        print(f"{idx}. {err}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
