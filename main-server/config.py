"""Main server config.

Services are selected by file path + class name so providers are replaceable
without changing calling code.
"""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str) -> str:
    raw = os.environ.get(name, default)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return ",".join(parts)

# Replace this path with another provider file to swap camera implementation.
# Format: "/abs/or/relative/path/to/file.py:ClassName"
CAMERA_SERVICE_CLASS = f"{PROJECT_ROOT / 'camera-pi' / 'pi_camera_service.py'}:PiCameraService"

CAMERA_SERVICE_CONFIG = {
    "host": "0.0.0.0",
    "port": 8081,
    "width": 480,
    "height": 270,
    "fps": 5,
    "camera_id": "front-door-pi-camera",
    "camera_index": 0,
    "rpicam_path": os.environ.get("CAMERA_RPICAM_PATH", "rpicam-vid"),
    "jpeg_quality": int(os.environ.get("CAMERA_JPEG_QUALITY", "80")),
    "autofocus_mode": os.environ.get("CAMERA_AUTOFOCUS_MODE", "continuous"),
    "restart_delay_sec": float(os.environ.get("CAMERA_RESTART_DELAY_SEC", "2.0")),
    "stale_after_sec": float(os.environ.get("CAMERA_STALE_AFTER_SEC", "5.0")),
}

VISUAL_CAMERA_MODE = os.environ.get("VISUAL_CAMERA_MODE", "direct").strip().lower()
if VISUAL_CAMERA_MODE not in {"stream", "direct"}:
    VISUAL_CAMERA_MODE = "direct"
VISUAL_CAMERA_HOST = os.environ.get("VISUAL_CAMERA_HOST", "127.0.0.1")
VISUAL_CAMERA_PORT = int(os.environ.get("VISUAL_CAMERA_PORT", str(CAMERA_SERVICE_CONFIG["port"])))
VISUAL_INPUT_SOURCE_DEFAULT = f"http://{VISUAL_CAMERA_HOST}:{VISUAL_CAMERA_PORT}/stream"

# Replace this path to swap visual processing implementation.
VISUAL_PROCESSING_SERVICE_CLASS = (
    f"{PROJECT_ROOT / 'visual-processing' / 'human_detection_service.py'}:HumanDetectionProcessingService"
)

VISUAL_PROCESSING_SERVICE_CONFIG = {
    "input_source": os.environ.get("VISUAL_INPUT_SOURCE", VISUAL_INPUT_SOURCE_DEFAULT),
    "input_mode": VISUAL_CAMERA_MODE,
    "host": "0.0.0.0",
    "port": 8091,
    "fps": 5,
    "min_confidence": 0.35,
    "detector": "yolo",
    "yolo_model": os.environ.get("VISUAL_YOLO_MODEL", str(PROJECT_ROOT / "models" / "visual-processing" / "yolo11n_openvino_model")),
    "motion_gated_yolo": _env_bool("VISUAL_MOTION_GATED_YOLO", False), # false for test
    "motion_pixel_ratio_threshold": float(
        os.environ.get("VISUAL_MOTION_PIXEL_RATIO_THRESHOLD", "0.01")
    ),
    "motion_min_contour_area": int(os.environ.get("VISUAL_MOTION_MIN_CONTOUR_AREA", "900")),
    "motion_hold_sec": float(os.environ.get("VISUAL_MOTION_HOLD_SEC", "2.0")),
    "service_id": "human-detector-main",
    "direct_camera_index": int(os.environ.get("VISUAL_DIRECT_CAMERA_INDEX", str(CAMERA_SERVICE_CONFIG["camera_index"]))),
    "direct_camera_width": int(os.environ.get("VISUAL_DIRECT_CAMERA_WIDTH", str(CAMERA_SERVICE_CONFIG["width"]))),
    "direct_camera_height": int(os.environ.get("VISUAL_DIRECT_CAMERA_HEIGHT", str(CAMERA_SERVICE_CONFIG["height"]))),
    "direct_camera_fps": int(os.environ.get("VISUAL_DIRECT_CAMERA_FPS", str(CAMERA_SERVICE_CONFIG["fps"]))),
    "direct_camera_rpicam_path": os.environ.get("VISUAL_DIRECT_CAMERA_RPICAM_PATH", CAMERA_SERVICE_CONFIG["rpicam_path"]),
    "direct_camera_jpeg_quality": int(
        os.environ.get("VISUAL_DIRECT_CAMERA_JPEG_QUALITY", str(CAMERA_SERVICE_CONFIG["jpeg_quality"]))
    ),
    "direct_camera_autofocus_mode": os.environ.get(
        "VISUAL_DIRECT_CAMERA_AUTOFOCUS_MODE",
        CAMERA_SERVICE_CONFIG["autofocus_mode"],
    ),
}

# Replace this path to swap mic+speaker implementation.
AUDIO_IO_SERVICE_CLASS = f"{PROJECT_ROOT / 'audio-io' / 'mic_speaker_service.py'}:MicSpeakerService"

AUDIO_IO_SERVICE_CONFIG = {
    "host": "0.0.0.0",
    "port": int(os.environ.get("AUDIO_IO_PORT", "8092")),
    "ws_enabled": _env_bool("AUDIO_IO_WS_ENABLED", True),
    "ws_host": os.environ.get("AUDIO_IO_WS_HOST", "0.0.0.0"),
    "ws_port": int(os.environ.get("AUDIO_IO_WS_PORT", "8095")),
    "service_id": os.environ.get("AUDIO_IO_SERVICE_ID", "mic-speaker-main"),
    "input_device": os.environ.get("AUDIO_IO_INPUT_DEVICE", ""),
    "output_device": os.environ.get("AUDIO_IO_OUTPUT_DEVICE", ""),
    "sample_rate": int(os.environ.get("AUDIO_IO_SAMPLE_RATE", "16000")),
    "chunk_samples": int(os.environ.get("AUDIO_IO_CHUNK_SAMPLES", "512")),
    "max_segments": int(os.environ.get("AUDIO_IO_MAX_SEGMENTS", "50")),
}

_tts_provider = os.environ.get("TTS_PROVIDER", "edge").strip().lower()
_tts_providers = {
    "edge": f"{PROJECT_ROOT / 'main-server' / 'tts' / 'edge_tts_service.py'}:EdgeTTSService",
    "kokoro": f"{PROJECT_ROOT / 'main-server' / 'tts' / 'kokoro_tts_service.py'}:KokoroTTSService",
}

# Replace this path (or set TTS_SERVICE_CLASS env) to swap TTS implementation.
TTS_SERVICE_CLASS = os.environ.get("TTS_SERVICE_CLASS", _tts_providers.get(_tts_provider, _tts_providers["edge"]))

if "kokoro_tts_service.py" in TTS_SERVICE_CLASS:
    TTS_SERVICE_CONFIG = {
        "host": os.environ.get("TTS_HOST", "0.0.0.0"),
        "port": int(os.environ.get("TTS_PORT", "8093")),
        "service_id": os.environ.get("TTS_SERVICE_ID", "tts-main"),
        "model_path": os.environ.get(
            "TTS_KOKORO_MODEL_PATH",
            str(PROJECT_ROOT / "models" / "tts" / "kokoro-v1.0.onnx"),
        ),
        "voices_path": os.environ.get(
            "TTS_KOKORO_VOICES_PATH",
            str(PROJECT_ROOT / "models" / "tts" / "voices-v1.0.bin"),
        ),
        "default_voice": os.environ.get("TTS_DEFAULT_VOICE", "af_sarah"),
        "default_lang": os.environ.get("TTS_KOKORO_DEFAULT_LANG", "en-us"),
        "default_speed": float(os.environ.get("TTS_KOKORO_DEFAULT_SPEED", "1.0")),
        "output_dir": os.environ.get("TTS_OUTPUT_DIR", "/tmp/magen-tts"),
        "keep_files": _env_bool("TTS_KEEP_FILES", False),
        "max_history": int(os.environ.get("TTS_MAX_HISTORY", "100")),
        "audio_io_play_url": os.environ.get(
            "TTS_AUDIO_IO_PLAY_URL",
            "http://127.0.0.1:8092/speaker/play-wav",
        ),
        "request_timeout_sec": float(os.environ.get("TTS_REQUEST_TIMEOUT_SEC", "30")),
    }
else:
    TTS_SERVICE_CONFIG = {
        "host": os.environ.get("TTS_HOST", "0.0.0.0"),
        "port": int(os.environ.get("TTS_PORT", "8093")),
        "service_id": os.environ.get("TTS_SERVICE_ID", "tts-main"),
        "default_voice": os.environ.get("TTS_DEFAULT_VOICE", "en-US-AriaNeural"),
        "default_rate": os.environ.get("TTS_DEFAULT_RATE", "+0%"),
        "default_pitch": os.environ.get("TTS_DEFAULT_PITCH", "+0Hz"),
        "default_volume": os.environ.get("TTS_DEFAULT_VOLUME", "+0%"),
        "output_format": os.environ.get("TTS_OUTPUT_FORMAT", "audio-24khz-48kbitrate-mono-mp3"),
        "output_dir": os.environ.get("TTS_OUTPUT_DIR", "/tmp/magen-tts"),
        "keep_files": _env_bool("TTS_KEEP_FILES", False),
        "max_history": int(os.environ.get("TTS_MAX_HISTORY", "100")),
        "audio_io_play_url": os.environ.get(
            "TTS_AUDIO_IO_PLAY_URL",
            "http://127.0.0.1:8092/speaker/play-wav",
        ),
        "request_timeout_sec": float(os.environ.get("TTS_REQUEST_TIMEOUT_SEC", "30")),
        "synthesis_retries": int(os.environ.get("TTS_SYNTHESIS_RETRIES", "3")),
        "retry_backoff_sec": float(os.environ.get("TTS_RETRY_BACKOFF_SEC", "0.8")),
    }

# Replace this path to swap LLM implementation.
LLM_SERVICE_CLASS = os.environ.get(
    "LLM_SERVICE_CLASS",
    f"{PROJECT_ROOT / 'main-server' / 'llm' / 'gemini_audio_llm_service.py'}:GeminiAudioLLMService",
)

_llm_health_urls = {
    "audio_io": os.environ.get("LLM_HEALTH_AUDIO_IO_URL", "http://127.0.0.1:8092/health"),
    "tts": os.environ.get("LLM_HEALTH_TTS_URL", "http://127.0.0.1:8093/health"),
    "visual_processing": os.environ.get(
        "LLM_HEALTH_VISUAL_URL",
        "http://127.0.0.1:8091/health",
    ),
}
if VISUAL_CAMERA_MODE == "stream":
    _llm_health_urls["camera"] = os.environ.get(
        "LLM_HEALTH_CAMERA_URL",
        f"http://127.0.0.1:{CAMERA_SERVICE_CONFIG['port']}/health",
    )

LLM_SERVICE_CONFIG = {
    "host": os.environ.get("LLM_HOST", "0.0.0.0"),
    "port": int(os.environ.get("LLM_PORT", "8094")),
    "service_id": os.environ.get("LLM_SERVICE_ID", "llm-main"),
    # LLM_API_KEY takes precedence, then GEMINI_API_KEY.
    "api_key": os.environ.get("LLM_API_KEY", os.environ.get("GEMINI_API_KEY", "placeholder")),
    "model_config_path": os.environ.get(
        "LLM_MODEL_CONFIG_PATH",
        str(PROJECT_ROOT / "main-server" / "llm" / "gemini_model_config.json"),
    ),
    "request_timeout_sec": float(os.environ.get("LLM_REQUEST_TIMEOUT_SEC", "45")),
    "max_events": int(os.environ.get("LLM_MAX_EVENTS", "500")),
    "max_history": int(os.environ.get("LLM_MAX_HISTORY", "200")),
    "assistant_max_tool_rounds": int(os.environ.get("LLM_ASSISTANT_MAX_TOOL_ROUNDS", "4")),
    # Secret used only for tool validation, never exposed to the model.
    "system_arm_password": os.environ.get("SYSTEM_ARM_PASSWORD", "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4"),
    "tts_speak_url": os.environ.get("LLM_TTS_SPEAK_URL", "http://127.0.0.1:8093/tts/speak"),
    "enable_tts_for_actions": _env_bool("LLM_ENABLE_TTS_FOR_ACTIONS", True),
    "tts_action_channels": _env_csv("LLM_TTS_ACTION_CHANNELS", "local"),
    "tts_timeout_sec": float(os.environ.get("LLM_TTS_TIMEOUT_SEC", "12")),
    "service_health_urls": {k: v for k, v in _llm_health_urls.items() if v.strip()},
}

# Replace this path to swap main website/API server implementation.
MAIN_SERVER_SERVICE_CLASS = os.environ.get(
    "MAIN_SERVER_SERVICE_CLASS",
    f"{PROJECT_ROOT / 'main-server' / 'main_web_server.py'}:MainServerService",
)

_main_server_health_urls = {
    "audio_io": os.environ.get("MAIN_HEALTH_AUDIO_IO_URL", "http://127.0.0.1:8092/health"),
    "tts": os.environ.get("MAIN_HEALTH_TTS_URL", "http://127.0.0.1:8093/health"),
    "visual_processing": os.environ.get("MAIN_HEALTH_VISUAL_URL", "http://127.0.0.1:8091/health"),
}
if VISUAL_CAMERA_MODE == "stream":
    _main_server_health_urls["camera"] = os.environ.get(
        "MAIN_HEALTH_CAMERA_URL",
        f"http://127.0.0.1:{CAMERA_SERVICE_CONFIG['port']}/health",
    )

MANAGED_SERVICES = [
    {
        "name": "camera",
        "enabled": _env_bool("MAIN_MANAGE_CAMERA", VISUAL_CAMERA_MODE == "stream"),
        "cwd": str(PROJECT_ROOT),
        "command": ["python3", "main-server/run_camera_service.py"],
        "health_url": "http://127.0.0.1:8081/health",
    },
    {
        "name": "audio_io",
        "enabled": _env_bool("MAIN_MANAGE_AUDIO_IO", True),
        "cwd": str(PROJECT_ROOT),
        "command": ["python3", "main-server/run_audio_io_service.py"],
        "health_url": "http://127.0.0.1:8092/health",
    },
    {
        "name": "tts",
        "enabled": _env_bool("MAIN_MANAGE_TTS", True),
        "cwd": str(PROJECT_ROOT),
        "command": ["python3", "main-server/run_tts_service.py"],
        "health_url": "http://127.0.0.1:8093/health",
    },
    {
        "name": "visual_processing",
        "enabled": _env_bool("MAIN_MANAGE_VISUAL", True),
        "cwd": str(PROJECT_ROOT),
        "command": ["python3", "main-server/run_visual_processing_service.py"],
        "health_url": "http://127.0.0.1:8091/health",
    },
]

MAIN_SERVER_SERVICE_CONFIG = {
    "host": os.environ.get("MAIN_SERVER_HOST", "0.0.0.0"),
    "port": int(os.environ.get("MAIN_SERVER_PORT", "8080")),
    "service_id": os.environ.get("MAIN_SERVER_ID", "main-server"),
    "website_root": os.environ.get(
        "MAIN_SERVER_WEBSITE_ROOT",
        str(PROJECT_ROOT / "website-ui" / "site"),
    ),
    "llm_service_class": LLM_SERVICE_CLASS,
    "llm_service_config": dict(LLM_SERVICE_CONFIG),
    "camera_stream_url": os.environ.get("MAIN_SERVER_CAMERA_STREAM_URL", "http://127.0.0.1:8081/stream"),
    "camera_snapshot_url": os.environ.get(
        "MAIN_SERVER_CAMERA_SNAPSHOT_URL",
        "http://127.0.0.1:8081/snapshot.jpg",
    ),
    "visual_stream_url": os.environ.get("MAIN_SERVER_VISUAL_STREAM_URL", "http://127.0.0.1:8091/stream"),
    "video_source": os.environ.get("MAIN_SERVER_VIDEO_SOURCE", "visual"),
    "video_proxy_fps": int(os.environ.get("MAIN_SERVER_VIDEO_PROXY_FPS", "5")),
    "state_dir": os.environ.get("MAIN_SERVER_STATE_DIR", str(PROJECT_ROOT / "logs")),
    "request_timeout_sec": float(os.environ.get("MAIN_SERVER_REQUEST_TIMEOUT_SEC", "20")),
    "llm_request_timeout_sec": float(os.environ.get("MAIN_SERVER_LLM_REQUEST_TIMEOUT_SEC", "45")),
    "llm_max_workers": int(os.environ.get("MAIN_SERVER_LLM_MAX_WORKERS", "4")),
    "log_level": os.environ.get("MAIN_SERVER_LOG_LEVEL", "INFO"),
    "verbose_requests": _env_bool("MAIN_SERVER_VERBOSE_REQUESTS", True),
    "max_chat_history": int(os.environ.get("MAIN_SERVER_MAX_CHAT_HISTORY", "200")),
    "max_log_entries": int(os.environ.get("MAIN_SERVER_MAX_LOG_ENTRIES", "2000")),
    "max_history_entries": int(os.environ.get("MAIN_SERVER_MAX_HISTORY_ENTRIES", "4000")),
    "mqtt_enabled": _env_bool("MAIN_MQTT_ENABLED", True),
    "mqtt_host": os.environ.get("MAIN_MQTT_HOST", "localhost"),
    "mqtt_port": int(os.environ.get("MAIN_MQTT_PORT", "8883")),
    "mqtt_username": os.environ.get("MAIN_MQTT_USER", "main_server"),
    "mqtt_password": os.environ.get("MAIN_MQTT_PASS", "1234"),
    "mqtt_use_tls": _env_bool("MAIN_MQTT_TLS", True),
    "mqtt_cafile": os.environ.get("MAIN_MQTT_CAFILE", "/etc/mosquitto/certs/ca.crt"),
    "mqtt_insecure_tls": _env_bool("MAIN_MQTT_INSECURE_TLS", False),
    "mqtt_client_id": os.environ.get("MAIN_MQTT_CLIENT_ID", "main-server"),
    "mqtt_sub_topics": _env_csv(
        "MAIN_MQTT_SUB_TOPICS",
        "alarm/state,alarm/state/request,alarm/trigger,alarm/auth/request,alarm/sensor/status,alarm/lock/status,alarm/heartbeat/esp",
    ),
    "mqtt_command_topic": os.environ.get("MAIN_MQTT_COMMAND_TOPIC", "alarm/command"),
    "mqtt_qos": int(os.environ.get("MAIN_MQTT_QOS", "1")),
    "mqtt_retain": _env_bool("MAIN_MQTT_RETAIN", False),
    "gpio_alarm_pin": int(os.environ.get("MAIN_GPIO_ALARM_PIN", "17")),
    "audio_io_base_url": os.environ.get("MAIN_AUDIO_IO_BASE_URL", "http://127.0.0.1:8092"),
    "audio_io_ws_url": os.environ.get("MAIN_AUDIO_IO_WS_URL", "ws://127.0.0.1:8095"),
    "tts_speak_url": os.environ.get("MAIN_TTS_SPEAK_URL", "http://127.0.0.1:8093/tts/speak"),
    "voice_loop_enabled": _env_bool("MAIN_VOICE_LOOP_ENABLED", True),
    "voice_loop_poll_sec": float(os.environ.get("MAIN_VOICE_LOOP_POLL_SEC", "0.8")),
    "voice_loop_button_pin": int(os.environ.get("MAIN_VOICE_LOOP_BUTTON_PIN", "23")),
    "voice_loop_press_threshold_sec": float(os.environ.get("MAIN_VOICE_LOOP_PRESS_THRESHOLD_SEC", "0.5")),
    "voice_loop_release_threshold_sec": float(os.environ.get("MAIN_VOICE_LOOP_RELEASE_THRESHOLD_SEC", "0.2")),
    "voice_loop_min_record_sec": float(os.environ.get("MAIN_VOICE_LOOP_MIN_RECORD_SEC", "0.35")),
    "voice_loop_channel": os.environ.get("MAIN_VOICE_LOOP_CHANNEL", "local"),
    "voice_loop_source": os.environ.get("MAIN_VOICE_LOOP_SOURCE", "main_server_voice"),
    "voice_loop_prompt": os.environ.get(
        "MAIN_VOICE_LOOP_PROMPT",
        "Interpret this local voice command and use tools when needed.",
    ),
    "voice_loop_busy_beep_enabled": _env_bool("MAIN_VOICE_LOOP_BUSY_BEEP", True),
    "voice_loop_busy_beep_frequency_hz": float(
        os.environ.get("MAIN_VOICE_LOOP_BUSY_BEEP_FREQUENCY_HZ", "880")
    ),
    "voice_loop_busy_beep_duration_ms": int(
        os.environ.get("MAIN_VOICE_LOOP_BUSY_BEEP_DURATION_MS", "140")
    ),
    "voice_loop_busy_beep_interval_sec": float(
        os.environ.get("MAIN_VOICE_LOOP_BUSY_BEEP_INTERVAL_SEC", "0.9")
    ),
    "voice_loop_listen_beep_enabled": _env_bool("MAIN_VOICE_LOOP_LISTEN_BEEP", True),
    "voice_loop_listen_beep_frequency_hz": float(
        os.environ.get("MAIN_VOICE_LOOP_LISTEN_BEEP_FREQUENCY_HZ", "640")
    ),
    "voice_loop_listen_beep_duration_ms": int(
        os.environ.get("MAIN_VOICE_LOOP_LISTEN_BEEP_DURATION_MS", "120")
    ),
    "voice_loop_listen_beep_volume": float(
        os.environ.get("MAIN_VOICE_LOOP_LISTEN_BEEP_VOLUME", "0.18")
    ),
    "health_urls": {k: v for k, v in _main_server_health_urls.items() if v.strip()},
    "managed_services": MANAGED_SERVICES,
}
