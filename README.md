# Magen Main Server

The main server is the Raspberry Pi control center for the Magen system. It serves the website, exposes the HTTP/WebSocket API, talks to the ESP over MQTT, starts the local Pi services, and gives the chat/voice LLM access to system tools.

## What It Runs

- Website pages from `website-ui/site`
- REST API under `/api/*`
- Website WebSocket at `/ws`
- Browser-to-Pi audio WebSocket at `/audio_ws`
- Video proxy at `/video_feed`
- MQTT connection to Mosquitto for ESP state, commands, sensors, lock status, and heartbeat
- In-process LLM service for chat and local voice commands
- Optional managed subprocesses:
  - audio IO service
  - TTS service
  - visual processing service
  - camera service, only when visual camera mode is `stream`

## Important Files

- `main-server/run_main_server.py` validates config and starts the server.
- `main-server/main_web_server.py` contains the website/API server, MQTT handling, service management, video proxy, voice loop, and alarm logic.
- `main-server/config.py` contains default settings and environment variable overrides.
- `main-server/check_config.py` validates service classes, required model files, URLs, ports, MQTT TLS files, and website paths.
- `main-server/llm/gemini_audio_llm_service.py` handles chat and voice LLM calls.
- `main-server/llm/system_tools.py` defines the tools the LLM can use.
- `main-server/tts/` contains TTS providers.
- `systemd/magen-main-server.service` is the boot service unit.
- `install_main_server_service.sh` installs and starts the systemd service.

## Service Flow

On startup, `run_main_server.py` calls `check_config.py`. If config is valid, it builds `MainServerService` from the class path in `config.py`.

`MainServerService` then:

1. Starts the managed services configured in `MANAGED_SERVICES`.
2. Starts the aiohttp website/API server on `MAIN_SERVER_HOST:MAIN_SERVER_PORT`.
3. Connects to MQTT if `MAIN_MQTT_ENABLED=true`.
4. Starts background loops for visual alarm checks, main-server heartbeat, and the local voice button loop.
5. Keeps logs, chat history, alarm state, and sensor state in `logs/`.

The ESP communicates with the Pi mostly through MQTT. The website and LLM talk to the main server through local API calls and in-process tool calls.

## Managed Services

The main server starts these by default:

- `audio_io`: `main-server/run_audio_io_service.py`
- `tts`: `main-server/run_tts_service.py`
- `visual_processing`: `main-server/run_visual_processing_service.py`

The camera service is only started automatically when:

```bash
VISUAL_CAMERA_MODE=stream
```

With the default:

```bash
VISUAL_CAMERA_MODE=direct
```

the visual processing service opens the Pi camera directly, and the separate camera service is not started.

## Common Commands

Run a config check:

```bash
.venv/bin/python main-server/check_config.py
```

Start the main server manually:

```bash
.venv/bin/python main-server/run_main_server.py
```

Install and start it as a boot service:

```bash
./install_main_server_service.sh
```

Check the service:

```bash
systemctl status magen-main-server.service
journalctl -u magen-main-server.service -f
```

## Website

Default URL:

```text
http://<pi-ip>:8080/
```

Main pages:

- `/Home_page.html`
- `/log.html`
- `/chat.html`
- `/video.html`
- `/sensors.html`
- `/about_us.html`

## Key API Endpoints

- `GET /health` or `/api/health`: main server health
- `GET /api/system/status`: alarm, lock, sensor, MQTT, and connectivity state
- `POST /api/system/arm`: arm system, requires password
- `POST /api/system/disarm`: disarm system, requires password
- `POST /api/system/alarm`: activate alarm
- `GET /api/sensors/status`: sensor status
- `POST /api/sensors/update`: enable or disable a sensor, requires password
- `GET /api/services/health`: managed service health
- `GET /api/mqtt/status`: MQTT connection and recent MQTT state
- `GET /video_feed`: proxied video feed

## MQTT

Defaults:

- Host: `localhost`
- Port: `8883`
- TLS: enabled
- CA file: `/etc/mosquitto/certs/ca.crt`
- Command topic: `alarm/command`

Subscribed topics:

- `alarm/state`
- `alarm/state/request`
- `alarm/trigger`
- `alarm/auth/request`
- `alarm/sensor/status`
- `alarm/lock/status`
- `alarm/heartbeat/esp`

The main server also publishes commands and heartbeat/state updates so the ESP and website can stay synchronized.

## Voice Loop

The local voice loop is handled by the main server and uses GPIO `23` by default.

Behavior:

1. Hold the button for at least `MAIN_VOICE_LOOP_PRESS_THRESHOLD_SEC`.
2. The audio IO service records while the button is held.
3. After release, the recorded segment is sent to the LLM.
4. The LLM may call system tools.
5. TTS can speak the response through the Pi speakers.

Useful settings:

- `MAIN_VOICE_LOOP_ENABLED`
- `MAIN_VOICE_LOOP_BUTTON_PIN`
- `MAIN_VOICE_LOOP_PRESS_THRESHOLD_SEC`
- `MAIN_VOICE_LOOP_RELEASE_THRESHOLD_SEC`
- `MAIN_VOICE_LOOP_MIN_RECORD_SEC`

## Important Environment Variables

Main server:

- `MAIN_SERVER_HOST`
- `MAIN_SERVER_PORT`
- `MAIN_SERVER_WEBSITE_ROOT`
- `MAIN_SERVER_STATE_DIR`
- `MAIN_SERVER_VIDEO_SOURCE`

MQTT:

- `MAIN_MQTT_ENABLED`
- `MAIN_MQTT_HOST`
- `MAIN_MQTT_PORT`
- `MAIN_MQTT_USER`
- `MAIN_MQTT_PASS`
- `MAIN_MQTT_TLS`
- `MAIN_MQTT_CAFILE`

Visual/camera:

- `VISUAL_CAMERA_MODE`: `direct` or `stream`
- `VISUAL_YOLO_MODEL`
- `VISUAL_MOTION_GATED_YOLO`
- `MAIN_MANAGE_CAMERA`
- `MAIN_MANAGE_VISUAL`

Audio/TTS/LLM:

- `MAIN_MANAGE_AUDIO_IO`
- `MAIN_MANAGE_TTS`
- `AUDIO_IO_INPUT_DEVICE`
- `AUDIO_IO_OUTPUT_DEVICE`
- `LLM_API_KEY` or `GEMINI_API_KEY`
- `SYSTEM_ARM_PASSWORD`

## State And Logs

By default, runtime state is stored in `logs/`:

- `logs/logs.jsonl`
- `logs/history.jsonl`
- `logs/chat.jsonl`
- `logs/control_state.json`
- service log files such as `audio_io.log`, `visual_processing.log`, and `camera.log`

These are runtime files, not source files.

## Notes

- Password validation uses the configured `SYSTEM_ARM_PASSWORD` value. The default value is a SHA-256 hash of the expected system password.
- The LLM tools can check system status, arm/disarm, enable/disable sensors, and read recent history. Actions that change alarm/sensor state require the password.
- The visual sensor is treated as a Pi-side sensor and can be enabled or disabled from the website like the ESP-side sensors.
- If MQTT is down, the Pi website can still run, but ESP synchronization and commands depend on the broker connection.
