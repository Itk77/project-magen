#!/usr/bin/env python3
"""Main web server for Magen.

Responsibilities:
- Serve website UI files
- Expose API endpoints for status, arm/disarm, chat, logs
- Bridge website chat to LLM assistant service
- Proxy local camera stream to website path (/video_feed)
- Provide websocket endpoint for live chat/log updates
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import subprocess
import threading
import sys
import traceback
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.request import urlopen

import aiohttp
from aiohttp import WSMsgType, web

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.append(str(_THIS_DIR))

from service_loader import build_service

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

try:
    import RPi.GPIO as GPIO
except ImportError:  # pragma: no cover
    GPIO = None


class MainServerService:
    PAGE_FILES = {
        "Home_page.html",
        "log.html",
        "chat.html",
        "video.html",
        "sensors.html",
        "about_us.html",
    }

    def _configure_logging(self) -> None:
        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=getattr(logging, self.log_level, logging.INFO),
                format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        self.logger.setLevel(getattr(logging, self.log_level, logging.INFO))

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        service_id: str = "main-server",
        website_root: str = "website-ui/site",
        llm_service_class: str = "",
        llm_service_config: dict[str, Any] | None = None,
        camera_stream_url: str = "http://127.0.0.1:8081/stream",
        camera_snapshot_url: str = "http://127.0.0.1:8081/snapshot.jpg",
        visual_stream_url: str = "http://127.0.0.1:8091/stream",
        video_source: str = "visual",
        video_proxy_fps: int = 5,
        state_dir: str = "logs",
        request_timeout_sec: float = 20.0,
        llm_request_timeout_sec: float = 45.0,
        llm_max_workers: int = 4,
        log_level: str = "INFO",
        verbose_requests: bool = True,
        max_chat_history: int = 200,
        max_log_entries: int = 2000,
        max_history_entries: int = 4000,
        health_urls: dict[str, str] | None = None,
        managed_services: list[dict[str, Any]] | None = None,
        mqtt_enabled: bool = True,
        mqtt_host: str = "localhost",
        mqtt_port: int = 8883,
        mqtt_username: str = "main_server",
        mqtt_password: str = "",
        mqtt_use_tls: bool = True,
        mqtt_cafile: str = "/etc/mosquitto/certs/ca.crt",
        mqtt_insecure_tls: bool = False,
        mqtt_client_id: str = "main-server",
        mqtt_sub_topics: str = "alarm/state,alarm/state/request,alarm/trigger,alarm/auth/request,alarm/sensor/status,alarm/lock/status,alarm/heartbeat/esp",
        mqtt_command_topic: str = "alarm/command",
        mqtt_qos: int = 1,
        mqtt_retain: bool = False,
        gpio_alarm_pin: int = 17,
        audio_io_base_url: str = "http://127.0.0.1:8092",
        audio_io_ws_url: str = "ws://127.0.0.1:8095",
        tts_speak_url: str = "http://127.0.0.1:8093/tts/speak",
        voice_loop_enabled: bool = True,
        voice_loop_poll_sec: float = 0.8,
        voice_loop_button_pin: int = 23,
        voice_loop_press_threshold_sec: float = 0.5,
        voice_loop_release_threshold_sec: float = 0.2,
        voice_loop_min_record_sec: float = 0.35,
        voice_loop_channel: str = "local",
        voice_loop_source: str = "main_server_voice",
        voice_loop_prompt: str = "Interpret this local voice command and use tools when needed.",
        voice_loop_busy_beep_enabled: bool = True,
        voice_loop_busy_beep_frequency_hz: float = 880.0,
        voice_loop_busy_beep_duration_ms: int = 140,
        voice_loop_busy_beep_interval_sec: float = 0.9,
        voice_loop_listen_beep_enabled: bool = True,
        voice_loop_listen_beep_frequency_hz: float = 640.0,
        voice_loop_listen_beep_duration_ms: int = 120,
        voice_loop_listen_beep_volume: float = 0.18,
    ) -> None:
        if port <= 0:
            raise ValueError("port must be positive")
        if request_timeout_sec <= 0:
            raise ValueError("request_timeout_sec must be positive")
        if llm_request_timeout_sec <= 0:
            raise ValueError("llm_request_timeout_sec must be positive")
        if llm_max_workers <= 0:
            raise ValueError("llm_max_workers must be positive")
        if max_chat_history <= 0:
            raise ValueError("max_chat_history must be positive")
        if max_log_entries <= 0:
            raise ValueError("max_log_entries must be positive")
        if mqtt_port <= 0:
            raise ValueError("mqtt_port must be positive")
        if mqtt_qos not in {0, 1, 2}:
            raise ValueError("mqtt_qos must be 0, 1, or 2")

        self.host = host
        self.port = int(port)
        self.service_id = service_id
        self.llm_service_class = llm_service_class.strip()
        self.llm_service_config = dict(llm_service_config or {})
        self.camera_stream_url = camera_stream_url
        self.camera_snapshot_url = camera_snapshot_url
        self.visual_stream_url = visual_stream_url
        self.video_source = video_source.strip().lower() or "camera"
        self.video_proxy_fps = max(1, int(video_proxy_fps))
        self.request_timeout_sec = float(request_timeout_sec)
        self.llm_request_timeout_sec = float(llm_request_timeout_sec)
        self.llm_max_workers = int(llm_max_workers)
        self.log_level = str(log_level).strip().upper() or "INFO"
        self.verbose_requests = bool(verbose_requests)

        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_jsonl_path = self.state_dir / "logs.jsonl"
        self.chat_jsonl_path = self.state_dir / "chat.jsonl"
        self.history_jsonl_path = self.state_dir / "history.jsonl"
        self.control_state_path = self.state_dir / "control_state.json"

        self.website_root = Path(website_root).expanduser().resolve()
        self.templates_root = self.website_root / "templates"
        self.static_roots = [self.website_root / "Static", self.website_root / "static"]

        self.health_urls = dict(health_urls or {})
        self.managed_services = list(managed_services or [])
        self._system_arm_password = str(self.llm_service_config.get("system_arm_password", "")).strip()

        self._chat_history: collections.deque[dict[str, Any]] = collections.deque(maxlen=max_chat_history)
        self._logs: collections.deque[dict[str, Any]] = collections.deque(maxlen=max_log_entries)
        self._history: collections.deque[dict[str, Any]] = collections.deque(maxlen=max_history_entries)
        self._log_counter = 0
        self._history_counter = 0

        self._started_at = 0.0
        self._last_error = ""

        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._http: aiohttp.ClientSession | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._llm_service: Any = None
        self._llm_call_lock = threading.Lock()
        self._llm_executor = ThreadPoolExecutor(
            max_workers=self.llm_max_workers,
            thread_name_prefix="magen-llm",
        )
        self.logger = logging.getLogger(f"magen.main.{self.service_id}")
        self._configure_logging()

        self.mqtt_enabled = bool(mqtt_enabled)
        self.mqtt_host = str(mqtt_host).strip()
        self.mqtt_port = int(mqtt_port)
        self.mqtt_username = str(mqtt_username).strip()
        self.mqtt_password = str(mqtt_password)
        self.mqtt_use_tls = bool(mqtt_use_tls)
        self.mqtt_cafile = str(mqtt_cafile).strip()
        self.mqtt_insecure_tls = bool(mqtt_insecure_tls)
        self.mqtt_client_id = (str(mqtt_client_id).strip() or self.service_id)
        self.mqtt_command_topic = str(mqtt_command_topic).strip() or "alarm/command"
        self.mqtt_qos = int(mqtt_qos)
        self.mqtt_retain = bool(mqtt_retain)
        self.gpio_alarm_pin = int(gpio_alarm_pin)
        self.mqtt_sub_topics = [
            topic.strip()
            for topic in str(mqtt_sub_topics).split(",")
            if topic.strip()
        ]
        self.audio_io_base_url = str(audio_io_base_url).rstrip("/")
        self.audio_io_ws_url = str(audio_io_ws_url).strip()
        self.tts_speak_url = str(tts_speak_url).strip()
        self.voice_loop_poll_sec = max(0.1, float(voice_loop_poll_sec))
        self.voice_loop_button_pin = int(voice_loop_button_pin)
        self.voice_loop_press_threshold_sec = max(0.05, float(voice_loop_press_threshold_sec))
        self.voice_loop_release_threshold_sec = max(0.02, float(voice_loop_release_threshold_sec))
        self.voice_loop_min_record_sec = max(0.0, float(voice_loop_min_record_sec))
        self.voice_loop_channel = str(voice_loop_channel).strip() or "local"
        self.voice_loop_source = str(voice_loop_source).strip() or "main_server_voice"
        self.voice_loop_prompt = str(voice_loop_prompt).strip()
        self.voice_loop_busy_beep_enabled = bool(voice_loop_busy_beep_enabled)
        self.voice_loop_busy_beep_frequency_hz = float(voice_loop_busy_beep_frequency_hz)
        self.voice_loop_busy_beep_duration_ms = max(40, int(voice_loop_busy_beep_duration_ms))
        self.voice_loop_busy_beep_interval_sec = max(0.05, float(voice_loop_busy_beep_interval_sec))
        self.voice_loop_listen_beep_enabled = bool(voice_loop_listen_beep_enabled)
        self.voice_loop_listen_beep_frequency_hz = float(voice_loop_listen_beep_frequency_hz)
        self.voice_loop_listen_beep_duration_ms = max(40, int(voice_loop_listen_beep_duration_ms))
        self.voice_loop_listen_beep_volume = max(0.01, float(voice_loop_listen_beep_volume))

        self._mqtt_client: Any = None
        self._mqtt_connected = False
        self._mqtt_last_error = ""
        self._mqtt_last_connect_unix = 0.0
        self._mqtt_last_disconnect_unix = 0.0
        self._mqtt_last_rx: dict[str, Any] = {}
        self._mqtt_last_tx: dict[str, Any] = {}
        self._mqtt_sensor_state: dict[str, Any] = {
            "alarm_state": "unknown",
            "desired_alarm_state": "deactivated",
            "last_trigger": "",
            "last_trigger_metadata": {},
            "last_auth_request": {},
            "sensors": {
                "pir": {"enabled": True, "active": False},
                "ldr": {"enabled": True, "value": None},
                "reed": {"enabled": True, "open": False},
                "visual": {"enabled": True, "online": False, "unit": "pi"},
            },
            "lock": {"active": False},
            "connectivity": {
                "esp_online": False,
                "esp_last_heartbeat_unix": 0.0,
                "main_last_heartbeat_unix": 0.0,
            },
            "updated_unix": 0.0,
        }
        self._mqtt_lock = threading.Lock()
        self._mqtt_loop: asyncio.AbstractEventLoop | None = None
        self._mqtt_event_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._mqtt_event_task: asyncio.Task[None] | None = None
        self._gpio_ready = False
        self._voice_button_ready = False
        self._voice_loop_enabled = bool(voice_loop_enabled)
        self._voice_loop_processing = False
        self._voice_loop_task: asyncio.Task[None] | None = None
        self._voice_button_pressed_since = 0.0
        self._voice_button_released_since = 0.0
        self._voice_button_recording = False
        self._voice_record_started_loop = 0.0
        self._voice_last_record_started_unix = 0.0
        self._voice_last_record_stopped_unix = 0.0
        self._voice_seen_event_ids: set[int] = set()
        self._voice_seen_segments: set[int] = set()
        self._voice_last_event_id = 0
        self._voice_last_segment_id = 0
        self._voice_last_assistant_text = ""
        self._voice_last_tool_calls: list[dict[str, Any]] = []
        self._voice_last_tool_results: list[dict[str, Any]] = []
        self._voice_dropped_segments = 0
        self._voice_last_error = ""
        self._voice_last_turn_unix = 0.0
        self._voice_last_listen_beep_unix = 0.0
        self._managed_processes: dict[str, subprocess.Popen[str]] = {}
        self._stream_hubs: dict[str, dict[str, Any]] = {}
        self._visual_alarm_task: asyncio.Task[None] | None = None
        self._main_heartbeat_task: asyncio.Task[None] | None = None

        self._init_alarm_gpio()
        self._init_voice_button_gpio()

        self._prepare_website_files()
        if not self.website_root.exists():
            raise FileNotFoundError(f"website_root does not exist: {self.website_root}")
        if not self.templates_root.exists():
            raise FileNotFoundError(f"templates directory does not exist: {self.templates_root}")
        if not self.llm_service_class:
            raise ValueError("llm_service_class must not be empty")

        self._load_persisted_state()
        self._load_control_state()
        self._set_alarm_gpio_state(str(self._mqtt_sensor_state.get("desired_alarm_state", "deactivated")))
        self._llm_service = build_service(self.llm_service_class, self.llm_service_config)
        if hasattr(self._llm_service, "set_system_status_provider"):
            self._llm_service.set_system_status_provider(self._llm_system_status_snapshot)

    def _init_alarm_gpio(self) -> None:
        if GPIO is None:
            self.logger.warning("RPi.GPIO not available; alarm GPIO output disabled")
            return
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(self.gpio_alarm_pin, GPIO.OUT)
            GPIO.output(self.gpio_alarm_pin, GPIO.HIGH)
            self._gpio_ready = True
            self.logger.info(
                "alarm gpio configured: pin=%d default=HIGH alarm=LOW",
                self.gpio_alarm_pin,
            )
        except Exception as exc:  # noqa: BLE001
            self._gpio_ready = False
            self.logger.warning("failed to initialize alarm GPIO pin %d: %s", self.gpio_alarm_pin, exc)

    def _set_alarm_gpio_state(self, alarm_state: str) -> None:
        if not self._gpio_ready or GPIO is None:
            return
        normalized = alarm_state.strip().lower()
        level = GPIO.LOW if normalized == "alarm" else GPIO.HIGH
        try:
            GPIO.setup(self.gpio_alarm_pin, GPIO.OUT)
            GPIO.output(self.gpio_alarm_pin, level)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            self.logger.warning(
                "failed to set alarm GPIO pin %d for state '%s': %s",
                self.gpio_alarm_pin,
                normalized,
                exc,
            )

    def _init_voice_button_gpio(self) -> None:
        if GPIO is None:
            self.logger.warning("RPi.GPIO not available; voice button disabled")
            return
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(self.voice_loop_button_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self._voice_button_ready = True
            self.logger.info(
                "voice push-to-talk button configured: pin=%d active=LOW",
                self.voice_loop_button_pin,
            )
        except Exception as exc:  # noqa: BLE001
            self._voice_button_ready = False
            self.logger.warning(
                "failed to initialize voice button GPIO pin %d: %s",
                self.voice_loop_button_pin,
                exc,
            )

    def _voice_button_pressed(self) -> bool:
        if not self._voice_button_ready or GPIO is None:
            return False
        try:
            return GPIO.input(self.voice_loop_button_pin) == GPIO.LOW
        except Exception as exc:  # noqa: BLE001
            self._voice_button_ready = False
            self._voice_last_error = str(exc)
            self.logger.warning("voice button GPIO read failed: %s", exc)
            return False

    def _prepare_website_files(self) -> None:
        self.website_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        out.append(item)
        except OSError:
            return []
        return out

    @staticmethod
    def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False))
                f.write("\n")
        except OSError:
            pass

    def _load_persisted_state(self) -> None:
        for entry in self._read_jsonl(self.logs_jsonl_path):
            self._logs.append(entry)
            try:
                self._log_counter = max(self._log_counter, int(entry.get("count", 0)))
            except Exception:  # noqa: BLE001
                pass

        for item in self._read_jsonl(self.chat_jsonl_path):
            self._chat_history.append(item)

        for evt in self._read_jsonl(self.history_jsonl_path):
            self._history.append(evt)
            try:
                self._history_counter = max(self._history_counter, int(evt.get("event_id", 0)))
            except Exception:  # noqa: BLE001
                pass

    def _load_control_state(self) -> None:
        if not self.control_state_path.exists():
            return
        try:
            with self.control_state_path.open("r", encoding="utf-8") as f:
                parsed = json.load(f)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(parsed, dict):
            return
        desired = str(parsed.get("desired_alarm_state", "")).strip().lower()
        if desired in {"deactivated", "armed", "alarm"}:
            self._mqtt_sensor_state["desired_alarm_state"] = desired
            self._mqtt_sensor_state["alarm_state"] = desired
        sensors = parsed.get("sensors")
        if isinstance(sensors, dict):
            for sensor_name in ("pir", "ldr", "reed", "visual"):
                sensor_payload = sensors.get(sensor_name)
                if isinstance(sensor_payload, dict):
                    current = self._mqtt_sensor_state["sensors"].setdefault(sensor_name, {})
                    current.update(sensor_payload)

    def _save_control_state(self) -> None:
        try:
            with self._mqtt_lock:
                payload = {
                    "desired_alarm_state": self._mqtt_sensor_state.get("desired_alarm_state", "deactivated"),
                    "sensors": self._mqtt_sensor_state.get("sensors", {}),
                    "updated_unix": dt.datetime.now().timestamp(),
                }
            with self.control_state_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError:
            return

    def _managed_service_env(self) -> dict[str, str]:
        env = os.environ.copy()
        venv_bin = str((Path(__file__).resolve().parent.parent / ".venv" / "bin").resolve())
        if Path(venv_bin).exists():
            env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
            env.setdefault("VIRTUAL_ENV", str(Path(venv_bin).parent))
        return env

    def _start_managed_services(self) -> None:
        env = self._managed_service_env()
        for spec in self.managed_services:
            if not isinstance(spec, dict) or not bool(spec.get("enabled", True)):
                continue
            name = str(spec.get("name", "")).strip()
            command = spec.get("command")
            cwd = str(spec.get("cwd", Path(__file__).resolve().parent.parent))
            if not name or not isinstance(command, list) or not command:
                self.logger.warning("skipping invalid managed service spec: %s", spec)
                continue
            health_url = str(spec.get("health_url", "")).strip()
            if health_url:
                try:
                    with urlopen(health_url, timeout=1.0) as resp:
                        if 200 <= int(resp.status) < 500:
                            self.logger.info("managed service %s already responds at %s", name, health_url)
                            continue
                except Exception:
                    pass
            if name in self._managed_processes and self._managed_processes[name].poll() is None:
                continue
            log_path = self.state_dir / f"{name}.log"
            log_file = log_path.open("a", encoding="utf-8")
            self.logger.info("starting managed service %s: %s", name, command)
            proc = subprocess.Popen(
                [str(part) for part in command],
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._managed_processes[name] = proc

    def _stop_managed_services(self) -> None:
        for name, proc in list(self._managed_processes.items()):
            if proc.poll() is None:
                self.logger.info("stopping managed service %s", name)
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        self._managed_processes.clear()

    def _record_history(self, event_type: str, *, source: str, details: dict[str, Any]) -> dict[str, Any]:
        self._history_counter += 1
        evt = {
            "event_id": self._history_counter,
            "timestamp_unix": dt.datetime.now().timestamp(),
            "event_type": event_type,
            "source": source,
            "details": details,
        }
        self._history.append(evt)
        self._append_jsonl(self.history_jsonl_path, evt)
        return evt

    def _append_chat_message(self, sender: str, text: str, *, source: str) -> dict[str, Any]:
        item = {
            "sender": sender,
            "text": text,
            "source": source,
            "timestamp_unix": dt.datetime.now().timestamp(),
        }
        self._chat_history.append(item)
        self._append_jsonl(self.chat_jsonl_path, item)
        return item

    def _log(self, message: str, *, source: str = "main") -> dict[str, Any]:
        now = dt.datetime.now()
        self._log_counter += 1
        entry = {
            "count": self._log_counter,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "message": message,
            "source": source,
            "timestamp_unix": now.timestamp(),
        }
        self._logs.append(entry)
        self._append_jsonl(self.logs_jsonl_path, entry)
        self.logger.info("[%s] %s", source, message)
        return entry

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if not self._ws_clients:
            return
        dead: list[web.WebSocketResponse] = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    @staticmethod
    def _tool_results_already_spoke(tool_results: Any) -> bool:
        if not isinstance(tool_results, list):
            return False
        for item in tool_results:
            if not isinstance(item, dict):
                continue
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            tts = result.get("tts")
            if isinstance(tts, dict) and bool(tts.get("ok", False)):
                return True
        return False

    @staticmethod
    def _suggest_tts_voice(text: str) -> str | None:
        if any("\u0590" <= ch <= "\u05FF" for ch in text):
            return "he-IL-HilaNeural"
        return None

    async def _publish_log(self, message: str, *, source: str = "main") -> dict[str, Any]:
        entry = self._log(message, source=source)
        await self._broadcast({"type": "new_log_entry", "message": entry["message"], "entry": entry})
        return entry

    def _voice_loop_status(self) -> dict[str, Any]:
        return {
            "enabled": self._voice_loop_enabled,
            "processing": self._voice_loop_processing,
            "poll_sec": self.voice_loop_poll_sec,
            "button_pin": self.voice_loop_button_pin,
            "button_ready": self._voice_button_ready,
            "button_pressed": self._voice_button_pressed(),
            "recording": self._voice_button_recording,
            "press_threshold_sec": self.voice_loop_press_threshold_sec,
            "release_threshold_sec": self.voice_loop_release_threshold_sec,
            "min_record_sec": self.voice_loop_min_record_sec,
            "channel": self.voice_loop_channel,
            "source": self.voice_loop_source,
            "audio_io_base_url": self.audio_io_base_url,
            "tts_speak_url": self.tts_speak_url,
            "last_event_id": self._voice_last_event_id,
            "last_segment_id": self._voice_last_segment_id,
            "last_assistant_text": self._voice_last_assistant_text,
            "last_tool_calls": list(self._voice_last_tool_calls),
            "last_tool_results": list(self._voice_last_tool_results),
            "dropped_segments": self._voice_dropped_segments,
            "last_record_started_unix": self._voice_last_record_started_unix,
            "last_record_stopped_unix": self._voice_last_record_stopped_unix,
            "last_turn_unix": self._voice_last_turn_unix,
            "last_error": self._voice_last_error,
        }

    async def _broadcast_voice_status(self) -> None:
        await self._broadcast({"type": "voice_loop_status", "voice_loop": self._voice_loop_status()})

    async def _set_voice_loop_enabled(self, enabled: bool, *, source: str) -> dict[str, Any]:
        changed = self._voice_loop_enabled != bool(enabled)
        self._voice_loop_enabled = bool(enabled)
        if changed:
            state_word = "enabled" if self._voice_loop_enabled else "disabled"
            self._record_history(
                "voice_loop_toggled",
                source=source,
                details={"enabled": self._voice_loop_enabled},
            )
            await self._publish_log(f"voice loop {state_word}", source="voice")
            await self._broadcast_voice_status()
        return {"ok": True, "voice_loop": self._voice_loop_status(), "changed": changed}

    def _mqtt_status(self) -> dict[str, Any]:
        now = dt.datetime.now().timestamp()
        with self._mqtt_lock:
            connectivity = self._mqtt_sensor_state.setdefault("connectivity", {})
            last_esp = float(connectivity.get("esp_last_heartbeat_unix", 0.0) or 0.0)
            connectivity["esp_online"] = last_esp > 0 and (now - last_esp) <= 15.0
            return {
                "enabled": self.mqtt_enabled,
                "available": mqtt is not None,
                "connected": self._mqtt_connected,
                "host": self.mqtt_host,
                "port": self.mqtt_port,
                "use_tls": self.mqtt_use_tls,
                "command_topic": self.mqtt_command_topic,
                "sub_topics": list(self.mqtt_sub_topics),
                "qos": self.mqtt_qos,
                "retain": self.mqtt_retain,
                "last_connect_unix": self._mqtt_last_connect_unix,
                "last_disconnect_unix": self._mqtt_last_disconnect_unix,
                "last_rx": dict(self._mqtt_last_rx),
                "last_tx": dict(self._mqtt_last_tx),
                "sensor_state": dict(self._mqtt_sensor_state),
                "last_error": self._mqtt_last_error,
            }

    def _connectivity_status(self) -> dict[str, Any]:
        now = dt.datetime.now().timestamp()
        with self._mqtt_lock:
            connectivity = self._mqtt_sensor_state.setdefault("connectivity", {})
            last_esp = float(connectivity.get("esp_last_heartbeat_unix", 0.0) or 0.0)
            esp_online = last_esp > 0 and (now - last_esp) <= 15.0
            connectivity["esp_online"] = esp_online
            return {
                "esp_online": esp_online,
                "esp_last_heartbeat_unix": last_esp,
                "esp_last_seen_age_sec": round(now - last_esp, 1) if last_esp else None,
                "main_mqtt_connected": self._mqtt_connected,
                "main_last_heartbeat_unix": float(connectivity.get("main_last_heartbeat_unix", 0.0) or 0.0),
            }

    def _llm_system_status_snapshot(self, _channel: str, _scope: str) -> dict[str, Any]:
        status = self._mqtt_status()
        sensor_state = json.loads(json.dumps(status["sensor_state"], default=str))
        self._refresh_visual_status_sync(sensor_state)
        desired = str(sensor_state.get("desired_alarm_state", "")).strip().lower()
        mqtt_state = str(sensor_state.get("alarm_state", "")).strip().lower()
        effective = desired if desired and desired != "unknown" else mqtt_state
        sensor_state["effective_alarm_state"] = effective or "unknown"
        sensor_state["alarm_active"] = effective == "alarm"
        sensor_state["armed"] = effective in {"armed", "alarm"}
        sensor_state["connectivity"] = self._connectivity_status()
        sensor_state["mqtt"] = {
            "enabled": status.get("enabled"),
            "available": status.get("available"),
            "connected": status.get("connected"),
            "last_error": status.get("last_error"),
        }
        return sensor_state

    def _refresh_visual_status_sync(self, sensor_state: dict[str, Any], *, timeout_sec: float = 0.8) -> None:
        sensors = sensor_state.setdefault("sensors", {})
        visual = sensors.setdefault("visual", {"enabled": True, "unit": "pi"})
        visual["unit"] = "pi"
        visual_base = self.visual_stream_url.rsplit("/", 1)[0]
        now = dt.datetime.now().timestamp()
        last_check = float(visual.get("health_checked_unix", 0.0) or 0.0)
        if last_check > 0 and now - last_check < 2.0:
            return

        try:
            with urlopen(visual_base + "/health", timeout=timeout_sec) as resp:
                status = int(getattr(resp, "status", 200))
                raw = resp.read()
            health: dict[str, Any] = {}
            if raw:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    if isinstance(parsed, dict):
                        health = parsed
                except Exception:
                    health = {}
            visual["online"] = 200 <= status < 400
            visual["health_status"] = str(health.get("status", "unknown"))
            visual["health_checked_unix"] = now
            visual.pop("error", None)
        except Exception as exc:  # noqa: BLE001
            visual["online"] = False
            visual["health_status"] = "unavailable"
            visual["health_checked_unix"] = now
            visual["error"] = str(exc)

        with self._mqtt_lock:
            cached_visual = self._mqtt_sensor_state["sensors"].setdefault("visual", {"unit": "pi"})
            cached_visual.update(visual)
            self._mqtt_sensor_state["updated_unix"] = now

    @staticmethod
    def _mqtt_reason_code_to_int(reason_code: Any) -> int:
        try:
            return int(reason_code)
        except Exception:  # noqa: BLE001
            value = getattr(reason_code, "value", None)
            try:
                return int(value)
            except Exception:  # noqa: BLE001
                return -1

    def _enqueue_mqtt_event(self, event: dict[str, Any]) -> None:
        loop = self._mqtt_loop
        q = self._mqtt_event_queue
        if loop is None or q is None:
            return
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(q.put_nowait, event)
        except Exception:  # noqa: BLE001
            return

    def _on_mqtt_connect(  # noqa: PLR0913
        self,
        client: Any,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        rc = self._mqtt_reason_code_to_int(reason_code)
        now = dt.datetime.now().timestamp()
        with self._mqtt_lock:
            self._mqtt_connected = rc == 0
            self._mqtt_last_connect_unix = now if rc == 0 else self._mqtt_last_connect_unix
            self._mqtt_last_error = "" if rc == 0 else f"connect failed rc={rc}"

        if rc == 0:
            subs: list[str] = []
            for topic in self.mqtt_sub_topics:
                try:
                    client.subscribe(topic, qos=self.mqtt_qos)
                    subs.append(topic)
                except Exception as exc:  # noqa: BLE001
                    with self._mqtt_lock:
                        self._mqtt_last_error = f"subscribe failed for {topic}: {exc}"
            self._enqueue_mqtt_event(
                {
                    "type": "mqtt_connected",
                    "timestamp_unix": now,
                    "subscriptions": subs,
                }
            )
            return

        self._enqueue_mqtt_event(
            {
                "type": "mqtt_connect_error",
                "timestamp_unix": now,
                "rc": rc,
            }
        )

    def _on_mqtt_disconnect(
        self,
        _client: Any,
        _userdata: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        rc = self._mqtt_reason_code_to_int(reason_code)
        now = dt.datetime.now().timestamp()
        with self._mqtt_lock:
            self._mqtt_connected = False
            self._mqtt_last_disconnect_unix = now
            if rc != 0:
                self._mqtt_last_error = f"disconnect rc={rc}"

        self._enqueue_mqtt_event(
            {
                "type": "mqtt_disconnected",
                "timestamp_unix": now,
                "rc": rc,
            }
        )

    def _on_mqtt_message(self, _client: Any, _userdata: Any, msg: Any) -> None:
        topic = str(getattr(msg, "topic", "")).strip()
        raw_payload = getattr(msg, "payload", b"")
        try:
            payload = bytes(raw_payload).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            payload = str(raw_payload)
        now = dt.datetime.now().timestamp()
        with self._mqtt_lock:
            self._mqtt_last_rx = {
                "topic": topic,
                "payload": payload,
                "timestamp_unix": now,
            }
        self._enqueue_mqtt_event(
            {
                "type": "mqtt_rx",
                "timestamp_unix": now,
                "topic": topic,
                "payload": payload,
            }
        )

    def _start_mqtt_client(self) -> None:
        if not self.mqtt_enabled:
            return
        if mqtt is None:
            with self._mqtt_lock:
                self._mqtt_last_error = "paho-mqtt is not installed"
            return
        try:
            try:
                client = mqtt.Client(
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id=self.mqtt_client_id,
                    clean_session=True,
                    protocol=mqtt.MQTTv311,
                )
            except Exception:  # noqa: BLE001
                client = mqtt.Client(client_id=self.mqtt_client_id, clean_session=True, protocol=mqtt.MQTTv311)

            if self.mqtt_username:
                client.username_pw_set(self.mqtt_username, self.mqtt_password)

            if self.mqtt_use_tls:
                tls_kwargs: dict[str, Any] = {}
                if self.mqtt_cafile:
                    tls_kwargs["ca_certs"] = self.mqtt_cafile
                client.tls_set(**tls_kwargs)
                client.tls_insecure_set(self.mqtt_insecure_tls)

            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.on_connect = self._on_mqtt_connect
            client.on_disconnect = self._on_mqtt_disconnect
            client.on_message = self._on_mqtt_message

            rc = client.connect_async(self.mqtt_host, self.mqtt_port, keepalive=30)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                with self._mqtt_lock:
                    self._mqtt_last_error = f"connect_async failed rc={rc}"
            client.loop_start()
            self._mqtt_client = client
        except Exception as exc:  # noqa: BLE001
            with self._mqtt_lock:
                self._mqtt_last_error = f"mqtt init failed: {exc}"
            self._mqtt_client = None

    def _stop_mqtt_client(self) -> None:
        client = self._mqtt_client
        self._mqtt_client = None
        if client is None:
            return
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def _mqtt_publish(self, *, topic: str, payload: str, source: str) -> dict[str, Any]:
        if not self.mqtt_enabled:
            return {"ok": False, "reason": "mqtt_disabled"}
        if mqtt is None:
            return {"ok": False, "reason": "mqtt_dependency_missing"}
        client = self._mqtt_client
        if client is None:
            return {"ok": False, "reason": "mqtt_client_not_initialized"}

        try:
            info = client.publish(topic, payload, qos=self.mqtt_qos, retain=self.mqtt_retain)
            rc = int(getattr(info, "rc", -1))
        except Exception as exc:  # noqa: BLE001
            with self._mqtt_lock:
                self._mqtt_last_error = str(exc)
            return {"ok": False, "reason": f"publish_exception: {exc}"}

        ok = rc == mqtt.MQTT_ERR_SUCCESS
        now = dt.datetime.now().timestamp()
        with self._mqtt_lock:
            self._mqtt_last_tx = {
                "topic": topic,
                "payload": payload,
                "timestamp_unix": now,
                "ok": ok,
            }
            if not ok:
                self._mqtt_last_error = f"publish failed rc={rc}"

        if not topic.startswith("alarm/heartbeat/"):
            self._record_history(
                "mqtt_tx",
                source=source,
                details={"topic": topic, "payload": payload, "ok": ok, "rc": rc},
            )
            await self._publish_log(
                f"mqtt tx {topic}: {payload} (ok={ok} rc={rc})",
                source="mqtt",
            )
        if not ok:
            await self._publish_log(f"mqtt publish failed rc={rc} topic={topic}", source="mqtt")
        return {"ok": ok, "topic": topic, "payload": payload, "rc": rc}

    async def _sync_llm_state_from_sensor(self, alarm_state: str) -> None:
        if not self._system_arm_password:
            return
        normalized = alarm_state.strip().lower()
        try:
            if normalized in {"armed", "alarm"}:
                await self._call_llm_async(
                    "system_arm",
                    {
                        "channel": "system",
                        "source": "mqtt_sync",
                        "password_hash": self._system_arm_password,
                    },
                )
            elif normalized == "deactivated":
                await self._call_llm_async(
                    "system_disarm",
                    {
                        "channel": "system",
                        "source": "mqtt_sync",
                        "password_hash": self._system_arm_password,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            await self._publish_log(f"mqtt->llm state sync failed: {exc}", source="mqtt")

    async def _handle_mqtt_auth_request(self, *, payload: str, parsed: dict[str, Any]) -> None:
        raw_id = parsed.get("id")
        raw_hash = str(parsed.get("hash", "")).strip().lower()

        try:
            request_id = int(raw_id)
        except Exception:  # noqa: BLE001
            request_id = -1

        valid_hash = len(raw_hash) == 64 and all(ch in "0123456789abcdef" for ch in raw_hash)
        if request_id < 0 or not valid_hash:
            self._record_history(
                "mqtt_auth_request_invalid",
                source="mqtt",
                details={"payload": payload, "parsed": parsed},
            )
            await self._publish_log(
                f"mqtt auth request invalid payload: {payload}",
                source="mqtt",
            )
            return

        result = "OK" if hmac.compare_digest(raw_hash, self._system_arm_password) else "FAIL"
        response_payload = json.dumps({"id": request_id, "result": result}, separators=(",", ":"))

        tx = await self._mqtt_publish(
            topic="alarm/auth/response",
            payload=response_payload,
            source="mqtt_auth",
        )
        self._record_history(
            "mqtt_auth_response",
            source="mqtt",
            details={
                "request_id": request_id,
                "result": result,
                "published": bool(tx.get("ok")),
            },
        )
        await self._publish_log(
            f"mqtt auth response id={request_id} result={result}",
            source="mqtt",
        )

    def _set_desired_alarm_state(self, state: str) -> None:
        normalized = state.strip().lower()
        if normalized not in {"deactivated", "armed", "alarm"}:
            raise ValueError(f"invalid desired alarm state: {state}")
        with self._mqtt_lock:
            self._mqtt_sensor_state["desired_alarm_state"] = normalized
            self._mqtt_sensor_state["updated_unix"] = dt.datetime.now().timestamp()
            if normalized == "alarm":
                self._mqtt_sensor_state["lock"] = {"active": True}
        self._save_control_state()
        self._set_alarm_gpio_state(normalized)

    def _desired_alarm_command(self) -> str:
        with self._mqtt_lock:
            desired = str(self._mqtt_sensor_state.get("desired_alarm_state", "deactivated")).strip().lower()
        if desired == "alarm":
            return "ALARM"
        if desired == "armed":
            return "ARM"
        return "DISARM"

    def _desired_sensor_commands(self) -> list[str]:
        with self._mqtt_lock:
            sensors = dict(self._mqtt_sensor_state.get("sensors", {}))
        commands: list[str] = []
        for key, name in (("pir", "PIR"), ("ldr", "LDR"), ("reed", "REED")):
            cfg = sensors.get(key, {})
            enabled = True if not isinstance(cfg, dict) else bool(cfg.get("enabled", True))
            commands.append(f"{name} {'ON' if enabled else 'OFF'}")
        return commands

    async def _publish_desired_state_to_esp(self, *, source: str) -> dict[str, Any]:
        command = self._desired_alarm_command()
        tx = await self._mqtt_publish(topic=self.mqtt_command_topic, payload=command, source=source)
        sensor_txs: list[dict[str, Any]] = []
        for sensor_command in self._desired_sensor_commands():
            sensor_txs.append(
                await self._mqtt_publish(topic="alarm/sensor", payload=sensor_command, source=f"{source}_sensor_sync")
            )
        return {"command": command, "state_tx": tx, "sensor_txs": sensor_txs}

    async def _publish_main_heartbeat(self) -> dict[str, Any]:
        now = dt.datetime.now().timestamp()
        with self._mqtt_lock:
            desired = str(self._mqtt_sensor_state.get("desired_alarm_state", "deactivated"))
            connectivity = self._mqtt_sensor_state.setdefault("connectivity", {})
            connectivity["main_last_heartbeat_unix"] = now
        payload = json.dumps(
            {
                "device": "main-server",
                "desired_alarm_state": desired,
                "timestamp_unix": now,
            },
            separators=(",", ":"),
        )
        return await self._mqtt_publish(topic="alarm/heartbeat/main", payload=payload, source="main_heartbeat")

    async def _main_heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            await self._publish_main_heartbeat()

    async def _handle_mqtt_event(self, event: dict[str, Any]) -> None:
        etype = str(event.get("type", ""))
        if etype == "mqtt_connected":
            subs = event.get("subscriptions", [])
            await self._publish_log(
                f"mqtt connected to {self.mqtt_host}:{self.mqtt_port} subs={subs}",
                source="mqtt",
            )
            self._record_history(
                "mqtt_connected",
                source="mqtt",
                details={"host": self.mqtt_host, "port": self.mqtt_port, "subscriptions": subs},
            )
            sync = await self._publish_desired_state_to_esp(source="mqtt_pi_boot_sync")
            self._record_history(
                "mqtt_pi_boot_sync",
                source="mqtt",
                details={
                    "command": sync.get("command"),
                    "published": bool(sync.get("state_tx", {}).get("ok")),
                    "sensor_txs": sync.get("sensor_txs", []),
                },
            )
            return

        if etype == "mqtt_connect_error":
            await self._publish_log(
                f"mqtt connect failed rc={event.get('rc')}",
                source="mqtt",
            )
            self._record_history("mqtt_connect_error", source="mqtt", details={"rc": event.get("rc")})
            return

        if etype == "mqtt_disconnected":
            await self._publish_log(
                f"mqtt disconnected rc={event.get('rc')}",
                source="mqtt",
            )
            self._record_history("mqtt_disconnected", source="mqtt", details={"rc": event.get("rc")})
            return

        if etype != "mqtt_rx":
            return

        topic = str(event.get("topic", "")).strip()
        payload = str(event.get("payload", ""))
        now = float(event.get("timestamp_unix", dt.datetime.now().timestamp()))
        is_heartbeat = topic.startswith("alarm/heartbeat/")
        if not is_heartbeat:
            await self._publish_log(f"mqtt rx {topic}: {payload}", source="mqtt")
            self._record_history("mqtt_rx", source="mqtt", details={"topic": topic, "payload": payload})
        if not is_heartbeat:
            await self._broadcast({"type": "mqtt_rx", "topic": topic, "payload": payload, "timestamp_unix": now})

        normalized_topic = topic.strip().lower()
        if normalized_topic == "alarm/heartbeat/esp":
            parsed: dict[str, Any] = {}
            try:
                maybe = json.loads(payload)
                if isinstance(maybe, dict):
                    parsed = maybe
            except json.JSONDecodeError:
                parsed = {}
            with self._mqtt_lock:
                connectivity = self._mqtt_sensor_state.setdefault("connectivity", {})
                connectivity["esp_online"] = True
                connectivity["esp_last_heartbeat_unix"] = now
                connectivity["esp_last_heartbeat"] = parsed or payload
                self._mqtt_sensor_state["updated_unix"] = now
            await self._broadcast(
                {
                    "type": "connectivity_status",
                    "connectivity": self._connectivity_status(),
                }
            )
            return

        if normalized_topic == "alarm/state/request":
            sync = await self._publish_desired_state_to_esp(source="mqtt_state_sync")
            self._record_history(
                "mqtt_state_sync_response",
                source="mqtt",
                details={
                    "requested_payload": payload,
                    "command": sync.get("command"),
                    "published": bool(sync.get("state_tx", {}).get("ok")),
                    "sensor_txs": sync.get("sensor_txs", []),
                },
            )
            return

        if normalized_topic == "alarm/state":
            alarm_state = payload.strip().lower()
            with self._mqtt_lock:
                self._mqtt_sensor_state["alarm_state"] = alarm_state
                self._mqtt_sensor_state["updated_unix"] = now
            if alarm_state in {"deactivated", "armed", "alarm"}:
                self._set_desired_alarm_state(alarm_state)
            self._set_alarm_gpio_state(alarm_state)
            await self._sync_llm_state_from_sensor(alarm_state)
            return

        if normalized_topic == "alarm/trigger":
            trigger_value = payload.strip()
            trigger_metadata: dict[str, Any] = {}
            try:
                maybe = json.loads(payload)
                if isinstance(maybe, dict):
                    trigger_metadata = maybe
                    trigger_value = str(maybe.get("sensor", trigger_value)).strip() or trigger_value
            except json.JSONDecodeError:
                trigger_metadata = {}
            with self._mqtt_lock:
                self._mqtt_sensor_state["last_trigger"] = trigger_value
                self._mqtt_sensor_state["last_trigger_metadata"] = trigger_metadata
                self._mqtt_sensor_state["updated_unix"] = now
            return

        if normalized_topic == "alarm/sensor/status":
            parsed: dict[str, Any] = {}
            try:
                maybe = json.loads(payload)
                if isinstance(maybe, dict):
                    parsed = maybe
            except json.JSONDecodeError:
                parsed = {}
            if parsed:
                with self._mqtt_lock:
                    for sensor_name in ("pir", "ldr", "reed"):
                        sensor_payload = parsed.get(sensor_name)
                        if isinstance(sensor_payload, dict):
                            current = self._mqtt_sensor_state["sensors"].setdefault(sensor_name, {})
                            for key, value in sensor_payload.items():
                                if key == "enabled":
                                    current.setdefault("enabled", bool(value))
                                    current["actual_enabled"] = bool(value)
                                else:
                                    current[key] = value
                    self._mqtt_sensor_state["alarm_state"] = str(
                        parsed.get("state", self._mqtt_sensor_state.get("alarm_state", "unknown"))
                    )
                    if "lock_active" in parsed:
                        self._mqtt_sensor_state["lock"] = {"active": bool(parsed.get("lock_active"))}
                    self._mqtt_sensor_state["updated_unix"] = now
            return

        if normalized_topic == "alarm/lock/status":
            parsed: dict[str, Any] = {}
            try:
                maybe = json.loads(payload)
                if isinstance(maybe, dict):
                    parsed = maybe
            except json.JSONDecodeError:
                parsed = {}
            with self._mqtt_lock:
                self._mqtt_sensor_state["lock"] = {
                    "active": bool(parsed.get("active")) if parsed else payload.strip().lower() in {"on", "active", "true", "1"},
                    "raw": parsed or payload,
                }
                self._mqtt_sensor_state["updated_unix"] = now
            return

        if normalized_topic == "alarm/auth/request":
            parsed: dict[str, Any] = {}
            try:
                maybe = json.loads(payload)
                if isinstance(maybe, dict):
                    parsed = maybe
            except json.JSONDecodeError:
                parsed = {}
            with self._mqtt_lock:
                self._mqtt_sensor_state["last_auth_request"] = {
                    "raw": payload,
                    "parsed": parsed,
                    "timestamp_unix": now,
                }
                self._mqtt_sensor_state["updated_unix"] = now
            await self._handle_mqtt_auth_request(payload=payload, parsed=parsed)

    async def _mqtt_event_loop(self) -> None:
        q = self._mqtt_event_queue
        if q is None:
            return
        while True:
            event = await q.get()
            await self._handle_mqtt_event(event)

    async def _handle_assistant_tool_actions(self, *, result: dict[str, Any], source: str) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        tool_results = result.get("tool_results", [])
        if not isinstance(tool_results, list):
            return actions

        for item in tool_results:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            tool_result = item.get("result")
            if not isinstance(tool_result, dict):
                continue
            if not bool(tool_result.get("ok")):
                continue
            if name == "set_sensor_enabled":
                sensor_action = tool_result.get("sensor_action")
                if not isinstance(sensor_action, dict):
                    continue
                sensor = str(sensor_action.get("sensor", "")).strip().upper()
                enabled = bool(sensor_action.get("enabled"))
                try:
                    update = await self._set_sensor_enabled(sensor=sensor, enabled=enabled, source=source)
                except Exception as exc:  # noqa: BLE001
                    update = {"ok": False, "error": str(exc)}
                actions.append(
                    {
                        "tool_name": name,
                        "sensor": sensor,
                        "enabled": enabled,
                        "update": update,
                    }
                )
                continue

            if name in {"arm_system", "disarm_system"}:
                armed = tool_result.get("armed")
                if isinstance(armed, bool):
                    cmd = "ARM" if armed else "DISARM"
                else:
                    cmd = "ARM" if name == "arm_system" else "DISARM"

                tx = await self._mqtt_publish(topic=self.mqtt_command_topic, payload=cmd, source=source)
                action = {
                    "tool_name": name,
                    "mqtt_command": cmd,
                    "mqtt": tx,
                }
                actions.append(action)

        if actions:
            result["mqtt_actions"] = actions
        return actions

    @staticmethod
    def _safe_rel_parts(raw_path: str) -> list[str] | None:
        parts = [p for p in PurePosixPath(raw_path).parts if p and p != "."]
        if any(p == ".." for p in parts):
            return None
        return parts

    @staticmethod
    def _resolve_case_insensitive(root: Path, parts: list[str]) -> Path | None:
        current = root
        for part in parts:
            next_path = current / part
            if next_path.exists():
                current = next_path
                continue

            if not current.exists() or not current.is_dir():
                return None

            lowered = part.lower()
            matched = None
            for child in current.iterdir():
                if child.name.lower() == lowered:
                    matched = child
                    break
            if matched is None:
                return None
            current = matched
        return current

    def _resolve_static_file(self, rel_path: str) -> Path | None:
        parts = self._safe_rel_parts(rel_path)
        if parts is None:
            return None
        for root in self.static_roots:
            found = self._resolve_case_insensitive(root, parts)
            if found and found.exists() and found.is_file():
                return found
        return None

    async def _json_request(
        self,
        *,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> tuple[int, dict[str, Any], str]:
        if self._http is None:
            raise RuntimeError("HTTP client is not initialized")

        timeout = aiohttp.ClientTimeout(total=float(timeout_sec or self.request_timeout_sec))
        try:
            async with self._http.request(method, url, json=payload, timeout=timeout) as resp:
                status = int(resp.status)
                raw = await resp.text()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"request failed: {exc}") from exc

        parsed: dict[str, Any]
        if raw.strip():
            try:
                body = json.loads(raw)
                parsed = body if isinstance(body, dict) else {"raw": body}
            except json.JSONDecodeError:
                parsed = {"raw": raw}
        else:
            parsed = {}

        return status, parsed, raw

    async def _bytes_request(
        self,
        *,
        method: str,
        url: str,
        timeout_sec: float | None = None,
    ) -> tuple[int, bytes]:
        if self._http is None:
            raise RuntimeError("HTTP client is not initialized")
        timeout = aiohttp.ClientTimeout(total=float(timeout_sec or self.request_timeout_sec))
        try:
            async with self._http.request(method, url, timeout=timeout) as resp:
                status = int(resp.status)
                body = await resp.read()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"request failed: {exc}") from exc
        return status, body

    async def _probe_url(self, name: str, url: str) -> dict[str, Any]:
        start = asyncio.get_running_loop().time()
        try:
            status, _, _ = await self._json_request(method="GET", url=url, timeout_sec=4.0)
            elapsed = int((asyncio.get_running_loop().time() - start) * 1000.0)
            return {
                "name": name,
                "url": url,
                "ok": 200 <= status < 400,
                "http_status": status,
                "latency_ms": elapsed,
            }
        except Exception as exc:  # noqa: BLE001
            elapsed = int((asyncio.get_running_loop().time() - start) * 1000.0)
            return {
                "name": name,
                "url": url,
                "ok": False,
                "latency_ms": elapsed,
                "error": str(exc),
            }

    async def _assistant_text(self, text: str, *, source: str = "website_chat") -> dict[str, Any]:
        if self._llm_service is None:
            raise RuntimeError("llm service is not initialized")
        result = await self._call_llm_async(
            "assistant_text_turn",
            {"text": text, "channel": "website", "source": source},
        )
        if not isinstance(result, dict):
            raise RuntimeError("invalid llm response shape")
        return result

    async def _assistant_audio(self, wav_bytes: bytes, *, source: str) -> dict[str, Any]:
        if not wav_bytes:
            raise RuntimeError("audio segment is empty")
        result = await self._call_llm_async(
            "assistant_audio_turn",
            {
                "audio_b64": base64.b64encode(wav_bytes).decode("ascii"),
                "audio_mime_type": "audio/wav",
                "prompt": self.voice_loop_prompt,
                "channel": self.voice_loop_channel,
                "source": source,
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("invalid llm audio response shape")
        return result

    def _call_llm_sync(self, method_name: str, kwargs: dict[str, Any]) -> Any:
        if self._llm_service is None:
            raise RuntimeError("llm service is not initialized")
        method = getattr(self._llm_service, method_name, None)
        if method is None:
            raise RuntimeError(f"llm service missing method: {method_name}")
        with self._llm_call_lock:
            return method(**kwargs)

    async def _call_llm_async(
        self,
        method_name: str,
        kwargs: dict[str, Any],
        timeout_sec: float | None = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(
            self._llm_executor,
            self._call_llm_sync,
            method_name,
            kwargs,
        )
        try:
            return await asyncio.wait_for(fut, timeout=timeout_sec or self.llm_request_timeout_sec)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"llm call timed out ({timeout_sec or self.llm_request_timeout_sec:.1f}s): {method_name}"
            ) from exc

    async def _system_status(self, *, channel: str, source: str, scope: str = "all") -> dict[str, Any]:
        if self._llm_service is None:
            raise RuntimeError("llm service is not initialized")
        result = await self._call_llm_async(
            "system_status",
            {"channel": channel, "source": source, "scope": scope},
        )
        if not isinstance(result, dict):
            raise RuntimeError("invalid status result")
        return result

    async def _system_info(self, *, channel: str, source: str) -> dict[str, Any]:
        if self._llm_service is None:
            raise RuntimeError("llm service is not initialized")
        result = await self._call_llm_async(
            "system_info",
            {"channel": channel, "source": source},
        )
        if not isinstance(result, dict):
            raise RuntimeError("invalid info result")
        return result

    async def _system_history(
        self,
        *,
        channel: str,
        source: str,
        hours: float,
        limit: int,
    ) -> dict[str, Any]:
        if self._llm_service is None:
            raise RuntimeError("llm service is not initialized")
        result = await self._call_llm_async(
            "system_history",
            {"channel": channel, "source": source, "hours": hours, "limit": limit},
        )
        if not isinstance(result, dict):
            raise RuntimeError("invalid history result")
        return result

    async def _system_arm(self, *, channel: str, source: str, password: str) -> dict[str, Any]:
        if self._llm_service is None:
            raise RuntimeError("llm service is not initialized")
        result = await self._call_llm_async(
            "system_arm",
            {"channel": channel, "source": source, "password": password},
        )
        if not isinstance(result, dict):
            raise RuntimeError("invalid arm result")
        return result

    async def _system_disarm(self, *, channel: str, source: str, password: str) -> dict[str, Any]:
        if self._llm_service is None:
            raise RuntimeError("llm service is not initialized")
        result = await self._call_llm_async(
            "system_disarm",
            {"channel": channel, "source": source, "password": password},
        )
        if not isinstance(result, dict):
            raise RuntimeError("invalid disarm result")
        return result

    async def _handle_health(self, _request: web.Request) -> web.Response:
        health_checks: list[dict[str, Any]] = []
        for name, url in self.health_urls.items():
            health_checks.append(await self._probe_url(name, url))
        llm_health: dict[str, Any] = {}
        if self._llm_service is not None and hasattr(self._llm_service, "get_health"):
            try:
                llm_health = await self._call_llm_async("get_health", {}, timeout_sec=5.0)
            except Exception as exc:  # noqa: BLE001
                llm_health = {"status": "error", "error": str(exc)}

        return web.json_response(
            {
                "status": "ok" if not self._last_error else "degraded",
                "service_id": self.service_id,
                "listen": {"host": self.host, "port": self.port},
                "website_root": str(self.website_root),
                "llm_in_process": True,
                "llm_service_class": self.llm_service_class,
                "video_source": self.video_source,
                "uptime_sec": round(asyncio.get_running_loop().time() - self._started_at, 2),
                "chat_history_count": len(self._chat_history),
                "log_count": len(self._logs),
                "history_count": len(self._history),
                "ws_clients": len(self._ws_clients),
                "service_health": health_checks,
                "llm_health": llm_health,
                "mqtt": self._mqtt_status(),
                "state_dir": str(self.state_dir),
                "last_error": self._last_error,
            }
        )

    async def _handle_root(self, _request: web.Request) -> web.Response:
        return web.HTTPFound("/Home_page.html")

    async def _handle_page(self, request: web.Request) -> web.StreamResponse:
        page = request.match_info.get("page", "")
        if page not in self.PAGE_FILES:
            raise web.HTTPNotFound(text="page not found")
        path = self.templates_root / page
        if not path.exists():
            raise web.HTTPNotFound(text=f"missing page file: {path}")
        return web.FileResponse(path, headers={"Cache-Control": "no-store, max-age=0"})

    async def _handle_template_alias(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info.get("name", "")
        if name not in self.PAGE_FILES:
            raise web.HTTPNotFound(text="template not found")
        path = self.templates_root / name
        if not path.exists():
            raise web.HTTPNotFound(text=f"missing page file: {path}")
        return web.FileResponse(path, headers={"Cache-Control": "no-store, max-age=0"})

    async def _handle_static(self, request: web.Request) -> web.StreamResponse:
        rel = request.match_info.get("path", "")
        file_path = self._resolve_static_file(rel)
        if file_path is None:
            raise web.HTTPNotFound(text="static file not found")
        headers = {"Cache-Control": "no-cache, max-age=0"}
        ctype, _ = mimetypes.guess_type(str(file_path))
        if ctype:
            headers["Content-Type"] = ctype
        return web.FileResponse(file_path, headers=headers)

    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        await ws.send_json({"type": "connected", "service_id": self.service_id})
        await self._publish_log("website websocket connected", source="website")
        self._record_history("ws_connected", source="website", details={"peer": request.remote})

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type == WSMsgType.ERROR:
                        break
                    continue

                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "invalid json"})
                    continue

                if not isinstance(payload, dict):
                    await ws.send_json({"type": "error", "message": "message must be an object"})
                    continue

                mtype = str(payload.get("type", "")).strip()
                if mtype == "ping":
                    await ws.send_json({"type": "pong"})
                    continue

                if mtype == "get_chat_history":
                    await ws.send_json({"type": "chat_history", "messages": list(self._chat_history)})
                    continue

                if mtype == "get_log_history":
                    await ws.send_json({"type": "log_history", "data": list(self._logs)})
                    continue

                if mtype == "chat_message":
                    text = str(payload.get("text", "")).strip()
                    if not text:
                        await ws.send_json({"type": "error", "message": "text must not be empty"})
                        continue
                    await self._handle_chat_turn(ws, text=text)
                    continue

                await ws.send_json({"type": "error", "message": f"unknown message type: {mtype}"})
        finally:
            self._ws_clients.discard(ws)
            await self._publish_log("website websocket disconnected", source="website")
            self._record_history("ws_disconnected", source="website", details={"peer": request.remote})

        return ws

    async def _handle_audio_ws(self, request: web.Request) -> web.StreamResponse:
        if self._http is None:
            raise web.HTTPInternalServerError(text="HTTP client is not initialized")
        if not self.audio_io_ws_url:
            raise web.HTTPBadGateway(text="audio websocket url is not configured")

        browser_ws = web.WebSocketResponse(heartbeat=20.0)
        await browser_ws.prepare(request)

        try:
            async with self._http.ws_connect(self.audio_io_ws_url, heartbeat=20.0) as audio_ws:
                async def browser_to_audio() -> None:
                    async for msg in browser_ws:
                        if msg.type == WSMsgType.TEXT:
                            await audio_ws.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await audio_ws.send_bytes(msg.data)
                        elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR}:
                            await audio_ws.close()
                            break

                async def audio_to_browser() -> None:
                    async for msg in audio_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await browser_ws.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await browser_ws.send_bytes(msg.data)
                        elif msg.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            await browser_ws.close()
                            break

                tasks = [
                    asyncio.create_task(browser_to_audio()),
                    asyncio.create_task(audio_to_browser()),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            if not browser_ws.closed:
                await browser_ws.close(message=str(exc).encode("utf-8", errors="ignore"))

        return browser_ws

    async def _handle_chat_turn(self, ws: web.WebSocketResponse, *, text: str) -> None:
        self._append_chat_message("You", text, source="website_ws")
        self._record_history("chat_user_message", source="website_ws", details={"text": text})
        await self._publish_log(f"user chat: {text}", source="chat")

        try:
            result = await self._assistant_text(text, source="website_ws_chat")
            mqtt_actions = await self._handle_assistant_tool_actions(result=result, source="assistant_ws")
            assistant_text = str(result.get("assistant_text", "")).strip() or "Done."
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            assistant_text = "Failed to contact assistant service."
            await self._publish_log(f"assistant error: {exc}", source="chat")
            await ws.send_json({"type": "reply", "text": assistant_text})
            return

        self._append_chat_message("Bot", assistant_text, source="website_ws")
        self._record_history(
            "chat_assistant_reply",
            source="website_ws",
            details={
                "assistant_text": assistant_text,
                "tool_calls": result.get("tool_calls", []),
                "mqtt_actions": mqtt_actions,
            },
        )
        await self._publish_log(f"assistant reply: {assistant_text}", source="chat")
        await ws.send_json({"type": "reply", "text": assistant_text})

    async def _tts_speak_text(self, text: str) -> dict[str, Any] | None:
        if not self.tts_speak_url or not text.strip():
            return None
        payload: dict[str, Any] = {"text": text}
        voice = self._suggest_tts_voice(text)
        if voice:
            payload["voice"] = voice
        status, parsed, raw = await self._json_request(
            method="POST",
            url=self.tts_speak_url,
            payload=payload,
            timeout_sec=60.0,
        )
        if status >= 400:
            raise RuntimeError(f"tts speak failed status={status} body={raw}")
        return parsed

    async def _audio_io_post(self, path: str, payload: dict[str, Any], *, timeout_sec: float) -> dict[str, Any]:
        status, parsed, raw = await self._json_request(
            method="POST",
            url=f"{self.audio_io_base_url}{path}",
            payload=payload,
            timeout_sec=timeout_sec,
        )
        if status >= 400:
            raise RuntimeError(f"audio-io {path} failed status={status} body={raw}")
        return parsed

    async def _audio_io_events(self) -> list[dict[str, Any]]:
        status, parsed, raw = await self._json_request(
            method="GET",
            url=f"{self.audio_io_base_url}/events",
            timeout_sec=5.0,
        )
        if status >= 400:
            raise RuntimeError(f"audio-io /events failed status={status} body={raw}")
        events = parsed.get("events", [])
        return events if isinstance(events, list) else []

    async def _audio_io_segment_wav(self, segment_id: int) -> bytes:
        status, body = await self._bytes_request(
            method="GET",
            url=f"{self.audio_io_base_url}/segments/{segment_id}.wav",
            timeout_sec=25.0,
        )
        if status >= 400:
            raise RuntimeError(f"audio-io segment {segment_id} fetch failed status={status}")
        return body

    async def _voice_busy_start(self) -> None:
        if not self.voice_loop_busy_beep_enabled:
            return
        await self._audio_io_post(
            "/speaker/busy/start",
            {
                "frequency_hz": self.voice_loop_busy_beep_frequency_hz,
                "duration_ms": self.voice_loop_busy_beep_duration_ms,
                "interval_sec": self.voice_loop_busy_beep_interval_sec,
                "volume": 0.25,
            },
            timeout_sec=6.0,
        )

    async def _voice_busy_stop(self) -> None:
        try:
            await self._audio_io_post("/speaker/busy/stop", {}, timeout_sec=5.0)
        except Exception:  # noqa: BLE001
            return

    async def _voice_listen_beep(self) -> None:
        if not self.voice_loop_listen_beep_enabled:
            return
        now = dt.datetime.now().timestamp()
        if (now - self._voice_last_listen_beep_unix) < 0.25:
            return
        self._voice_last_listen_beep_unix = now
        try:
            await self._audio_io_post(
                "/speaker/tone",
                {
                    "frequency_hz": self.voice_loop_listen_beep_frequency_hz,
                    "duration_ms": self.voice_loop_listen_beep_duration_ms,
                    "volume": self.voice_loop_listen_beep_volume,
                },
                timeout_sec=4.0,
            )
        except Exception:  # noqa: BLE001
            return

    async def _drop_voice_backlog(self) -> None:
        try:
            events = await self._audio_io_events()
        except Exception as exc:  # noqa: BLE001
            self._voice_last_error = str(exc)
            return
        dropped_this_turn = 0
        for evt in events:
            if not isinstance(evt, dict):
                continue
            evt_id = int(evt.get("id", 0))
            if evt_id > 0:
                self._voice_seen_event_ids.add(evt_id)
                self._voice_last_event_id = max(self._voice_last_event_id, evt_id)
            if str(evt.get("event", "")) != "segment_saved":
                continue
            seg_id = int(evt.get("segment_id", 0))
            if seg_id > 0 and seg_id not in self._voice_seen_segments:
                self._voice_seen_segments.add(seg_id)
                self._voice_dropped_segments += 1
                dropped_this_turn += 1
        if dropped_this_turn > 0:
            await self._publish_log(
                f"voice loop dropped {dropped_this_turn} queued segment(s) during assistant turn "
                f"(total dropped={self._voice_dropped_segments})",
                source="voice",
            )
            await self._broadcast_voice_status()

    async def _voice_record_start(self) -> None:
        if self._voice_button_recording:
            return
        await self._voice_listen_beep()
        await self._audio_io_post("/mic/record/start", {}, timeout_sec=6.0)
        loop = asyncio.get_running_loop()
        self._voice_button_recording = True
        self._voice_record_started_loop = loop.time()
        self._voice_last_record_started_unix = dt.datetime.now().timestamp()
        self._voice_last_error = ""
        self._record_history(
            "voice_recording_started",
            source=self.voice_loop_source,
            details={"button_pin": self.voice_loop_button_pin},
        )
        await self._publish_log("voice recording started", source="voice")
        await self._broadcast_voice_status()

    async def _voice_record_stop(self) -> None:
        if not self._voice_button_recording:
            return
        loop = asyncio.get_running_loop()
        recorded_sec = max(0.0, loop.time() - self._voice_record_started_loop)
        result = await self._audio_io_post("/mic/record/stop", {}, timeout_sec=10.0)
        self._voice_button_recording = False
        self._voice_record_started_loop = 0.0
        self._voice_last_record_stopped_unix = dt.datetime.now().timestamp()

        segment = result.get("segment") if isinstance(result, dict) else None
        if not isinstance(segment, dict):
            await self._publish_log("voice recording ignored: no audio segment captured", source="voice")
            await self._broadcast_voice_status()
            return

        segment_id = int(segment.get("segment_id", 0) or 0)
        duration_ms = int(segment.get("duration_ms", 0) or 0)
        duration_sec = max(recorded_sec, duration_ms / 1000.0)
        if segment_id <= 0 or duration_sec < self.voice_loop_min_record_sec:
            await self._publish_log(
                f"voice recording ignored: too short ({duration_sec:.2f}s)",
                source="voice",
            )
            await self._broadcast_voice_status()
            return

        self._voice_seen_segments.add(segment_id)
        await self._publish_log(f"voice recording stopped: segment {segment_id}", source="voice")
        await self._process_voice_segment(segment_id)

    async def _process_voice_segment(self, segment_id: int) -> None:
        self._voice_seen_segments.add(segment_id)
        self._voice_loop_processing = True
        self._voice_last_segment_id = segment_id
        self._voice_last_error = ""
        await self._broadcast_voice_status()

        try:
            wav_bytes = await self._audio_io_segment_wav(segment_id)
            await self._voice_busy_start()
            try:
                result = await self._assistant_audio(wav_bytes, source=self.voice_loop_source)
            finally:
                await self._voice_busy_stop()
            mqtt_actions = await self._handle_assistant_tool_actions(result=result, source="assistant_voice")

            assistant_text = str(result.get("assistant_text", "")).strip()
            self._voice_last_assistant_text = assistant_text
            tool_calls = result.get("tool_calls", [])
            tool_results = result.get("tool_results", [])
            self._voice_last_tool_calls = tool_calls if isinstance(tool_calls, list) else []
            self._voice_last_tool_results = tool_results if isinstance(tool_results, list) else []
            self._voice_last_turn_unix = dt.datetime.now().timestamp()

            if assistant_text and not self._tool_results_already_spoke(tool_results):
                try:
                    await self._tts_speak_text(assistant_text)
                except Exception as exc:  # noqa: BLE001
                    await self._publish_log(f"voice loop tts failed: {exc}", source="voice")

            self._append_chat_message("Voice User", "[voice request]", source="voice_loop")
            if assistant_text:
                self._append_chat_message("Voice Bot", assistant_text, source="voice_loop")
            self._record_history(
                "voice_assistant_turn",
                source=self.voice_loop_source,
                details={
                    "segment_id": segment_id,
                    "assistant_text": assistant_text,
                    "tool_calls": self._voice_last_tool_calls,
                    "mqtt_actions": mqtt_actions,
                },
            )
            await self._publish_log(
                f"voice segment {segment_id} processed"
                + (f": {assistant_text}" if assistant_text else ""),
                source="voice",
            )
            await self._broadcast(
                {
                    "type": "voice_loop_result",
                    "segment_id": segment_id,
                    "assistant_text": assistant_text,
                    "tool_calls": self._voice_last_tool_calls,
                    "tool_results": self._voice_last_tool_results,
                    "mqtt_actions": mqtt_actions,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self._voice_last_error = str(exc)
            self._record_history(
                "voice_assistant_error",
                source=self.voice_loop_source,
                details={"segment_id": segment_id, "error": str(exc)},
            )
            await self._publish_log(f"voice loop failed for segment {segment_id}: {exc}", source="voice")
        finally:
            self._voice_loop_processing = False
            await self._broadcast_voice_status()

    async def _voice_loop_run(self) -> None:
        poll_sec = min(self.voice_loop_poll_sec, 0.05)
        while True:
            try:
                if not self._voice_loop_enabled:
                    if self._voice_button_recording:
                        await self._voice_record_stop()
                    self._voice_button_pressed_since = 0.0
                    self._voice_button_released_since = 0.0
                    await asyncio.sleep(self.voice_loop_poll_sec)
                    continue

                if not self._voice_button_ready:
                    await asyncio.sleep(self.voice_loop_poll_sec)
                    continue

                now = asyncio.get_running_loop().time()
                pressed = self._voice_button_pressed()

                if pressed:
                    self._voice_button_released_since = 0.0
                    if self._voice_button_pressed_since <= 0.0:
                        self._voice_button_pressed_since = now
                    held_sec = now - self._voice_button_pressed_since
                    if (
                        not self._voice_button_recording
                        and not self._voice_loop_processing
                        and held_sec >= self.voice_loop_press_threshold_sec
                    ):
                        await self._voice_record_start()
                else:
                    self._voice_button_pressed_since = 0.0
                    if self._voice_button_recording:
                        if self._voice_button_released_since <= 0.0:
                            self._voice_button_released_since = now
                        released_sec = now - self._voice_button_released_since
                        if released_sec >= self.voice_loop_release_threshold_sec:
                            self._voice_button_released_since = 0.0
                            await self._voice_record_stop()
                    else:
                        self._voice_button_released_since = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._voice_button_recording = False
                self._voice_record_started_loop = 0.0
                self._voice_button_pressed_since = asyncio.get_running_loop().time()
                self._voice_last_error = str(exc)
                await self._publish_log(f"voice loop error: {exc}", source="voice")
                await self._broadcast_voice_status()

            await asyncio.sleep(poll_sec)

    async def _handle_api_logs(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "300"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        limit = max(1, min(limit, len(self._logs) or 1))
        return web.json_response({"logs": list(self._logs)[-limit:]})

    async def _handle_api_history(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "300"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        limit = max(1, min(limit, len(self._history) or 1))
        return web.json_response({"history": list(self._history)[-limit:]})

    async def _handle_api_chat_history(self, _request: web.Request) -> web.Response:
        return web.json_response({"messages": list(self._chat_history)})

    async def _handle_api_chat_send(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        text = str(body.get("text", "")).strip()
        if not text:
            raise web.HTTPBadRequest(text="text must not be empty")

        self._append_chat_message("You", text, source="website_api")
        self._record_history("chat_user_message", source="website_api", details={"text": text})
        await self._publish_log(f"user chat(api): {text}", source="chat")

        try:
            result = await self._assistant_text(text, source="website_api_chat")
            mqtt_actions = await self._handle_assistant_tool_actions(result=result, source="assistant_api")
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            await self._publish_log(f"assistant error(api): {exc}", source="chat")
            raise web.HTTPBadGateway(text=f"assistant call failed: {exc}") from exc

        assistant_text = str(result.get("assistant_text", "")).strip() or "Done."
        self._append_chat_message("Bot", assistant_text, source="website_api")
        self._record_history(
            "chat_assistant_reply",
            source="website_api",
            details={
                "assistant_text": assistant_text,
                "tool_calls": result.get("tool_calls", []),
                "mqtt_actions": mqtt_actions,
            },
        )
        await self._publish_log(f"assistant reply(api): {assistant_text}", source="chat")
        return web.json_response({"ok": True, "result": result})

    async def _handle_api_system_status(self, request: web.Request) -> web.Response:
        channel = str(request.query.get("channel", "website"))
        source = str(request.query.get("source", "website_api"))
        scope = str(request.query.get("scope", "all"))
        try:
            result = await self._system_status(channel=channel, source=source, scope=scope)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            raise web.HTTPBadGateway(text=f"status query failed: {exc}") from exc
        self._record_history(
            "system_status_query",
            source=source,
            details={"channel": channel, "scope": scope, "ok": bool(result.get("ok"))},
        )
        result["mqtt"] = self._mqtt_status()
        return web.json_response(result)

    async def _handle_api_system_info(self, request: web.Request) -> web.Response:
        channel = str(request.query.get("channel", "website"))
        source = str(request.query.get("source", "website_api"))
        try:
            result = await self._system_info(channel=channel, source=source)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            raise web.HTTPBadGateway(text=f"info query failed: {exc}") from exc
        self._record_history(
            "system_info_query",
            source=source,
            details={"channel": channel, "ok": bool(result.get("ok"))},
        )
        return web.json_response(result)

    async def _handle_api_system_history(self, request: web.Request) -> web.Response:
        channel = str(request.query.get("channel", "website"))
        source = str(request.query.get("source", "website_api"))
        hours_raw = str(request.query.get("hours", "2"))
        limit_raw = str(request.query.get("limit", "100"))
        try:
            hours = float(hours_raw)
            limit = int(limit_raw)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=f"invalid hours/limit: {exc}") from exc
        try:
            result = await self._system_history(
                channel=channel,
                source=source,
                hours=hours,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            raise web.HTTPBadGateway(text=f"history query failed: {exc}") from exc
        self._record_history(
            "system_history_query",
            source=source,
            details={"channel": channel, "hours": hours, "limit": limit, "ok": bool(result.get("ok"))},
        )
        return web.json_response(result)

    async def _handle_api_arm(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        password = str(body.get("password", ""))
        payload = {
            "password": password,
            "channel": str(body.get("channel", "website")),
            "source": str(body.get("source", "website_api")),
        }
        try:
            result = await self._system_arm(
                channel=str(payload["channel"]),
                source=str(payload["source"]),
                password=str(payload["password"]),
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            await self._publish_log(f"arm request failed: {exc}", source="api")
            raise web.HTTPBadGateway(text=f"arm request failed: {exc}") from exc

        msg = "arm request processed"
        if isinstance(result, dict):
            msg = str(result.get("result", {}).get("message", msg))
        if bool(result.get("ok")):
            self._set_desired_alarm_state("armed")
            mqtt_result = await self._mqtt_publish(
                topic=self.mqtt_command_topic,
                payload="ARM",
                source=str(payload["source"]),
            )
            result["mqtt"] = mqtt_result
        await self._publish_log(msg, source="api")
        self._record_history(
            "system_arm_request",
            source=str(payload["source"]),
            details={
                "channel": payload["channel"],
                "ok": bool(result.get("ok")),
                "message": msg,
                "mqtt": result.get("mqtt"),
            },
        )
        return web.json_response(result, status=200 if bool(result.get("ok")) else 400)

    async def _handle_api_disarm(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        password = str(body.get("password", ""))
        payload = {
            "password": password,
            "channel": str(body.get("channel", "website")),
            "source": str(body.get("source", "website_api")),
        }
        try:
            result = await self._system_disarm(
                channel=str(payload["channel"]),
                source=str(payload["source"]),
                password=str(payload["password"]),
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            await self._publish_log(f"disarm request failed: {exc}", source="api")
            raise web.HTTPBadGateway(text=f"disarm request failed: {exc}") from exc

        msg = "disarm request processed"
        if isinstance(result, dict):
            msg = str(result.get("result", {}).get("message", msg))
        if bool(result.get("ok")):
            self._set_desired_alarm_state("deactivated")
            mqtt_result = await self._mqtt_publish(
                topic=self.mqtt_command_topic,
                payload="DISARM",
                source=str(payload["source"]),
            )
            result["mqtt"] = mqtt_result
        await self._publish_log(msg, source="api")
        self._record_history(
            "system_disarm_request",
            source=str(payload["source"]),
            details={
                "channel": payload["channel"],
                "ok": bool(result.get("ok")),
                "message": msg,
                "mqtt": result.get("mqtt"),
            },
        )
        return web.json_response(result, status=200 if bool(result.get("ok")) else 400)

    async def _handle_api_alarm(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        password = str(body.get("password", ""))
        payload = {
            "password": password,
            "channel": str(body.get("channel", "website")),
            "source": str(body.get("source", "website_api")),
        }
        try:
            result = await self._system_arm(
                channel=str(payload["channel"]),
                source=str(payload["source"]),
                password=str(payload["password"]),
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            await self._publish_log(f"alarm activation failed: {exc}", source="api")
            raise web.HTTPBadGateway(text=f"alarm activation failed: {exc}") from exc

        msg = "alarm activation processed"
        if isinstance(result, dict):
            msg = str(result.get("result", {}).get("message", msg))
        if bool(result.get("ok")):
            msg = "Alarm activated."
            if isinstance(result.get("result"), dict):
                result["result"]["message"] = msg
            self._set_desired_alarm_state("alarm")
            with self._mqtt_lock:
                self._mqtt_sensor_state["last_trigger"] = "REMOTE"
                self._mqtt_sensor_state["last_trigger_metadata"] = {"sensor": "REMOTE", "source": payload["source"]}
            mqtt_result = await self._mqtt_publish(
                topic=self.mqtt_command_topic,
                payload="ALARM",
                source=str(payload["source"]),
            )
            result["mqtt"] = mqtt_result
        await self._publish_log(msg, source="api")
        self._record_history(
            "system_alarm_request",
            source=str(payload["source"]),
            details={
                "channel": payload["channel"],
                "ok": bool(result.get("ok")),
                "message": msg,
                "mqtt": result.get("mqtt"),
            },
        )
        return web.json_response(result, status=200 if bool(result.get("ok")) else 400)

    async def _handle_api_services_health(self, _request: web.Request) -> web.Response:
        checks: list[dict[str, Any]] = []
        for name, url in self.health_urls.items():
            checks.append(await self._probe_url(name, url))
        llm_health: dict[str, Any] | None = None
        if self._llm_service is not None and hasattr(self._llm_service, "get_health"):
            try:
                llm_health = await self._call_llm_async("get_health", {}, timeout_sec=5.0)
            except Exception as exc:  # noqa: BLE001
                llm_health = {"status": "error", "error": str(exc)}
        return web.json_response({"services": checks, "llm_in_process": llm_health, "mqtt": self._mqtt_status()})

    async def _handle_api_mqtt_status(self, _request: web.Request) -> web.Response:
        return web.json_response({"mqtt": self._mqtt_status()})

    async def _sensor_status_snapshot(self) -> dict[str, Any]:
        status = self._mqtt_status()
        sensor_state = status["sensor_state"]
        sensors = sensor_state.setdefault("sensors", {})
        visual = sensors.setdefault("visual", {"enabled": True, "unit": "pi"})
        visual["unit"] = "pi"
        visual_base = self.visual_stream_url.rsplit("/", 1)[0]
        try:
            health_status, health, _ = await self._json_request(
                method="GET",
                url=visual_base + "/health",
                timeout_sec=1.5,
            )
            visual["online"] = 200 <= health_status < 400
            visual["health_status"] = health.get("status", "unknown")
            visual["health_checked_unix"] = dt.datetime.now().timestamp()
            visual.pop("error", None)
        except Exception as exc:  # noqa: BLE001
            visual["online"] = False
            visual["health_status"] = "unavailable"
            visual["health_checked_unix"] = dt.datetime.now().timestamp()
            visual["error"] = str(exc)
        with self._mqtt_lock:
            cached_visual = self._mqtt_sensor_state["sensors"].setdefault("visual", {"unit": "pi"})
            cached_visual.update(visual)
            self._mqtt_sensor_state["updated_unix"] = dt.datetime.now().timestamp()
        sensor_state["connectivity"] = self._connectivity_status()
        return sensor_state

    async def _set_sensor_enabled(self, *, sensor: str, enabled: bool, source: str) -> dict[str, Any]:
        sensor = sensor.strip().upper()
        if sensor not in {"PIR", "LDR", "REED", "VISUAL"}:
            raise ValueError("sensor must be one of: PIR, LDR, REED, VISUAL")

        sensor_key = sensor.lower()
        with self._mqtt_lock:
            current = self._mqtt_sensor_state["sensors"].setdefault(sensor_key, {})
            current["enabled"] = bool(enabled)
            if sensor_key == "visual":
                current["unit"] = "pi"
                if not enabled:
                    current["active"] = False
                    current["human_count"] = 0
            self._mqtt_sensor_state["updated_unix"] = dt.datetime.now().timestamp()
            sensor_snapshot = dict(self._mqtt_sensor_state)
        self._save_control_state()

        payload = f"{sensor} {'ON' if enabled else 'OFF'}"
        if sensor == "VISUAL":
            tx = {"ok": True, "topic": "local", "payload": payload, "rc": 0}
        else:
            tx = await self._mqtt_publish(topic="alarm/sensor", payload=payload, source=source)
        await self._broadcast({"type": "sensor_status", "sensors": sensor_snapshot})
        return {"ok": bool(tx.get("ok")), "command": payload, "mqtt": tx, "sensors": sensor_snapshot}

    async def _handle_api_sensors_status(self, _request: web.Request) -> web.Response:
        tx: dict[str, Any] | None = None
        if str(_request.query.get("request", "")).strip() in {"1", "true", "yes"}:
            tx = await self._mqtt_publish(topic="alarm/sensor", payload="STATUS", source="website_sensors_status")
        sensor_state = await self._sensor_status_snapshot()
        return web.json_response({"ok": True, "sensors": sensor_state, "mqtt": self._mqtt_status(), "status_request": tx})

    async def _handle_api_sensor_update(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        sensor = str(body.get("sensor", "")).strip().upper()
        enabled = bool(body.get("enabled", False))
        if sensor not in {"PIR", "LDR", "REED", "VISUAL"}:
            raise web.HTTPBadRequest(text="sensor must be one of: PIR, LDR, REED, VISUAL")

        result = await self._set_sensor_enabled(sensor=sensor, enabled=enabled, source="website_sensors_update")
        return web.json_response(result)

    async def _handle_api_voice_status(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "voice_loop": self._voice_loop_status()})

    async def _handle_api_voice_start(self, _request: web.Request) -> web.Response:
        result = await self._set_voice_loop_enabled(True, source="website_voice_api")
        await self._voice_listen_beep()
        return web.json_response(result)

    async def _handle_api_voice_stop(self, _request: web.Request) -> web.Response:
        result = await self._set_voice_loop_enabled(False, source="website_voice_api")
        return web.json_response(result)

    async def _visual_alarm_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            with self._mqtt_lock:
                desired = str(self._mqtt_sensor_state.get("desired_alarm_state", "deactivated")).strip().lower()
                visual_cfg = self._mqtt_sensor_state.get("sensors", {}).get("visual", {})
                visual_enabled = True if not isinstance(visual_cfg, dict) else bool(visual_cfg.get("enabled", True))
            if desired != "armed":
                continue
            if not visual_enabled:
                continue
            try:
                status, parsed, _ = await self._json_request(
                    method="GET",
                    url=self.visual_stream_url.rsplit("/", 1)[0] + "/detections",
                    timeout_sec=2.5,
                )
            except Exception:
                continue
            if status >= 400:
                continue
            human_count = int(parsed.get("human_count", 0) or 0)
            with self._mqtt_lock:
                visual = self._mqtt_sensor_state["sensors"].setdefault("visual", {"unit": "pi"})
                visual["online"] = True
                visual["active"] = human_count > 0
                visual["human_count"] = human_count
                visual["updated_unix"] = dt.datetime.now().timestamp()
            if human_count <= 0:
                continue
            with self._mqtt_lock:
                current = str(self._mqtt_sensor_state.get("desired_alarm_state", "")).strip().lower()
                if current != "armed":
                    continue
                self._mqtt_sensor_state["last_trigger"] = "VISUAL"
                self._mqtt_sensor_state["last_trigger_metadata"] = {
                    "sensor": "VISUAL",
                    "human_count": human_count,
                    "detections": parsed.get("detections", []),
                }
            self._set_desired_alarm_state("alarm")
            await self._mqtt_publish(topic=self.mqtt_command_topic, payload="ALARM", source="visual_detection")
            await self._mqtt_publish(
                topic="alarm/trigger",
                payload=json.dumps(
                    {
                        "sensor": "VISUAL",
                        "state": "alarm",
                        "human_count": human_count,
                        "detections": parsed.get("detections", []),
                    },
                    separators=(",", ":"),
                ),
                source="visual_detection",
            )
            await self._publish_log("visual detection triggered alarm", source="visual")

    def _selected_video_url(self) -> str:
        if self.video_source == "visual":
            return self.visual_stream_url
        return self.camera_stream_url

    def _snapshot_url_for_stream(self, source_url: str) -> str:
        if source_url == self.camera_stream_url:
            return self.camera_snapshot_url
        if source_url.rstrip("/").endswith("/stream"):
            return source_url.rstrip("/")[: -len("/stream")] + "/snapshot.jpg"
        return source_url

    async def _open_stream_upstream(self, source_url: str) -> aiohttp.ClientResponse:
        if self._http is None:
            raise web.HTTPInternalServerError(text="HTTP client is not initialized")

        try:
            upstream = await self._http.get(source_url, timeout=aiohttp.ClientTimeout(total=None))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"upstream stream connect error: {exc}") from exc

        if upstream.status >= 400:
            body = await upstream.text()
            await upstream.release()
            raise RuntimeError(f"upstream stream returned {upstream.status}: {body}")
        return upstream

    async def _proxy_stream(self, request: web.Request, source_url: str) -> web.StreamResponse:
        try:
            upstream = await self._open_stream_upstream(source_url)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            raise web.HTTPBadGateway(text=f"upstream stream error: {exc}") from exc

        headers = {}
        ctype = upstream.headers.get("Content-Type")
        if ctype:
            headers["Content-Type"] = ctype
        cache_control = upstream.headers.get("Cache-Control")
        if cache_control:
            headers["Cache-Control"] = cache_control

        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)

        try:
            async for chunk in upstream.content.iter_chunked(16384):
                if not chunk:
                    continue
                await response.write(chunk)
        except ConnectionResetError:
            pass
        finally:
            await upstream.release()

        return response

    async def _stream_hub_reader(self, source_url: str, hub: dict[str, Any]) -> None:
        snapshot_url = self._snapshot_url_for_stream(source_url)
        frame_interval = 1.0 / float(self.video_proxy_fps)
        while hub.get("clients"):
            started = asyncio.get_running_loop().time()
            try:
                if self._http is None:
                    raise RuntimeError("HTTP client is not initialized")
                async with self._http.get(
                    snapshot_url,
                    timeout=aiohttp.ClientTimeout(total=max(2.0, frame_interval * 3.0)),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise RuntimeError(f"snapshot returned {resp.status}: {body}")
                    frame = await resp.read()
                    content_type = resp.headers.get("Content-Type", "image/jpeg")

                if not frame:
                    raise RuntimeError("empty snapshot frame")

                hub["content_type"] = "multipart/x-mixed-replace; boundary=frame"
                frame_id = int(hub.get("frame_id", 0)) + 1
                hub["frame_id"] = frame_id
                multipart_frame = (
                    b"--frame\r\n"
                    + f"Content-Type: {content_type}\r\n".encode("ascii")
                    + f"Content-Length: {len(frame)}\r\n".encode("ascii")
                    + f"X-Frame-Id: {frame_id}\r\n\r\n".encode("ascii")
                    + frame
                    + b"\r\n"
                )

                for q in list(hub.get("clients", set())):
                    try:
                        q.put_nowait(multipart_frame)
                    except asyncio.QueueFull:
                        try:
                            _ = q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            q.put_nowait(multipart_frame)
                        except asyncio.QueueFull:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("shared snapshot reader failed for %s: %s", snapshot_url, exc)
                await asyncio.sleep(0.5)
                continue

            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, frame_interval - elapsed))
        self._stream_hubs.pop(source_url, None)

    async def _shared_proxy_stream(self, request: web.Request, source_url: str) -> web.StreamResponse:
        hub = self._stream_hubs.get(source_url)
        if hub is None:
            hub = {
                "clients": set(),
                "content_type": "multipart/x-mixed-replace; boundary=frame",
                "task": None,
            }
            self._stream_hubs[source_url] = hub

        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=3)
        hub["clients"].add(q)
        if hub.get("task") is None or hub["task"].done():
            hub["task"] = asyncio.create_task(
                self._stream_hub_reader(source_url, hub),
                name=f"stream-hub-{len(self._stream_hubs)}",
            )

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": str(hub.get("content_type") or "multipart/x-mixed-replace; boundary=frame"),
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )
        await response.prepare(request)
        try:
            while True:
                chunk = await q.get()
                await response.write(chunk)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            hub["clients"].discard(q)
            if not hub["clients"]:
                task = hub.get("task")
                if task is not None:
                    task.cancel()
        return response

    async def _handle_video_feed(self, request: web.Request) -> web.StreamResponse:
        requested_source = str(request.query.get("source", "")).strip().lower()
        if requested_source == "camera":
            candidates = [self.camera_stream_url, self.visual_stream_url]
        elif requested_source == "visual":
            candidates = [self.visual_stream_url, self.camera_stream_url]
        else:
            primary = self._selected_video_url()
            secondary = self.visual_stream_url if primary == self.camera_stream_url else self.camera_stream_url
            candidates = [primary, secondary]

        last_err: Exception | None = None
        for idx, candidate in enumerate(candidates, start=1):
            try:
                self.logger.debug("video feed candidate %d/%d: %s", idx, len(candidates), candidate)
                return await self._shared_proxy_stream(request, candidate)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self.logger.warning("video feed candidate failed: %s | error=%s", candidate, exc)
                continue

        err_msg = f"all video feed candidates failed: {last_err}" if last_err else "unknown video feed error"
        self._last_error = err_msg
        raise web.HTTPBadGateway(text=err_msg)

    async def _handle_camera_snapshot(self, _request: web.Request) -> web.Response:
        if self._http is None:
            raise web.HTTPInternalServerError(text="HTTP client is not initialized")

        try:
            async with self._http.get(
                self.camera_snapshot_url,
                timeout=aiohttp.ClientTimeout(total=self.request_timeout_sec),
            ) as resp:
                body = await resp.read()
                status = int(resp.status)
                ctype = resp.headers.get("Content-Type", "image/jpeg")
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            raise web.HTTPBadGateway(text=f"snapshot fetch failed: {exc}") from exc

        return web.Response(status=status, body=body, content_type=ctype)

    @staticmethod
    def _wants_json_error(path: str) -> bool:
        return path.startswith("/api/") or path in {"/health", "/api/health"}

    def _build_app(self) -> web.Application:
        @web.middleware
        async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
            try:
                return await handler(request)
            except web.HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                self.logger.error(
                    "Unhandled exception for %s %s: %s\n%s",
                    request.method,
                    request.path_qs,
                    exc,
                    traceback.format_exc(),
                )
                if self._wants_json_error(request.path):
                    return web.json_response({"error": "internal server error"}, status=500)
                raise web.HTTPInternalServerError(text="internal server error") from exc

        @web.middleware
        async def access_log_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
            started = asyncio.get_running_loop().time()
            status = 500
            try:
                response = await handler(request)
                status = int(getattr(response, "status", 200))
                return response
            except web.HTTPException as exc:
                status = int(exc.status)
                raise
            finally:
                elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000.0)
                if self.verbose_requests or status >= 400:
                    self.logger.info(
                        "%s %s -> %s (%d ms) from %s",
                        request.method,
                        request.path_qs,
                        status,
                        elapsed_ms,
                        request.remote,
                    )

        app = web.Application(middlewares=[error_middleware, access_log_middleware])

        app.router.add_get("/", self._handle_root)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/api/health", self._handle_health)

        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/audio_ws", self._handle_audio_ws)

        app.router.add_get("/video_feed", self._handle_video_feed)
        app.router.add_get("/api/camera/snapshot.jpg", self._handle_camera_snapshot)

        app.router.add_get("/api/logs", self._handle_api_logs)
        app.router.add_get("/api/history", self._handle_api_history)
        app.router.add_get("/api/chat/history", self._handle_api_chat_history)
        app.router.add_post("/api/chat/send", self._handle_api_chat_send)

        app.router.add_get("/api/system/status", self._handle_api_system_status)
        app.router.add_get("/api/system/info", self._handle_api_system_info)
        app.router.add_get("/api/system/history", self._handle_api_system_history)
        app.router.add_post("/api/system/arm", self._handle_api_arm)
        app.router.add_post("/api/system/disarm", self._handle_api_disarm)
        app.router.add_post("/api/system/alarm", self._handle_api_alarm)
        app.router.add_get("/api/services/health", self._handle_api_services_health)
        app.router.add_get("/api/mqtt/status", self._handle_api_mqtt_status)
        app.router.add_get("/api/sensors/status", self._handle_api_sensors_status)
        app.router.add_post("/api/sensors/update", self._handle_api_sensor_update)
        page_pattern = r"/{page:Home_page\.html|log\.html|chat\.html|video\.html|sensors\.html|about_us\.html}"
        app.router.add_get(page_pattern, self._handle_page)
        app.router.add_get("/templates/{name}", self._handle_template_alias)
        app.router.add_get("/static/{path:.*}", self._handle_static)
        app.router.add_get("/Static/{path:.*}", self._handle_static)

        return app

    async def _async_start(self) -> None:
        self._started_at = asyncio.get_running_loop().time()
        self._mqtt_loop = asyncio.get_running_loop()
        self._mqtt_event_queue = asyncio.Queue()
        self._mqtt_event_task = asyncio.create_task(self._mqtt_event_loop(), name="mqtt-event-loop")
        self._http = aiohttp.ClientSession()
        self._start_managed_services()
        self._app = self._build_app()
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self._start_mqtt_client()
        self._visual_alarm_task = asyncio.create_task(self._visual_alarm_loop(), name="visual-alarm-loop")
        self._main_heartbeat_task = asyncio.create_task(self._main_heartbeat_loop(), name="main-mqtt-heartbeat")
        self._voice_loop_task = asyncio.create_task(self._voice_loop_run(), name="voice-button-loop")

        await self._publish_log(
            f"main server listening on http://{self.host}:{self.port}",
            source="startup",
        )
        self._record_history(
            "main_server_started",
            source="startup",
            details={"host": self.host, "port": self.port, "video_source": self.video_source},
        )
        self.logger.info("service_id=%s listening on http://%s:%s", self.service_id, self.host, self.port)
        self.logger.info("pages: /Home_page.html /log.html /chat.html /video.html /sensors.html /about_us.html")
        self.logger.info("websocket: /ws | api: /api/*")
        if self.mqtt_enabled:
            self.logger.info(
                "mqtt: enabled host=%s port=%s tls=%s subs=%s cmd_topic=%s",
                self.mqtt_host,
                self.mqtt_port,
                self.mqtt_use_tls,
                self.mqtt_sub_topics,
                self.mqtt_command_topic,
            )
        else:
            self.logger.info("mqtt: disabled")
        self.logger.info("state files: %s | %s | %s", self.logs_jsonl_path, self.chat_jsonl_path, self.history_jsonl_path)

    async def _async_shutdown(self) -> None:
        if self._visual_alarm_task is not None:
            self._visual_alarm_task.cancel()
            try:
                await self._visual_alarm_task
            except asyncio.CancelledError:
                pass
            self._visual_alarm_task = None
        if self._main_heartbeat_task is not None:
            self._main_heartbeat_task.cancel()
            try:
                await self._main_heartbeat_task
            except asyncio.CancelledError:
                pass
            self._main_heartbeat_task = None
        if self._voice_button_recording:
            try:
                await self._audio_io_post("/mic/record/stop", {}, timeout_sec=5.0)
            except Exception:  # noqa: BLE001
                pass
            self._voice_button_recording = False
        if self._voice_loop_task is not None:
            self._voice_loop_task.cancel()
            try:
                await self._voice_loop_task
            except asyncio.CancelledError:
                pass
            self._voice_loop_task = None
        self._stop_mqtt_client()
        if self._mqtt_event_task is not None:
            self._mqtt_event_task.cancel()
            try:
                await self._mqtt_event_task
            except asyncio.CancelledError:
                pass
            self._mqtt_event_task = None
        self._mqtt_event_queue = None
        self._mqtt_loop = None

        for hub in list(self._stream_hubs.values()):
            task = hub.get("task")
            if task is not None:
                task.cancel()
        self._stream_hubs.clear()

        dead: list[web.WebSocketResponse] = []
        for ws in self._ws_clients:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._http is not None:
            await self._http.close()
            self._http = None
        self._llm_executor.shutdown(wait=False, cancel_futures=True)
        self._stop_managed_services()

    def serve_forever(self) -> None:
        async def runner() -> None:
            try:
                await self._async_start()
                while True:
                    await asyncio.sleep(3600)
            finally:
                await self._async_shutdown()

        try:
            asyncio.run(runner())
        except KeyboardInterrupt:
            pass

    def run(self) -> None:
        self.serve_forever()


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run main Magen website/API server.")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--service-id", default="main-server")
    ap.add_argument("--website-root", default="website-ui/site")
    ap.add_argument("--llm-service-class", required=True)
    ap.add_argument("--llm-service-config", default="{}")
    ap.add_argument("--camera-stream-url", default="http://127.0.0.1:8081/stream")
    ap.add_argument("--camera-snapshot-url", default="http://127.0.0.1:8081/snapshot.jpg")
    ap.add_argument("--visual-stream-url", default="http://127.0.0.1:8091/stream")
    ap.add_argument("--video-source", choices=["camera", "visual"], default="visual")
    ap.add_argument("--state-dir", default="logs")
    ap.add_argument("--request-timeout-sec", type=float, default=20.0)
    ap.add_argument("--llm-request-timeout-sec", type=float, default=45.0)
    ap.add_argument("--llm-max-workers", type=int, default=4)
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--verbose-requests", action="store_true")
    ap.add_argument("--mqtt-enabled", action="store_true")
    ap.add_argument("--mqtt-host", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=8883)
    ap.add_argument("--mqtt-username", default="main_server")
    ap.add_argument("--mqtt-password", default="")
    ap.add_argument("--mqtt-use-tls", action="store_true")
    ap.add_argument("--mqtt-cafile", default="/etc/mosquitto/certs/ca.crt")
    ap.add_argument("--mqtt-insecure-tls", action="store_true")
    ap.add_argument("--mqtt-client-id", default="main-server")
    ap.add_argument(
        "--mqtt-sub-topics",
        default="alarm/state,alarm/state/request,alarm/trigger,alarm/auth/request,alarm/sensor/status,alarm/lock/status,alarm/heartbeat/esp",
    )
    ap.add_argument("--mqtt-command-topic", default="alarm/command")
    ap.add_argument("--mqtt-qos", type=int, default=1)
    ap.add_argument("--mqtt-retain", action="store_true")
    return ap


def main() -> None:
    args = _build_arg_parser().parse_args()
    try:
        llm_cfg = json.loads(args.llm_service_config)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--llm-service-config must be valid JSON: {exc}") from exc
    if not isinstance(llm_cfg, dict):
        raise SystemExit("--llm-service-config must decode to a JSON object")
    service = MainServerService(
        host=args.host,
        port=args.port,
        service_id=args.service_id,
        website_root=args.website_root,
        llm_service_class=args.llm_service_class,
        llm_service_config=llm_cfg,
        camera_stream_url=args.camera_stream_url,
        camera_snapshot_url=args.camera_snapshot_url,
        visual_stream_url=args.visual_stream_url,
        video_source=args.video_source,
        state_dir=args.state_dir,
        request_timeout_sec=args.request_timeout_sec,
        llm_request_timeout_sec=args.llm_request_timeout_sec,
        llm_max_workers=args.llm_max_workers,
        log_level=args.log_level,
        verbose_requests=bool(args.verbose_requests),
        mqtt_enabled=bool(args.mqtt_enabled),
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        mqtt_username=args.mqtt_username,
        mqtt_password=args.mqtt_password,
        mqtt_use_tls=bool(args.mqtt_use_tls),
        mqtt_cafile=args.mqtt_cafile,
        mqtt_insecure_tls=bool(args.mqtt_insecure_tls),
        mqtt_client_id=args.mqtt_client_id,
        mqtt_sub_topics=args.mqtt_sub_topics,
        mqtt_command_topic=args.mqtt_command_topic,
        mqtt_qos=args.mqtt_qos,
        mqtt_retain=bool(args.mqtt_retain),
    )
    service.serve_forever()


if __name__ == "__main__":
    main()
