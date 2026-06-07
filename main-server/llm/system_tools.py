#!/usr/bin/env python3
"""System tool runtime for the LLM assistant.

This module keeps security system state, executes tool calls, and records
history that can be queried by website assistant flows.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(slots=True)
class ToolRequestContext:
    channel: str
    source: str
    request_id: str


class SystemToolRuntime:
    """Tool executor for arm/disarm/status/history operations."""

    ACTION_TOOL_NAMES = {"arm_system", "disarm_system", "set_sensor_enabled"}

    def __init__(
        self,
        *,
        system_arm_password: str,
        tts_speak_url: str,
        enable_tts_for_actions: bool,
        tts_action_channels: list[str],
        tts_timeout_sec: float,
        service_health_urls: dict[str, str] | None = None,
        system_status_provider: Callable[[str, str], dict[str, Any]] | None = None,
        system_history_provider: Callable[[str, float, int], dict[str, Any]] | None = None,
        history_size: int = 2000,
    ) -> None:
        if history_size <= 0:
            raise ValueError("history_size must be positive")
        if tts_timeout_sec <= 0:
            raise ValueError("tts_timeout_sec must be positive")

        self._system_arm_password = str(system_arm_password)
        self._tts_speak_url = str(tts_speak_url).strip()
        self._enable_tts_for_actions = bool(enable_tts_for_actions)
        self._tts_action_channels = {str(c).strip().lower() for c in tts_action_channels if str(c).strip()}
        if not self._tts_action_channels:
            self._tts_action_channels = {"local"}
        self._tts_timeout_sec = float(tts_timeout_sec)

        self._service_health_urls = dict(service_health_urls or {})
        self._system_status_provider = system_status_provider
        self._system_history_provider = system_history_provider

        self._armed = False
        self._last_state_change_unix = time.time()
        self._last_state_actor = "init"

        self._history: collections.deque[dict[str, Any]] = collections.deque(maxlen=history_size)
        self._event_seq = 0
        self._lock = threading.Lock()

        self.record_event(
            event_type="system_initialized",
            channel="system",
            source="startup",
            details={"armed": self._armed},
        )

    def set_system_status_provider(self, provider: Callable[[str, str], dict[str, Any]] | None) -> None:
        self._system_status_provider = provider

    def set_system_history_provider(self, provider: Callable[[str, float, int], dict[str, Any]] | None) -> None:
        self._system_history_provider = provider

    @staticmethod
    def tool_schemas() -> list[dict[str, Any]]:
        return [
            {
                "name": "get_system_status",
                "description": "Return current security status, alarm state, lock state, sensor readings, connectivity, and service health.",
                "arguments": {
                    "scope": "optional string: all|local|website",
                },
            },
            {
                "name": "get_system_info",
                "description": "Return static system info and capabilities.",
                "arguments": {},
            },
            {
                "name": "arm_system",
                "description": "Arm the system using user-provided password.",
                "arguments": {
                    "password": "optional plaintext password from user",
                    "password_token": "optional token (recommended when available)",
                },
            },
            {
                "name": "disarm_system",
                "description": "Disarm the system using user-provided password.",
                "arguments": {
                    "password": "optional plaintext password from user",
                    "password_token": "optional token (recommended when available)",
                },
            },
            {
                "name": "get_system_history",
                "description": "Return historical events for recent hours (website channel only).",
                "arguments": {
                    "hours": "optional integer, default 2, max 48",
                    "limit": "optional integer, default 100, max 500",
                },
            },
            {
                "name": "set_sensor_enabled",
                "description": "Enable or disable one security sensor using user-provided password.",
                "arguments": {
                    "sensor": "required string: PIR|LDR|REED|VISUAL",
                    "enabled": "required boolean",
                    "password": "optional plaintext password from user",
                    "password_token": "optional token (recommended when available)",
                },
            },
        ]

    @staticmethod
    def sanitize_tool_arguments(args: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for k, v in args.items():
            key = str(k)
            if "password" in key.lower() and key.lower() != "password_token":
                cleaned[key] = "***"
            else:
                cleaned[key] = v
        return cleaned

    def record_event(self, event_type: str, channel: str, source: str, details: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._event_seq += 1
            evt = {
                "event_id": self._event_seq,
                "timestamp": time.time(),
                "event_type": event_type,
                "channel": channel,
                "source": source,
                "details": details,
            }
            self._history.append(evt)
        return evt

    def query_history(self, *, hours: float, limit: int) -> list[dict[str, Any]]:
        since_unix = time.time() - (max(0.0, hours) * 3600.0)
        with self._lock:
            filtered = [item for item in self._history if float(item.get("timestamp", 0.0)) >= since_unix]
        return filtered[-max(1, limit) :]

    def _probe_service(self, name: str, url: str) -> dict[str, Any]:
        start = time.time()
        req = Request(url, method="GET")
        try:
            with urlopen(req, timeout=2.0) as resp:
                status = int(getattr(resp, "status", 200))
                elapsed = int((time.time() - start) * 1000.0)
            return {
                "name": name,
                "url": url,
                "ok": 200 <= status < 400,
                "http_status": status,
                "latency_ms": elapsed,
            }
        except HTTPError as exc:
            elapsed = int((time.time() - start) * 1000.0)
            return {
                "name": name,
                "url": url,
                "ok": False,
                "http_status": int(exc.code),
                "latency_ms": elapsed,
                "error": str(exc),
            }
        except URLError as exc:
            elapsed = int((time.time() - start) * 1000.0)
            return {
                "name": name,
                "url": url,
                "ok": False,
                "latency_ms": elapsed,
                "error": str(exc),
            }

    def get_status_payload(self, channel: str, scope: str) -> dict[str, Any]:
        with self._lock:
            armed = self._armed
            last_change = self._last_state_change_unix
            actor = self._last_state_actor

        payload = {
            "armed": armed,
            "last_state_change_unix": last_change,
            "last_state_actor": actor,
            "requested_channel": channel,
            "scope": scope,
        }

        if self._system_status_provider is not None:
            try:
                security = self._system_status_provider(channel, scope)
                if isinstance(security, dict):
                    payload["security"] = security
                    effective = str(security.get("effective_alarm_state", "")).strip().lower()
                    payload["alarm_state"] = effective or security.get("alarm_state")
                    payload["desired_alarm_state"] = security.get("desired_alarm_state")
                    payload["armed"] = effective in {"armed", "alarm"}
                    payload["alarm_active"] = effective == "alarm"
            except Exception as exc:  # noqa: BLE001
                payload["security_error"] = str(exc)

        health: list[dict[str, Any]] = []
        for name, url in self._service_health_urls.items():
            health.append(self._probe_service(name=name, url=url))
        if health:
            payload["service_health"] = health

        return payload

    def _resolve_password(self, args: dict[str, Any], password_tokens: dict[str, str]) -> str:
        password_hash = str(args.get("password_hash", "")).strip().lower()
        if password_hash:
            return f"sha256:{password_hash}"
        token = str(args.get("password_token", "")).strip()
        if token:
            return str(password_tokens.get(token, ""))
        return str(args.get("password", "")).strip()

    def _check_password(self, provided_password: str) -> bool:
        expected = self._system_arm_password
        if not expected:
            return False
        if provided_password.startswith("sha256:"):
            provided_hash = provided_password[len("sha256:") :].strip().lower()
            return hmac.compare_digest(provided_hash, expected)
        provided_hash = hashlib.sha256(provided_password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(provided_hash, expected)

    def _maybe_tts_action_message(self, *, channel: str, text: str) -> dict[str, Any] | None:
        if not self._enable_tts_for_actions:
            return None
        if channel.strip().lower() not in self._tts_action_channels:
            return None
        if not self._tts_speak_url:
            return {"ok": False, "error": "tts_speak_url not configured"}

        payload = json.dumps({"text": text}).encode("utf-8")
        req = Request(
            self._tts_speak_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=self._tts_timeout_sec) as resp:
                status = int(getattr(resp, "status", 200))
                body = resp.read()
            if status >= 400:
                return {"ok": False, "http_status": status}

            decoded: dict[str, Any] | None = None
            if body:
                try:
                    parsed = json.loads(body.decode("utf-8"))
                    if isinstance(parsed, dict):
                        decoded = parsed
                except json.JSONDecodeError:
                    decoded = None

            return {
                "ok": True,
                "http_status": status,
                "response": decoded,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def execute_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        ctx: ToolRequestContext,
        password_tokens: dict[str, str],
    ) -> dict[str, Any]:
        tool_name = str(name).strip()
        args = dict(arguments)

        if tool_name == "get_system_status":
            scope = str(args.get("scope", "all")).strip().lower() or "all"
            if scope not in {"all", "local", "website"}:
                scope = "all"

            payload = self.get_status_payload(channel=ctx.channel, scope=scope)
            result = {"ok": True, "message": "Current system status retrieved.", "status": payload}
            self.record_event("tool_get_system_status", ctx.channel, ctx.source, {"scope": scope})
            return result

        if tool_name == "get_system_info":
            result = {
                "ok": True,
                "message": "System capability information retrieved.",
                "info": {
                    "channels_supported": ["local", "website"],
                    "tool_names": [s["name"] for s in self.tool_schemas()],
                    "history_access": "website only",
                    "sensor_control": "PIR/LDR/REED/VISUAL can be enabled or disabled with a valid password.",
                    "alarm_activation": "Direct alarm-active activation is not exposed to the assistant; arm and disarm are available.",
                    "lock_control": "Lock state can be reported in status, but lock control is not exposed to the assistant.",
                    "password_access": "Assistant cannot read configured password; it can only submit user-provided values to tools.",
                },
            }
            self.record_event("tool_get_system_info", ctx.channel, ctx.source, {})
            return result

        if tool_name == "get_system_history":
            if ctx.channel.strip().lower() != "website":
                denied = {
                    "ok": False,
                    "message": "Historical state queries are available from website assistant only.",
                    "error_code": "history_not_available_for_channel",
                }
                self.record_event(
                    "tool_get_system_history_denied",
                    ctx.channel,
                    ctx.source,
                    {"reason": "channel_restriction"},
                )
                return denied

            hours = float(args.get("hours", 2))
            hours = min(max(hours, 0.1), 48.0)
            limit = int(args.get("limit", 100))
            limit = min(max(limit, 1), 500)

            events = self.query_history(hours=hours, limit=limit)
            server_history: dict[str, Any] | None = None
            if self._system_history_provider is not None:
                try:
                    provided = self._system_history_provider(ctx.channel, hours, limit)
                    if isinstance(provided, dict):
                        server_history = provided
                except Exception as exc:  # noqa: BLE001
                    server_history = {"ok": False, "error": str(exc)}
            result = {
                "ok": True,
                "message": f"Retrieved system history for the last {hours:.1f} hour(s).",
                "hours": hours,
                "count": len(events),
                "events": events,
            }
            if server_history is not None:
                result["server_history"] = server_history
                result["server_history_count"] = len(server_history.get("history", [])) if isinstance(server_history.get("history"), list) else 0
                result["server_log_count"] = len(server_history.get("logs", [])) if isinstance(server_history.get("logs"), list) else 0
            self.record_event(
                "tool_get_system_history",
                ctx.channel,
                ctx.source,
                {"hours": hours, "limit": limit, "result_count": len(events)},
            )
            return result

        if tool_name in {"arm_system", "disarm_system"}:
            provided_password = self._resolve_password(args=args, password_tokens=password_tokens)
            action = "arm" if tool_name == "arm_system" else "disarm"

            if not provided_password:
                result = {
                    "ok": False,
                    "message": f"Cannot {action} system: password is required.",
                    "error_code": "password_required",
                }
                self.record_event(
                    f"tool_{tool_name}_failed",
                    ctx.channel,
                    ctx.source,
                    {"reason": "missing_password"},
                )
                return result

            if not self._check_password(provided_password):
                result = {
                    "ok": False,
                    "message": f"Cannot {action} system: invalid password.",
                    "error_code": "invalid_password",
                }
                self.record_event(
                    f"tool_{tool_name}_failed",
                    ctx.channel,
                    ctx.source,
                    {"reason": "invalid_password"},
                )
                return result

            with self._lock:
                self._armed = tool_name == "arm_system"
                self._last_state_change_unix = time.time()
                self._last_state_actor = f"{ctx.channel}:{ctx.source}"
                armed_now = self._armed

            confirm_msg = "System armed successfully." if armed_now else "System disarmed successfully."
            tts_result = self._maybe_tts_action_message(channel=ctx.channel, text=confirm_msg)

            self.record_event(
                f"tool_{tool_name}_ok",
                ctx.channel,
                ctx.source,
                {"armed": armed_now, "tts_sent": bool(tts_result and tts_result.get("ok"))},
            )

            return {
                "ok": True,
                "message": confirm_msg,
                "armed": armed_now,
                "tts": tts_result,
                "_password_redaction_value": provided_password,
            }

        if tool_name == "set_sensor_enabled":
            provided_password = self._resolve_password(args=args, password_tokens=password_tokens)
            sensor = str(args.get("sensor", "")).strip().upper()
            enabled_raw = args.get("enabled")
            if isinstance(enabled_raw, str):
                enabled = enabled_raw.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}
            else:
                enabled = bool(enabled_raw)

            if sensor not in {"PIR", "LDR", "REED", "VISUAL"}:
                result = {
                    "ok": False,
                    "message": "Cannot update sensor: sensor must be PIR, LDR, REED, or VISUAL.",
                    "error_code": "invalid_sensor",
                }
                self.record_event("tool_set_sensor_enabled_failed", ctx.channel, ctx.source, {"reason": "invalid_sensor"})
                return result

            if not provided_password:
                result = {
                    "ok": False,
                    "message": "Cannot update sensor: password is required.",
                    "error_code": "password_required",
                }
                self.record_event("tool_set_sensor_enabled_failed", ctx.channel, ctx.source, {"reason": "missing_password"})
                return result

            if not self._check_password(provided_password):
                result = {
                    "ok": False,
                    "message": "Cannot update sensor: invalid password.",
                    "error_code": "invalid_password",
                }
                self.record_event("tool_set_sensor_enabled_failed", ctx.channel, ctx.source, {"reason": "invalid_password"})
                return result

            message = f"{sensor} sensor {'enabled' if enabled else 'disabled'}."
            tts_result = self._maybe_tts_action_message(channel=ctx.channel, text=message)
            self.record_event(
                "tool_set_sensor_enabled_ok",
                ctx.channel,
                ctx.source,
                {"sensor": sensor, "enabled": enabled, "tts_sent": bool(tts_result and tts_result.get("ok"))},
            )
            return {
                "ok": True,
                "message": message,
                "sensor_action": {"sensor": sensor, "enabled": enabled},
                "tts": tts_result,
                "_password_redaction_value": provided_password,
            }

        self.record_event(
            "tool_unknown",
            ctx.channel,
            ctx.source,
            {"tool_name": tool_name},
        )
        return {
            "ok": False,
            "message": f"Unknown tool: {tool_name}",
            "error_code": "unknown_tool",
        }
