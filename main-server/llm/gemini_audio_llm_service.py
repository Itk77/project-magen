#!/usr/bin/env python3
"""Gemini LLM provider with tool-enabled assistant flows.

Supports:
- Audio input (single turn + live) and text input
- Tool use for system status/control/history
- Optional Gemini audio response passthrough
- Dedicated model config JSON via path pointer
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import json
import re
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.append(str(_THIS_DIR))

from system_tools import SystemToolRuntime, ToolRequestContext

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency 'google-genai'. Install with: pip install google-genai") from exc


class GeminiAudioLLMService:
    """Gemini provider for audio/text assistant with tools."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8094,
        service_id: str = "llm-main",
        api_key: str = "",
        model_config_path: str = "main-server/llm/gemini_model_config.json",
        request_timeout_sec: float = 45.0,
        max_events: int = 500,
        max_history: int = 200,
        assistant_max_tool_rounds: int = 4,
        system_arm_password: str = "",
        tts_speak_url: str = "http://127.0.0.1:8093/tts/speak",
        enable_tts_for_actions: bool = True,
        tts_action_channels: str = "local",
        tts_timeout_sec: float = 12.0,
        service_health_urls: dict[str, str] | None = None,
    ) -> None:
        if port <= 0:
            raise ValueError("port must be positive")
        if request_timeout_sec <= 0:
            raise ValueError("request_timeout_sec must be positive")
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        if max_history <= 0:
            raise ValueError("max_history must be positive")
        if assistant_max_tool_rounds <= 0:
            raise ValueError("assistant_max_tool_rounds must be positive")

        self.host = host
        self.port = int(port)
        self.service_id = service_id
        self.api_key = api_key.strip()
        self.model_config_path = str(Path(model_config_path).expanduser().resolve())
        self.request_timeout_sec = float(request_timeout_sec)
        self.assistant_max_tool_rounds = int(assistant_max_tool_rounds)

        self._model_config = self._load_model_config(self.model_config_path)

        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=max_events)
        self._history: collections.deque[dict[str, Any]] = collections.deque(maxlen=max_history)
        self._lock = threading.Lock()

        self._started_at = time.time()
        self._last_error = ""
        self._server: LLMHTTPServer | None = None
        self._client: Any = None

        channels = [c.strip() for c in str(tts_action_channels).split(",") if c.strip()]
        self._tool_runtime = SystemToolRuntime(
            system_arm_password=system_arm_password,
            tts_speak_url=tts_speak_url,
            enable_tts_for_actions=bool(enable_tts_for_actions),
            tts_action_channels=channels,
            tts_timeout_sec=float(tts_timeout_sec),
            service_health_urls=service_health_urls or {},
            history_size=4000,
        )

    def set_system_status_provider(self, provider: Any) -> None:
        self._tool_runtime.set_system_status_provider(provider)

    def set_system_history_provider(self, provider: Any) -> None:
        self._tool_runtime.set_system_history_provider(provider)

    @staticmethod
    def _load_model_config(config_path: str) -> dict[str, Any]:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"LLM model config file not found: {path}")

        with path.open("r", encoding="utf-8") as f:
            parsed = json.load(f)

        if not isinstance(parsed, dict):
            raise ValueError("model config JSON must be an object")
        required_keys = {
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
        missing = sorted([k for k in required_keys if k not in parsed])
        if missing:
            raise ValueError(f"model config missing required keys: {', '.join(missing)}")

        if not isinstance(parsed["response_modalities"], list) or not parsed["response_modalities"]:
            raise ValueError("model config 'response_modalities' must be a non-empty list")

        audio_resp = parsed["audio_response"]
        if not isinstance(audio_resp, dict):
            raise ValueError("model config 'audio_response' must be an object")
        for key in ("enabled", "voice_name", "mime_type"):
            if key not in audio_resp:
                raise ValueError(f"model config 'audio_response' missing key: {key}")

        live_cfg = parsed["live"]
        if not isinstance(live_cfg, dict):
            raise ValueError("model config 'live' must be an object")
        for key in ("response_modalities", "max_receive_seconds"):
            if key not in live_cfg:
                raise ValueError(f"model config 'live' missing key: {key}")
        if not isinstance(live_cfg["response_modalities"], list) or not live_cfg["response_modalities"]:
            raise ValueError("model config 'live.response_modalities' must be a non-empty list")

        normalized = dict(parsed)
        normalized["audio_text_model"] = str(parsed["audio_text_model"]).strip()
        normalized["live_model"] = str(parsed["live_model"]).strip()
        normalized["temperature"] = float(parsed["temperature"])
        normalized["top_p"] = float(parsed["top_p"])
        normalized["max_output_tokens"] = int(parsed["max_output_tokens"])
        normalized["thinking_level"] = str(parsed["thinking_level"]).strip().upper()
        normalized["include_thoughts"] = bool(parsed["include_thoughts"])
        normalized["system_instruction"] = str(parsed["system_instruction"])
        normalized["assistant_instruction"] = str(parsed["assistant_instruction"])
        normalized["default_prompt"] = str(parsed["default_prompt"])
        normalized["input_audio_mime_type"] = str(parsed["input_audio_mime_type"]).strip()
        normalized["response_modalities"] = [str(m).upper() for m in parsed["response_modalities"] if str(m).strip()]

        normalized["audio_response"] = {
            "enabled": bool(audio_resp["enabled"]),
            "voice_name": str(audio_resp["voice_name"]).strip(),
            "mime_type": str(audio_resp["mime_type"]).strip(),
        }
        normalized["live"] = {
            "response_modalities": [
                str(m).upper() for m in live_cfg["response_modalities"] if str(m).strip()
            ],
            "max_receive_seconds": int(live_cfg["max_receive_seconds"]),
        }

        if not normalized["audio_text_model"] or not normalized["live_model"]:
            raise ValueError("model config model names must not be empty")
        if normalized["max_output_tokens"] <= 0:
            raise ValueError("model config 'max_output_tokens' must be positive")
        if normalized["live"]["max_receive_seconds"] <= 0:
            raise ValueError("model config 'live.max_receive_seconds' must be positive")
        if not normalized["response_modalities"]:
            raise ValueError("model config 'response_modalities' must contain at least one modality")
        if not normalized["live"]["response_modalities"]:
            raise ValueError("model config 'live.response_modalities' must contain at least one modality")
        if not normalized["input_audio_mime_type"]:
            raise ValueError("model config 'input_audio_mime_type' must not be empty")
        if not normalized["audio_response"]["voice_name"]:
            raise ValueError("model config 'audio_response.voice_name' must not be empty")

        return normalized

    @staticmethod
    def _to_plain(obj: Any, depth: int = 0) -> Any:
        if depth > 8:
            return str(obj)
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, bytes):
            return {"__bytes__": base64.b64encode(obj).decode("ascii")}
        if isinstance(obj, dict):
            return {str(k): GeminiAudioLLMService._to_plain(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [GeminiAudioLLMService._to_plain(v, depth + 1) for v in obj]

        model_dump = getattr(obj, "model_dump", None)
        if callable(model_dump):
            try:
                return GeminiAudioLLMService._to_plain(model_dump(), depth + 1)
            except Exception:  # noqa: BLE001
                pass

        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            try:
                return GeminiAudioLLMService._to_plain(to_dict(), depth + 1)
            except Exception:  # noqa: BLE001
                pass

        as_dict = getattr(obj, "__dict__", None)
        if isinstance(as_dict, dict):
            return {
                str(k): GeminiAudioLLMService._to_plain(v, depth + 1)
                for k, v in as_dict.items()
                if not str(k).startswith("_")
            }

        return str(obj)

    @staticmethod
    def _extract_text_and_audio(payload: Any) -> tuple[list[str], list[dict[str, Any]], bool]:
        text_parts: list[str] = []
        audio_parts: list[dict[str, Any]] = []
        turn_complete = False

        def walk(node: Any) -> None:
            nonlocal turn_complete
            if isinstance(node, dict):
                if node.get("turn_complete") is True or node.get("turnComplete") is True:
                    turn_complete = True

                text_val = node.get("text")
                if isinstance(text_val, str) and text_val.strip():
                    text_parts.append(text_val)

                inline = node.get("inline_data")
                if inline is None:
                    inline = node.get("inlineData")
                if isinstance(inline, dict):
                    data = inline.get("data")
                    mime = inline.get("mime_type") or inline.get("mimeType") or "application/octet-stream"
                    if isinstance(data, bytes):
                        audio_parts.append(
                            {
                                "audio_b64": base64.b64encode(data).decode("ascii"),
                                "mime_type": str(mime),
                            }
                        )
                    elif isinstance(data, str) and data:
                        audio_parts.append({"audio_b64": data, "mime_type": str(mime)})

                for value in node.values():
                    walk(value)
                return

            if isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return text_parts, audio_parts, turn_complete

    def _emit_event(self, event_type: str, **data: Any) -> None:
        evt = {
            "id": len(self._events) + 1,
            "timestamp": time.time(),
            "event": event_type,
            **data,
        }
        with self._lock:
            self._events.append(evt)

    def _append_history(self, item: dict[str, Any]) -> None:
        with self._lock:
            self._history.append(item)

    def _recent_turn_context(self, *, channel: str, limit: int = 4) -> str:
        with self._lock:
            items = list(self._history)

        relevant: list[dict[str, Any]] = []
        channel_norm = channel.strip().lower()
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            if str(item.get("kind", "")).strip() != "assistant_turn":
                continue
            if str(item.get("channel", "")).strip().lower() != channel_norm:
                continue
            relevant.append(item)
            if len(relevant) >= limit:
                break

        if not relevant:
            return ""

        lines = ["Recent conversation context (oldest to newest):"]
        for item in reversed(relevant):
            assistant_text = str(item.get("assistant_text", "")).strip()
            original_request = str(item.get("original_request", "")).strip()
            tool_calls = item.get("tool_calls", [])
            tool_results = item.get("tool_results", [])
            if original_request:
                lines.append(f"- Prior user request summary: {original_request}")
            if assistant_text:
                lines.append(f"- Prior assistant reply: {assistant_text}")
            if isinstance(tool_calls, list) and tool_calls:
                tool_names = [str(call.get('name', '')).strip() for call in tool_calls if isinstance(call, dict)]
                tool_names = [name for name in tool_names if name]
                if tool_names:
                    lines.append(f"- Prior tool calls: {', '.join(tool_names)}")
            if isinstance(tool_results, list) and tool_results:
                result_messages: list[str] = []
                for result_item in tool_results:
                    if not isinstance(result_item, dict):
                        continue
                    result = result_item.get("result")
                    if not isinstance(result, dict):
                        continue
                    msg = str(result.get("message", "")).strip()
                    if msg:
                        result_messages.append(msg)
                if result_messages:
                    lines.append(f"- Prior tool results: {' | '.join(result_messages)}")
        return "\n".join(lines)

    def _get_client(self) -> Any:
        if not self.api_key:
            raise RuntimeError("Gemini API key is empty. Set GEMINI_API_KEY or LLM_API_KEY.")
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    @staticmethod
    def _part_text(text: str) -> Any:
        part_cls = getattr(genai_types, "Part", None)
        if part_cls is not None and hasattr(part_cls, "from_text"):
            return part_cls.from_text(text=text)
        return {"text": text}

    @staticmethod
    def _part_audio(data: bytes, mime_type: str) -> Any:
        part_cls = getattr(genai_types, "Part", None)
        if part_cls is not None and hasattr(part_cls, "from_bytes"):
            return part_cls.from_bytes(data=data, mime_type=mime_type)
        return {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(data).decode("ascii")}}

    @staticmethod
    def _content_user(parts: list[Any]) -> Any:
        content_cls = getattr(genai_types, "Content", None)
        if content_cls is not None:
            try:
                return content_cls(role="user", parts=parts)
            except Exception:  # noqa: BLE001
                pass
        return {"role": "user", "parts": parts}

    @staticmethod
    def _class_has_field(cls: Any, field_name: str) -> bool:
        fields = getattr(cls, "model_fields", None)
        if isinstance(fields, dict):
            return field_name in fields
        return False

    def _build_thinking_config(self) -> Any | None:
        """Build ThinkingConfig compatibly across google-genai versions."""
        thinking_cfg_cls = getattr(genai_types, "ThinkingConfig", None)
        if thinking_cfg_cls is None:
            return None

        thinking_level = str(self._model_config["thinking_level"]).strip().upper()

        # Newer SDKs may support explicit thinking level control.
        if self._class_has_field(thinking_cfg_cls, "thinking_level"):
            try:
                return thinking_cfg_cls(thinking_level=thinking_level)
            except Exception:  # noqa: BLE001
                return {"thinking_level": thinking_level}

        # Older SDKs (e.g. 0.8.0) only support include_thoughts.
        # For low-latency behavior, keep thoughts excluded.
        if self._class_has_field(thinking_cfg_cls, "include_thoughts"):
            include_thoughts = bool(self._model_config["include_thoughts"])
            try:
                return thinking_cfg_cls(include_thoughts=include_thoughts)
            except Exception:  # noqa: BLE001
                return {"include_thoughts": include_thoughts}

        return None

    def _build_generate_config(
        self,
        response_modalities: list[str],
        include_audio_response: bool,
        voice_name: str,
    ) -> Any:
        cfg: dict[str, Any] = {
            "temperature": float(self._model_config["temperature"]),
            "top_p": float(self._model_config["top_p"]),
            "max_output_tokens": int(self._model_config["max_output_tokens"]),
            "response_modalities": response_modalities,
        }

        sys_inst = str(self._model_config["system_instruction"]).strip()
        if sys_inst:
            cfg["system_instruction"] = sys_inst

        thinking_cfg = self._build_thinking_config()
        if thinking_cfg is not None:
            cfg["thinking_config"] = thinking_cfg

        if include_audio_response and "AUDIO" in [m.upper() for m in response_modalities]:
            try:
                prebuilt_cls = getattr(genai_types, "PrebuiltVoiceConfig")
                voice_cfg_cls = getattr(genai_types, "VoiceConfig")
                speech_cfg_cls = getattr(genai_types, "SpeechConfig")
                cfg["speech_config"] = speech_cfg_cls(
                    voice_config=voice_cfg_cls(
                        prebuilt_voice_config=prebuilt_cls(
                            voice_name=voice_name,
                        )
                    )
                )
            except Exception:  # noqa: BLE001
                cfg["speech_config"] = {
                    "voice_config": {"prebuilt_voice_config": {"voice_name": voice_name}}
                }

        config_cls = getattr(genai_types, "GenerateContentConfig", None)
        if config_cls is None:
            return cfg
        try:
            return config_cls(**cfg)
        except Exception:  # noqa: BLE001
            return cfg

    def _generate_content_once(
        self,
        *,
        model: str,
        parts: list[Any],
        response_modalities: list[str],
        include_audio_response: bool,
        voice_name: str,
    ) -> Any:
        cfg = self._build_generate_config(
            response_modalities=response_modalities,
            include_audio_response=include_audio_response,
            voice_name=voice_name,
        )
        client = self._get_client()
        return client.models.generate_content(
            model=model,
            contents=[self._content_user(parts)],
            config=cfg,
        )

    @staticmethod
    def _redact_sensitive_output(text: str) -> str:
        return re.sub(r"(?i)(password\s*(?:is|=|:)?\s*)(\S+)", r"\1***", text)

    @staticmethod
    def _extract_password_tokens(text: str) -> tuple[str, dict[str, str]]:
        token_map: dict[str, str] = {}
        idx = 0

        pattern = re.compile(r"(?i)(\bpassword\b\s*(?:is|=|:)?\s*)([^\s,.;]+)")

        def repl(match: re.Match[str]) -> str:
            nonlocal idx
            idx += 1
            token = f"PWD_TOKEN_{idx}"
            token_map[token] = match.group(2)
            return f"{match.group(1)}{token}"

        sanitized = pattern.sub(repl, text)
        return sanitized, token_map

    @staticmethod
    def _extract_first_json_object(text: str) -> str | None:
        s = text.strip()
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", s)
            s = re.sub(r"\n```$", "", s.strip())

        if s.startswith("{") and s.endswith("}"):
            return s

        start = s.find("{")
        if start < 0:
            return None

        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue

            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
        return None

    @staticmethod
    def _extract_assistant_response_fallback(text: str) -> str:
        s = text.strip()
        if not s:
            return ""
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", s)
            s = re.sub(r"\n```$", "", s.strip())

        m = re.search(r'"assistant_response"\s*:\s*"', s)
        if not m:
            return ""

        i = m.end()
        out_chars: list[str] = []
        esc = False
        closed = False
        while i < len(s):
            ch = s[i]
            i += 1
            if esc:
                out_chars.append("\\" + ch)
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                closed = True
                break
            out_chars.append(ch)

        raw_inner = "".join(out_chars)
        if not raw_inner and not closed:
            return ""

        # Decode common JSON string escapes when present.
        try:
            decoded = json.loads(f'"{raw_inner}"')
            if isinstance(decoded, str):
                return decoded.strip()
        except Exception:  # noqa: BLE001
            pass

        return raw_inner.strip()

    def _safe_assistant_text(self, raw_text: str) -> str:
        cleaned = raw_text.strip()
        if not cleaned:
            return ""
        extracted = self._extract_assistant_response_fallback(cleaned)
        return extracted if extracted else cleaned

    def _parse_tool_plan(self, raw_text: str) -> dict[str, Any]:
        block = self._extract_first_json_object(raw_text)
        if not block:
            return {
                "assistant_response": self._safe_assistant_text(raw_text),
                "tool_calls": [],
            }

        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            return {
                "assistant_response": self._safe_assistant_text(raw_text),
                "tool_calls": [],
            }

        if not isinstance(parsed, dict):
            return {
                "assistant_response": self._safe_assistant_text(raw_text),
                "tool_calls": [],
            }

        response_text = str(parsed.get("assistant_response", "")).strip()
        if not response_text:
            response_text = self._safe_assistant_text(raw_text)
        tool_calls_raw = parsed.get("tool_calls", [])
        tool_calls: list[dict[str, Any]] = []

        if isinstance(tool_calls_raw, list):
            for item in tool_calls_raw[: self.assistant_max_tool_rounds]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                args = item.get("arguments", {})
                if not name:
                    continue
                if not isinstance(args, dict):
                    args = {}
                tool_calls.append({"name": name, "arguments": args})

        return {
            "assistant_response": response_text,
            "tool_calls": tool_calls,
        }

    def _build_tool_instruction(
        self,
        *,
        channel: str,
        password_tokens: dict[str, str],
    ) -> str:
        schemas = self._tool_runtime.tool_schemas()
        tool_json = json.dumps(schemas, ensure_ascii=True)
        assistant_instruction = str(self._model_config["assistant_instruction"]).strip()

        lines = [
            assistant_instruction or "You are a home security assistant.",
            f"Current channel: {channel}",
            "Language requirements:",
            "- The user will likely speak Hebrew or English; Hebrew is more likely.",
            "- Detect the language from the current user turn and reply in that same language.",
            "- If language is mixed/unclear, default to Hebrew.",
            "You may use tools to get status/info/history, arm/disarm, or enable/disable sensors.",
            "Return STRICT JSON only with this shape:",
            '{"assistant_response":"text", "tool_calls":[{"name":"tool_name","arguments":{}}]}',
            "Do not include markdown fences.",
            "If no tools needed, return tool_calls as empty list.",
            "Never reveal secrets or configured passwords in assistant_response.",
            "Intent safety requirements:",
            "- If the user asks to arm the system, call arm_system.",
            "- If the user asks to disarm, deactivate, stop the alarm, turn it off, cancel arming, or unlock, call disarm_system.",
            "- If the user asks to enable/disable PIR, LDR, reed, door, or visual/camera detection sensors, call set_sensor_enabled.",
            "- If the user asks to activate the alarm/siren immediately, do not call arm_system as a substitute; explain that direct alarm activation is not exposed to the assistant.",
            "- If the user asks to control the lock, explain that lock control is not exposed to the assistant; use get_system_status for lock state.",
            "- Hebrew examples for disarm intent include: נטרל, כבה, בטל דריכה, עצור אזעקה.",
            "- Hebrew examples for arm intent include: דרך, הפעל, תדרך, דרוך את המערכת.",
            "- Never substitute arm_system for a disarm request or disarm_system for an arm request.",
            "- If intent between arm and disarm is genuinely unclear, do not guess; ask a short clarification question instead of calling a tool.",
            "Tool definitions:",
            tool_json,
        ]

        if channel.strip().lower() != "website":
            lines.append("Do not call get_system_history unless channel is website.")

        try:
            current_status = self._tool_runtime.get_status_payload(channel=channel, scope="local")
            current_armed = bool(current_status.get("armed", False))
            lines.append(f"Current system armed state: {'armed' if current_armed else 'disarmed'}.")
        except Exception:  # noqa: BLE001
            pass

        if password_tokens:
            lines.append(
                "Available password tokens from user input: "
                + json.dumps(sorted(password_tokens.keys()), ensure_ascii=True)
            )
            lines.append("If a protected tool needs a password and token exists, use arguments.password_token instead of raw password.")

        recent_context = self._recent_turn_context(channel=channel)
        if recent_context:
            lines.append(recent_context)

        return "\n".join(lines)

    def _execute_tool_calls(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        ctx: ToolRequestContext,
        password_tokens: dict[str, str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        sanitized_calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []

        for call in tool_calls[: self.assistant_max_tool_rounds]:
            name = str(call.get("name", "")).strip()
            args = call.get("arguments", {})
            if not isinstance(args, dict):
                args = {}

            safe_args = self._tool_runtime.sanitize_tool_arguments(args)
            sanitized_calls.append({"name": name, "arguments": safe_args})

            result = self._tool_runtime.execute_tool(
                name=name,
                arguments=args,
                ctx=ctx,
                password_tokens=password_tokens,
            )
            results.append({"name": name, "result": result})

            self._emit_event(
                "tool_executed",
                request_id=ctx.request_id,
                tool_name=name,
                ok=bool(result.get("ok")),
            )

        return sanitized_calls, results

    @staticmethod
    def _successful_password_values(
        *,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        password_tokens: dict[str, str],
    ) -> list[str]:
        values: list[str] = []
        action_tools = SystemToolRuntime.ACTION_TOOL_NAMES
        for call, item in zip(tool_calls, tool_results):
            name = str(call.get("name", "")).strip()
            if name not in action_tools:
                continue
            result = item.get("result") if isinstance(item, dict) else None
            if not isinstance(result, dict) or not bool(result.get("ok")):
                continue
            redaction_value = str(result.pop("_password_redaction_value", "")).strip()
            if redaction_value:
                values.append(redaction_value)
                continue
            args = call.get("arguments", {})
            if not isinstance(args, dict):
                continue

            token = str(args.get("password_token", "")).strip()
            if token and token in password_tokens:
                values.append(str(password_tokens[token]))
                continue

            for key in ("password", "password_hash"):
                value = str(args.get(key, "")).strip()
                if value:
                    values.append(value)

        deduped: list[str] = []
        for value in values:
            if value and value not in deduped:
                deduped.append(value)
        return deduped

    @staticmethod
    def _redact_values(text: str, values: list[str]) -> str:
        redacted = text
        for value in values:
            redacted = redacted.replace(value, "***")
        return redacted

    def _compose_final_response(
        self,
        *,
        model: str,
        channel: str,
        source: str,
        original_request: str,
        plan_assistant_response: str,
        tool_results: list[dict[str, Any]],
    ) -> str:
        if not tool_results:
            text = plan_assistant_response.strip()
            if '"assistant_response"' in text:
                text = self._safe_assistant_text(text)
            if text:
                return self._redact_sensitive_output(text)
            return "I processed your request."

        summary_payload = {
            "channel": channel,
            "source": source,
            "original_request": original_request,
            "assistant_draft": plan_assistant_response,
            "tool_results": tool_results,
        }

        prompt = (
            "Generate a concise final user-facing reply based on tool results. "
            "Do not reveal secrets or any password values. "
            "Reply in the user's language (Hebrew or English). "
            "If assistant_draft language is clear, keep that same language. "
            "If an action failed, explain why and what user should do next.\n\n"
            f"DATA:\n{json.dumps(summary_payload, ensure_ascii=True)}"
        )

        try:
            response = self._generate_content_once(
                model=model,
                parts=[self._part_text(prompt)],
                response_modalities=["TEXT"],
                include_audio_response=False,
                voice_name=str(self._model_config["audio_response"]["voice_name"]),
            )
            text = str(getattr(response, "text", "")).strip()
            if text:
                return self._redact_sensitive_output(text)
        except Exception:  # noqa: BLE001
            pass

        messages: list[str] = []
        for item in tool_results:
            result = item.get("result", {})
            msg = str(result.get("message", "")).strip()
            if msg:
                messages.append(msg)
        if messages:
            return self._redact_sensitive_output(" ".join(messages))
        return "Done."

    def _assistant_core(
        self,
        *,
        input_parts: list[Any],
        channel: str,
        source: str,
        password_tokens: dict[str, str],
        original_request_for_history: str,
    ) -> dict[str, Any]:
        request_id = f"req-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        channel_norm = channel.strip().lower() or "local"
        source_norm = source.strip() or "unknown"

        ctx = ToolRequestContext(channel=channel_norm, source=source_norm, request_id=request_id)

        self._tool_runtime.record_event(
            event_type="assistant_request",
            channel=channel_norm,
            source=source_norm,
            details={
                "request_id": request_id,
                "has_password_tokens": bool(password_tokens),
            },
        )

        instruction = self._build_tool_instruction(channel=channel_norm, password_tokens=password_tokens)
        model = str(self._model_config["audio_text_model"])

        started = time.time()
        response = self._generate_content_once(
            model=model,
            parts=[self._part_text(instruction)] + input_parts,
            response_modalities=["TEXT"],
            include_audio_response=False,
            voice_name=str(self._model_config["audio_response"]["voice_name"]),
        )
        latency_ms = int((time.time() - started) * 1000.0)

        raw_text = str(getattr(response, "text", "")).strip()
        if not raw_text:
            plain = self._to_plain(response)
            texts, _, _ = self._extract_text_and_audio(plain)
            raw_text = "\n".join(texts).strip()

        plan = self._parse_tool_plan(raw_text)
        tool_calls = list(plan.get("tool_calls", []))
        sanitized_calls, tool_results = self._execute_tool_calls(
            tool_calls=tool_calls,
            ctx=ctx,
            password_tokens=password_tokens,
        )
        password_redactions = self._successful_password_values(
            tool_calls=tool_calls,
            tool_results=tool_results,
            password_tokens=password_tokens,
        )

        final_text = self._compose_final_response(
            model=model,
            channel=channel_norm,
            source=source_norm,
            original_request=original_request_for_history,
            plan_assistant_response=str(plan.get("assistant_response", "")),
            tool_results=tool_results,
        )

        result = {
            "request_id": request_id,
            "channel": channel_norm,
            "source": source_norm,
            "latency_ms": latency_ms,
            "assistant_text": final_text,
            "tool_calls": sanitized_calls,
            "tool_results": tool_results,
        }
        if password_redactions and original_request_for_history:
            result["redacted_user_text"] = self._redact_values(original_request_for_history, password_redactions)

        self._append_history(
            {
                "timestamp": time.time(),
                "kind": "assistant_turn",
                "request_id": request_id,
                "channel": channel_norm,
                "source": source_norm,
                "original_request": original_request_for_history,
                "tool_count": len(tool_results),
                "tool_calls": sanitized_calls,
                "tool_results": tool_results,
                "assistant_text": final_text,
                "redacted_user_text": result.get("redacted_user_text", original_request_for_history),
            }
        )

        self._emit_event(
            "assistant_turn_ok",
            request_id=request_id,
            channel=channel_norm,
            source=source_norm,
            tool_count=len(tool_results),
        )
        return result

    def assistant_text_turn(self, *, text: str, channel: str, source: str) -> dict[str, Any]:
        raw = text.strip()
        if not raw:
            raise ValueError("text is required")

        sanitized, token_map = self._extract_password_tokens(raw)
        input_parts = [self._part_text(f"User text request:\n{sanitized}")]

        result = self._assistant_core(
            input_parts=input_parts,
            channel=channel,
            source=source,
            password_tokens=token_map,
            original_request_for_history=sanitized,
        )
        if "redacted_user_text" in result:
            for token, value in token_map.items():
                result["redacted_user_text"] = str(result["redacted_user_text"]).replace(token, "***")
        return result

    def assistant_audio_turn(
        self,
        *,
        audio_b64: str,
        audio_mime_type: str,
        prompt: str,
        channel: str,
        source: str,
    ) -> dict[str, Any]:
        if not audio_b64.strip():
            raise ValueError("audio_b64 is required")

        audio_bytes = base64.b64decode(audio_b64)
        selected_prompt = prompt.strip() or str(self._model_config["default_prompt"]).strip()
        p = (
            "Audio user request received. "
            "Understand intent directly from audio, then call tools when needed. "
            + selected_prompt
        )

        input_parts = [
            self._part_text(p),
            self._part_audio(audio_bytes, audio_mime_type),
        ]

        return self._assistant_core(
            input_parts=input_parts,
            channel=channel,
            source=source,
            password_tokens={},
            original_request_for_history="[audio_request]",
        )

    def assistant_live_turn(
        self,
        *,
        audio_chunks_b64: list[str],
        audio_mime_type: str,
        prompt: str,
        channel: str,
        source: str,
        model: str | None,
        max_receive_seconds: int | None,
    ) -> dict[str, Any]:
        if not audio_chunks_b64:
            raise ValueError("audio_chunks_b64 is required")

        chunks = [base64.b64decode(item) for item in audio_chunks_b64 if item.strip()]
        if not chunks:
            raise ValueError("audio_chunks_b64 did not contain valid data")

        selected_prompt = prompt.strip() or str(self._model_config["default_prompt"]).strip()
        p = (
            "Live audio user request received in chunks. "
            "Understand intent directly from audio, then call tools when needed. "
            + selected_prompt
        )

        combined = b"".join(chunks)
        input_parts = [
            self._part_text(p),
            self._part_audio(combined, audio_mime_type),
        ]

        assistant = self._assistant_core(
            input_parts=input_parts,
            channel=channel,
            source=source,
            password_tokens={},
            original_request_for_history="[live_audio_request]",
        )
        assistant["live"] = {
            "model": model or str(self._model_config["live_model"]),
            "chunk_count": len(chunks),
            "max_receive_seconds": max_receive_seconds,
        }
        return assistant

    # Low-level API preserved for direct raw Gemini interactions.
    def audio_turn(
        self,
        *,
        audio_b64: str,
        audio_mime_type: str,
        prompt: str,
        model: str | None,
        response_modalities: list[str] | None,
        include_audio_response: bool,
        voice_name: str,
    ) -> dict[str, Any]:
        if not audio_b64.strip():
            raise ValueError("audio_b64 is required")

        audio_bytes = base64.b64decode(audio_b64)
        selected_model = (model or str(self._model_config["audio_text_model"])).strip()
        selected_prompt = prompt.strip() or str(self._model_config["default_prompt"]).strip()

        modalities = response_modalities or list(self._model_config["response_modalities"])
        modalities = [str(m).upper() for m in modalities if str(m).strip()]
        if not modalities:
            modalities = ["TEXT"]

        parts = []
        if selected_prompt:
            parts.append(self._part_text(selected_prompt))
        parts.append(self._part_audio(audio_bytes, audio_mime_type))

        started = time.time()
        response = self._generate_content_once(
            model=selected_model,
            parts=parts,
            response_modalities=modalities,
            include_audio_response=include_audio_response,
            voice_name=voice_name,
        )
        elapsed_ms = int((time.time() - started) * 1000.0)

        plain = self._to_plain(response)
        text_items, audio_items, _ = self._extract_text_and_audio(plain)

        text = ""
        text_attr = getattr(response, "text", None)
        if isinstance(text_attr, str) and text_attr.strip():
            text = text_attr
        elif text_items:
            text = "\n".join(text_items).strip()

        usage: dict[str, Any] | None = None
        usage_obj = getattr(response, "usage_metadata", None)
        if usage_obj is not None:
            usage = self._to_plain(usage_obj)
            if not isinstance(usage, dict):
                usage = {"value": usage}

        result = {
            "model": selected_model,
            "latency_ms": elapsed_ms,
            "response_modalities": modalities,
            "text": text,
            "audio": audio_items,
            "usage": usage,
        }

        self._append_history(
            {
                "kind": "audio_turn",
                "timestamp": time.time(),
                "model": selected_model,
                "modalities": modalities,
                "latency_ms": elapsed_ms,
                "has_text": bool(text),
                "audio_parts": len(audio_items),
            }
        )
        self._emit_event(
            "audio_turn_ok",
            model=selected_model,
            latency_ms=elapsed_ms,
            has_text=bool(text),
            audio_parts=len(audio_items),
        )

        return result

    def _build_live_connect_config(self, modalities: list[str], include_audio_response: bool, voice_name: str) -> Any:
        cfg: dict[str, Any] = {
            "response_modalities": modalities,
        }

        sys_inst = str(self._model_config["system_instruction"]).strip()
        if sys_inst:
            cfg["system_instruction"] = sys_inst

        live_cfg_cls = getattr(genai_types, "LiveConnectConfig", None)
        if live_cfg_cls is not None and self._class_has_field(live_cfg_cls, "thinking_config"):
            thinking_cfg = self._build_thinking_config()
            if thinking_cfg is not None:
                cfg["thinking_config"] = thinking_cfg

        if include_audio_response and "AUDIO" in [m.upper() for m in modalities]:
            cfg["speech_config"] = {
                "voice_config": {"prebuilt_voice_config": {"voice_name": voice_name}}
            }

        if live_cfg_cls is None:
            return cfg
        try:
            return live_cfg_cls(**cfg)
        except Exception:  # noqa: BLE001
            return cfg

    @staticmethod
    def _make_blob(data: bytes, mime_type: str) -> Any:
        blob_cls = getattr(genai_types, "Blob", None)
        if blob_cls is None:
            return {"data": data, "mime_type": mime_type}
        try:
            return blob_cls(data=data, mime_type=mime_type)
        except Exception:  # noqa: BLE001
            return {"data": data, "mime_type": mime_type}

    async def _live_audio_turn_async(
        self,
        *,
        audio_chunks: list[bytes],
        audio_mime_type: str,
        prompt: str,
        model: str,
        response_modalities: list[str],
        include_audio_response: bool,
        voice_name: str,
        max_receive_seconds: int,
    ) -> dict[str, Any]:
        client = self._get_client()
        aio_client = getattr(client, "aio", None)
        if aio_client is None:
            raise RuntimeError("google-genai client does not expose aio live interface")

        live_api = getattr(aio_client, "live", None)
        connect = getattr(live_api, "connect", None) if live_api is not None else None
        if connect is None:
            raise RuntimeError("google-genai live API is unavailable in this version")

        cfg = self._build_live_connect_config(response_modalities, include_audio_response, voice_name)
        text_parts: list[str] = []
        audio_parts: list[dict[str, Any]] = []

        started = time.time()
        async with connect(model=model, config=cfg) as session:
            if prompt:
                if hasattr(session, "send_client_content"):
                    try:
                        await session.send_client_content(turns=prompt, turn_complete=False)
                    except TypeError:
                        await session.send_client_content(turns=prompt)
                elif hasattr(session, "send"):
                    await session.send(prompt)

            for chunk in audio_chunks:
                blob = self._make_blob(chunk, audio_mime_type)
                sent = False
                if hasattr(session, "send_realtime_input"):
                    try:
                        await session.send_realtime_input(audio=blob)
                        sent = True
                    except TypeError:
                        pass
                if not sent and hasattr(session, "send"):
                    try:
                        await session.send(input_audio=blob)
                    except TypeError:
                        await session.send(blob)

            if hasattr(session, "send_client_content"):
                try:
                    await session.send_client_content(turn_complete=True)
                except Exception:  # noqa: BLE001
                    pass

            deadline = time.monotonic() + max(1, int(max_receive_seconds))
            receiver = getattr(session, "receive", None)
            if receiver is None:
                raise RuntimeError("live session does not expose receive()")

            async for evt in receiver():
                plain_evt = self._to_plain(evt)
                found_text, found_audio, turn_complete = self._extract_text_and_audio(plain_evt)
                if found_text:
                    text_parts.extend(found_text)
                if found_audio:
                    audio_parts.extend(found_audio)
                if turn_complete or time.monotonic() >= deadline:
                    break

        elapsed_ms = int((time.time() - started) * 1000.0)
        text = "\n".join([t for t in text_parts if t.strip()]).strip()

        return {
            "model": model,
            "latency_ms": elapsed_ms,
            "response_modalities": response_modalities,
            "text": text,
            "audio": audio_parts,
        }

    def live_audio_turn(
        self,
        *,
        audio_chunks_b64: list[str],
        audio_mime_type: str,
        prompt: str,
        model: str | None,
        response_modalities: list[str] | None,
        include_audio_response: bool,
        voice_name: str,
        max_receive_seconds: int | None,
    ) -> dict[str, Any]:
        if not audio_chunks_b64:
            raise ValueError("audio_chunks_b64 is required for live turn")

        chunks = [base64.b64decode(item) for item in audio_chunks_b64 if item.strip()]
        if not chunks:
            raise ValueError("audio_chunks_b64 did not contain valid data")

        selected_model = (model or str(self._model_config["live_model"])).strip()
        modalities = response_modalities or list(self._model_config["live"]["response_modalities"])
        modalities = [str(m).upper() for m in modalities if str(m).strip()]
        if not modalities:
            modalities = ["TEXT"]

        timeout_sec = int(
            max_receive_seconds
            if max_receive_seconds is not None
            else self._model_config["live"]["max_receive_seconds"]
        )

        started = time.time()
        result = asyncio.run(
            self._live_audio_turn_async(
                audio_chunks=chunks,
                audio_mime_type=audio_mime_type,
                prompt=prompt.strip(),
                model=selected_model,
                response_modalities=modalities,
                include_audio_response=include_audio_response,
                voice_name=voice_name,
                max_receive_seconds=timeout_sec,
            )
        )
        elapsed_ms = int((time.time() - started) * 1000.0)

        self._append_history(
            {
                "kind": "live_audio_turn",
                "timestamp": time.time(),
                "model": selected_model,
                "modalities": modalities,
                "latency_ms": elapsed_ms,
                "chunk_count": len(chunks),
                "has_text": bool(result.get("text")),
                "audio_parts": len(result.get("audio", [])),
            }
        )
        self._emit_event(
            "live_audio_turn_ok",
            model=selected_model,
            latency_ms=elapsed_ms,
            chunk_count=len(chunks),
            has_text=bool(result.get("text")),
            audio_parts=len(result.get("audio", [])),
        )

        return result

    def _tool_ctx(self, channel: str, source: str) -> ToolRequestContext:
        channel_norm = channel.strip().lower() or "website"
        source_norm = source.strip() or "api"
        request_id = f"sys-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        return ToolRequestContext(channel=channel_norm, source=source_norm, request_id=request_id)

    def system_status(self, *, channel: str, source: str, scope: str = "all") -> dict[str, Any]:
        ctx = self._tool_ctx(channel=channel, source=source)
        tool_result = self._tool_runtime.execute_tool(
            name="get_system_status",
            arguments={"scope": scope},
            ctx=ctx,
            password_tokens={},
        )
        return {
            "ok": bool(tool_result.get("ok")),
            "channel": ctx.channel,
            "source": ctx.source,
            "scope": scope,
            "result": tool_result,
        }

    def system_info(self, *, channel: str, source: str) -> dict[str, Any]:
        ctx = self._tool_ctx(channel=channel, source=source)
        tool_result = self._tool_runtime.execute_tool(
            name="get_system_info",
            arguments={},
            ctx=ctx,
            password_tokens={},
        )
        return {
            "ok": bool(tool_result.get("ok")),
            "channel": ctx.channel,
            "source": ctx.source,
            "result": tool_result,
        }

    def system_history(
        self,
        *,
        channel: str,
        source: str,
        hours: float = 2.0,
        limit: int = 100,
    ) -> dict[str, Any]:
        ctx = self._tool_ctx(channel=channel, source=source)
        tool_result = self._tool_runtime.execute_tool(
            name="get_system_history",
            arguments={"hours": hours, "limit": limit},
            ctx=ctx,
            password_tokens={},
        )
        return {
            "ok": bool(tool_result.get("ok")),
            "channel": ctx.channel,
            "source": ctx.source,
            "hours": hours,
            "limit": limit,
            "result": tool_result,
        }

    def system_arm(
        self,
        *,
        channel: str,
        source: str,
        password: str = "",
        password_hash: str = "",
    ) -> dict[str, Any]:
        ctx = self._tool_ctx(channel=channel, source=source)
        tool_result = self._tool_runtime.execute_tool(
            name="arm_system",
            arguments={"password": password, "password_hash": password_hash},
            ctx=ctx,
            password_tokens={},
        )
        return {
            "ok": bool(tool_result.get("ok")),
            "channel": ctx.channel,
            "source": ctx.source,
            "result": tool_result,
        }

    def system_disarm(
        self,
        *,
        channel: str,
        source: str,
        password: str = "",
        password_hash: str = "",
    ) -> dict[str, Any]:
        ctx = self._tool_ctx(channel=channel, source=source)
        tool_result = self._tool_runtime.execute_tool(
            name="disarm_system",
            arguments={"password": password, "password_hash": password_hash},
            ctx=ctx,
            password_tokens={},
        )
        return {
            "ok": bool(tool_result.get("ok")),
            "channel": ctx.channel,
            "source": ctx.source,
            "result": tool_result,
        }

    def get_health(self) -> dict[str, Any]:
        with self._lock:
            history_count = len(self._history)
            events_count = len(self._events)

        system_status = self._tool_runtime.get_status_payload(channel="system", scope="all")

        return {
            "status": "ok" if not self._last_error else "degraded",
            "service_id": self.service_id,
            "listen": {"host": self.host, "port": self.port},
            "provider": "gemini",
            "model_config_path": self.model_config_path,
            "models": {
                "audio_text_model": self._model_config["audio_text_model"],
                "live_model": self._model_config["live_model"],
            },
            "api_key_configured": bool(self.api_key),
            "history_count": history_count,
            "events_count": events_count,
            "system_status": system_status,
            "uptime_sec": round(time.time() - self._started_at, 2),
            "last_error": self._last_error,
        }

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)[-limit:]

    def serve_forever(self) -> None:
        self._server = LLMHTTPServer((self.host, self.port), GeminiLLMRequestHandler, self)
        print(f"[llm] service_id={self.service_id} provider=gemini listening on http://{self.host}:{self.port}")
        try:
            self._server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


class LLMHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        service: GeminiAudioLLMService,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.service = service


class GeminiLLMRequestHandler(BaseHTTPRequestHandler):
    server: LLMHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[llm] {self.client_address[0]} - {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in {"/", "/index.html"}:
            self._serve_index()
            return
        if path == "/health":
            self._json(HTTPStatus.OK, self.server.service.get_health())
            return
        if path == "/events":
            q = parse_qs(parsed.query)
            limit = int(q.get("limit", ["200"])[0])
            self._json(HTTPStatus.OK, {"events": self.server.service.list_events(limit=limit)})
            return
        if path == "/history":
            q = parse_qs(parsed.query)
            limit = int(q.get("limit", ["100"])[0])
            self._json(HTTPStatus.OK, {"history": self.server.service.list_history(limit=limit)})
            return
        if path == "/model-config":
            self._json(HTTPStatus.OK, {"model_config": self.server.service._model_config})
            return
        if path == "/system/status":
            q = parse_qs(parsed.query)
            channel = str(q.get("channel", ["website"])[0])
            source = str(q.get("source", ["website_api"])[0])
            scope = str(q.get("scope", ["all"])[0])
            result = self.server.service.system_status(
                channel=channel,
                source=source,
                scope=scope,
            )
            self._json(HTTPStatus.OK, result)
            return
        if path == "/system/info":
            q = parse_qs(parsed.query)
            channel = str(q.get("channel", ["website"])[0])
            source = str(q.get("source", ["website_api"])[0])
            result = self.server.service.system_info(
                channel=channel,
                source=source,
            )
            self._json(HTTPStatus.OK, result)
            return
        if path == "/system/history":
            q = parse_qs(parsed.query)
            channel = str(q.get("channel", ["website"])[0])
            source = str(q.get("source", ["website_api"])[0])
            try:
                hours = float(q.get("hours", ["2"])[0])
                limit = int(q.get("limit", ["100"])[0])
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "hours and limit must be numeric"})
                return
            result = self.server.service.system_history(
                channel=channel,
                source=source,
                hours=hours,
                limit=limit,
            )
            self._json(HTTPStatus.OK, result)
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()

        try:
            if path == "/llm/transcribe":
                result = self.server.service.audio_turn(
                    audio_b64=str(body.get("audio_b64", "")),
                    audio_mime_type=str(
                        body.get(
                            "audio_mime_type",
                            self.server.service._model_config["input_audio_mime_type"],
                        )
                    ),
                    prompt=str(body.get("prompt", "")),
                    model=(str(body.get("model", "")).strip() or None),
                    response_modalities=self._parse_modalities(body.get("response_modalities")),
                    include_audio_response=bool(
                        body.get(
                            "include_audio_response",
                            self.server.service._model_config["audio_response"]["enabled"],
                        )
                    ),
                    voice_name=str(
                        body.get(
                            "voice_name",
                            self.server.service._model_config["audio_response"]["voice_name"],
                        )
                    ),
                )
                self._json(HTTPStatus.OK, {"ok": True, "result": result})
                return

            if path == "/system/arm":
                result = self.server.service.system_arm(
                    channel=str(body.get("channel", "website")),
                    source=str(body.get("source", "website_api")),
                    password=str(body.get("password", "")),
                )
                status = HTTPStatus.OK if bool(result.get("ok")) else HTTPStatus.BAD_REQUEST
                self._json(status, result)
                return

            if path == "/system/disarm":
                result = self.server.service.system_disarm(
                    channel=str(body.get("channel", "website")),
                    source=str(body.get("source", "website_api")),
                    password=str(body.get("password", "")),
                )
                status = HTTPStatus.OK if bool(result.get("ok")) else HTTPStatus.BAD_REQUEST
                self._json(status, result)
                return

            if path == "/llm/live":
                chunks = body.get("audio_chunks_b64")
                if chunks is None:
                    single = str(body.get("audio_b64", "")).strip()
                    chunks = [single] if single else []
                if not isinstance(chunks, list):
                    raise ValueError("audio_chunks_b64 must be an array")

                result = self.server.service.live_audio_turn(
                    audio_chunks_b64=[str(item) for item in chunks],
                    audio_mime_type=str(
                        body.get(
                            "audio_mime_type",
                            self.server.service._model_config["input_audio_mime_type"],
                        )
                    ),
                    prompt=str(body.get("prompt", "")),
                    model=(str(body.get("model", "")).strip() or None),
                    response_modalities=self._parse_modalities(body.get("response_modalities")),
                    include_audio_response=bool(
                        body.get(
                            "include_audio_response",
                            self.server.service._model_config["audio_response"]["enabled"],
                        )
                    ),
                    voice_name=str(
                        body.get(
                            "voice_name",
                            self.server.service._model_config["audio_response"]["voice_name"],
                        )
                    ),
                    max_receive_seconds=(
                        int(body["max_receive_seconds"])
                        if "max_receive_seconds" in body and body["max_receive_seconds"] is not None
                        else None
                    ),
                )
                self._json(HTTPStatus.OK, {"ok": True, "result": result})
                return

            if path == "/llm/assistant/text":
                text = str(body.get("text", ""))
                channel = str(body.get("channel", "website"))
                source = str(body.get("source", "website"))
                result = self.server.service.assistant_text_turn(
                    text=text,
                    channel=channel,
                    source=source,
                )
                self._json(HTTPStatus.OK, {"ok": True, "result": result})
                return

            if path == "/llm/assistant/audio":
                result = self.server.service.assistant_audio_turn(
                    audio_b64=str(body.get("audio_b64", "")),
                    audio_mime_type=str(
                        body.get(
                            "audio_mime_type",
                            self.server.service._model_config["input_audio_mime_type"],
                        )
                    ),
                    prompt=str(body.get("prompt", "")),
                    channel=str(body.get("channel", "local")),
                    source=str(body.get("source", "local_mic")),
                )
                self._json(HTTPStatus.OK, {"ok": True, "result": result})
                return

            if path == "/llm/assistant/live":
                chunks = body.get("audio_chunks_b64")
                if chunks is None:
                    single = str(body.get("audio_b64", "")).strip()
                    chunks = [single] if single else []
                if not isinstance(chunks, list):
                    raise ValueError("audio_chunks_b64 must be an array")

                result = self.server.service.assistant_live_turn(
                    audio_chunks_b64=[str(item) for item in chunks],
                    audio_mime_type=str(
                        body.get(
                            "audio_mime_type",
                            self.server.service._model_config["input_audio_mime_type"],
                        )
                    ),
                    prompt=str(body.get("prompt", "")),
                    channel=str(body.get("channel", "local")),
                    source=str(body.get("source", "local_mic_live")),
                    model=(str(body.get("model", "")).strip() or None),
                    max_receive_seconds=(
                        int(body["max_receive_seconds"])
                        if "max_receive_seconds" in body and body["max_receive_seconds"] is not None
                        else None
                    ),
                )
                self._json(HTTPStatus.OK, {"ok": True, "result": result})
                return

        except ValueError as exc:
            self.server.service._last_error = str(exc)
            self.server.service._emit_event("llm_error", error=str(exc), kind="bad_request")
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            self.server.service._last_error = str(exc)
            self.server.service._emit_event("llm_error", error=str(exc), kind="runtime")
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    @staticmethod
    def _parse_modalities(raw: Any) -> list[str] | None:
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise ValueError("response_modalities must be an array of strings")
        out = [str(item).upper() for item in raw if str(item).strip()]
        return out or None

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_index(self) -> None:
        svc = self.server.service
        body = f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Gemini LLM Service</title>
    <style>
      body {{ background:#0f1520; color:#dce7f5; font-family:Segoe UI, Arial, sans-serif; margin:0; padding:24px; }}
      .card {{ max-width:980px; margin:0 auto; background:#142138; border:1px solid #2e4767; border-radius:10px; padding:16px; }}
      pre {{ background:#0d1829; border:1px solid #2e4767; border-radius:8px; padding:12px; overflow:auto; max-height:340px; }}
      code {{ color:#9ecfff; }}
    </style>
  </head>
  <body>
    <div class=\"card\">
      <h1>Gemini LLM Service</h1>
      <p><code>service_id={svc.service_id}</code></p>
      <p>
        <a href=\"/health\">/health</a> |
        <a href=\"/model-config\">/model-config</a> |
        <a href=\"/events\">/events</a> |
        <a href=\"/history\">/history</a>
      </p>
      <pre>
POST /llm/assistant/text
{{
  \"text\": \"arm system with password 1234\",
  \"channel\": \"website\",
  \"source\": \"website_chat\"
}}

POST /llm/assistant/audio
{{
  \"audio_b64\": \"...\",
  \"audio_mime_type\": \"audio/wav\",
  \"channel\": \"local\",
  \"source\": \"local_mic\"
}}
      </pre>
    </div>
  </body>
</html>
"""
        raw = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run Gemini audio+text LLM service")
    ap.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8094, help="bind port (default: 8094)")
    ap.add_argument("--service-id", default="llm-main", help="logical service id")
    ap.add_argument("--api-key", default="", help="Gemini API key")
    ap.add_argument(
        "--model-config-path",
        default="main-server/llm/gemini_model_config.json",
        help="path to model config JSON",
    )
    ap.add_argument("--request-timeout-sec", type=float, default=45.0)
    ap.add_argument("--max-events", type=int, default=500)
    ap.add_argument("--max-history", type=int, default=200)
    ap.add_argument("--assistant-max-tool-rounds", type=int, default=4)
    ap.add_argument("--system-arm-password", default="")
    ap.add_argument("--tts-speak-url", default="http://127.0.0.1:8093/tts/speak")
    ap.add_argument("--disable-tts-for-actions", action="store_true")
    ap.add_argument("--tts-action-channels", default="local")
    ap.add_argument("--tts-timeout-sec", type=float, default=12.0)
    return ap


def main() -> None:
    args = _build_arg_parser().parse_args()

    service = GeminiAudioLLMService(
        host=args.host,
        port=args.port,
        service_id=args.service_id,
        api_key=args.api_key,
        model_config_path=args.model_config_path,
        request_timeout_sec=args.request_timeout_sec,
        max_events=args.max_events,
        max_history=args.max_history,
        assistant_max_tool_rounds=args.assistant_max_tool_rounds,
        system_arm_password=args.system_arm_password,
        tts_speak_url=args.tts_speak_url,
        enable_tts_for_actions=not args.disable_tts_for_actions,
        tts_action_channels=args.tts_action_channels,
        tts_timeout_sec=args.tts_timeout_sec,
        service_health_urls={},
    )
    service.serve_forever()


if __name__ == "__main__":
    main()
