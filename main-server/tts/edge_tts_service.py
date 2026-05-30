#!/usr/bin/env python3
"""Pluggable Edge TTS service provider.

This service can synthesize speech with edge-tts and optionally forward the
resulting WAV file to the local audio I/O service for speaker playback.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import shutil
import ssl
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

import aiohttp
import certifi

try:
    import edge_tts
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency 'edge-tts'. Install with: pip install edge-tts") from exc

from edge_tts.communicate import (
    connect_id,
    date_to_string,
    get_headers_and_data,
    mkssml,
    remove_incompatible_characters,
    ssml_headers_plus_data,
)
from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
from edge_tts.data_classes import TTSConfig
from edge_tts.drm import DRM


@dataclass(slots=True)
class TTSResult:
    clip_id: str
    created_unix: float
    text: str
    voice: str
    rate: str
    pitch: str
    volume: str
    output_format: str
    output_path: str
    is_wav: bool
    duration_ms: int | None
    file_size_bytes: int
    played: bool
    play_error: str


class EdgeTTSService:
    """Synthesize speech with edge-tts and optionally play via audio-io."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8093,
        service_id: str = "tts-main",
        default_voice: str = "en-US-AriaNeural",
        default_rate: str = "+0%",
        default_pitch: str = "+0Hz",
        default_volume: str = "+0%",
        output_format: str = "audio-24khz-48kbitrate-mono-mp3",
        output_dir: str = "/tmp/magen-tts",
        keep_files: bool = False,
        max_history: int = 100,
        audio_io_play_url: str = "http://127.0.0.1:8092/speaker/play-wav",
        request_timeout_sec: float = 30.0,
        synthesis_retries: int = 3,
        retry_backoff_sec: float = 0.8,
    ) -> None:
        if port <= 0:
            raise ValueError("port must be positive")
        if max_history <= 0:
            raise ValueError("max_history must be positive")
        if request_timeout_sec <= 0:
            raise ValueError("request_timeout_sec must be positive")
        if synthesis_retries <= 0:
            raise ValueError("synthesis_retries must be positive")
        if retry_backoff_sec < 0:
            raise ValueError("retry_backoff_sec must be >= 0")

        self.host = host
        self.port = int(port)
        self.service_id = service_id

        self.default_voice = default_voice
        self.default_rate = default_rate
        self.default_pitch = default_pitch
        self.default_volume = default_volume
        self.output_format = output_format

        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.keep_files = bool(keep_files)
        self.audio_io_play_url = audio_io_play_url
        self.request_timeout_sec = float(request_timeout_sec)
        self.synthesis_retries = int(synthesis_retries)
        self.retry_backoff_sec = float(retry_backoff_sec)

        self._history: collections.deque[TTSResult] = collections.deque(maxlen=max_history)
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=500)
        self._lock = threading.Lock()

        self._server: TTSHTTPServer | None = None
        self._started_at = time.time()
        self._last_error = ""

    @staticmethod
    def _is_wav_file(path: Path) -> bool:
        try:
            with path.open("rb") as f:
                header = f.read(12)
        except OSError:
            return False
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"

    @staticmethod
    def _wav_duration_ms(path: Path) -> int | None:
        try:
            with wave.open(str(path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
            if rate <= 0:
                return None
            return int((frames / rate) * 1000.0)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _convert_to_wav_with_ffmpeg(src_path: Path) -> Path | None:
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return None
        dst_path = src_path.with_suffix(".ffmpeg.wav")
        cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src_path),
            str(dst_path),
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)  # noqa: S603
        if proc.returncode != 0:
            return None
        if not dst_path.exists():
            return None
        return dst_path

    @staticmethod
    def _parse_bool(raw: Any, default: bool) -> bool:
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)

    def _emit_event(self, event_type: str, **data: Any) -> dict[str, Any]:
        evt = {
            "id": len(self._events) + 1,
            "timestamp": time.time(),
            "event": event_type,
            **data,
        }
        with self._lock:
            self._events.append(evt)
        return evt

    def _format_candidates(self) -> list[str]:
        configured = self.output_format.strip()
        candidates: list[str] = [
            configured if configured else "riff-24khz-16bit-mono-pcm",
            "riff-24khz-16bit-mono-pcm",
            "riff-16khz-16bit-mono-pcm",
            "audio-24khz-48kbitrate-mono-mp3",
        ]
        deduped: list[str] = []
        for item in candidates:
            if item and item not in deduped:
                deduped.append(item)
        return deduped

    async def _save_audio_with_output_format(
        self,
        text: str,
        voice: str,
        rate: str,
        pitch: str,
        volume: str,
        output_format: str,
        output_path: Path,
    ) -> None:
        tts_cfg = TTSConfig(
            voice=voice,
            rate=rate,
            volume=volume,
            pitch=pitch,
            boundary="SentenceBoundary",
        )
        escaped_text = escape(remove_incompatible_characters(text))

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=None,
            sock_connect=10,
            sock_read=60,
        )

        ws_url = (
            f"{WSS_URL}&ConnectionId={connect_id()}"
            f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}"
        )

        audio_chunks: list[bytes] = []
        async with aiohttp.ClientSession(
            trust_env=True,
            timeout=timeout,
        ) as session, session.ws_connect(
            ws_url,
            compress=15,
            headers=DRM.headers_with_muid(WSS_HEADERS),
            ssl=ssl_ctx,
        ) as websocket:
            await websocket.send_str(
                f"X-Timestamp:{date_to_string()}\r\n"
                "Content-Type:application/json; charset=utf-8\r\n"
                "Path:speech.config\r\n\r\n"
                '{"context":{"synthesis":{"audio":{"metadataoptions":{'
                '"sentenceBoundaryEnabled":"true","wordBoundaryEnabled":"false"'
                "},"
                f'"outputFormat":"{output_format}"'
                "}}}}\r\n"
            )

            await websocket.send_str(
                ssml_headers_plus_data(
                    connect_id(),
                    date_to_string(),
                    mkssml(tts_cfg, escaped_text),
                )
            )

            async for received in websocket:
                if received.type == aiohttp.WSMsgType.TEXT:
                    encoded_data: bytes = received.data.encode("utf-8")
                    header_end = encoded_data.find(b"\r\n\r\n")
                    if header_end <= 0:
                        continue
                    parameters, _data = get_headers_and_data(encoded_data, header_end)
                    if parameters.get(b"Path", None) == b"turn.end":
                        break
                    continue

                if received.type == aiohttp.WSMsgType.BINARY:
                    if len(received.data) < 2:
                        continue
                    header_length = int.from_bytes(received.data[:2], "big")
                    if header_length > len(received.data):
                        continue
                    parameters, data = get_headers_and_data(received.data, header_length)
                    if parameters.get(b"Path") == b"audio" and data:
                        audio_chunks.append(bytes(data))
                    continue

                if received.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(received.data if received.data else "unknown websocket error")

        if not audio_chunks:
            raise RuntimeError("No audio was received. Please verify that your parameters are correct.")

        with output_path.open("wb") as f:
            f.write(b"".join(audio_chunks))

    async def _save_audio(
        self,
        text: str,
        voice: str,
        rate: str,
        pitch: str,
        volume: str,
        output_path: Path,
    ) -> str:
        errors: list[str] = []
        for output_format in self._format_candidates():
            for attempt in range(1, self.synthesis_retries + 1):
                try:
                    if output_path.exists():
                        output_path.unlink()
                    await self._save_audio_with_output_format(
                        text=text,
                        voice=voice,
                        rate=rate,
                        pitch=pitch,
                        volume=volume,
                        output_format=output_format,
                        output_path=output_path,
                    )
                    if not output_path.exists() or output_path.stat().st_size <= 0:
                        raise RuntimeError("edge-tts returned an empty output file")
                    return output_format
                except Exception as exc:  # noqa: BLE001
                    err_text = str(exc)
                    errors.append(
                        f"format={output_format} attempt={attempt}/{self.synthesis_retries}: {err_text}"
                    )
                    retryable = "No audio was received" in err_text
                    if retryable and attempt < self.synthesis_retries:
                        await asyncio.sleep(self.retry_backoff_sec * attempt)
                        continue
                    break

        details = "; ".join(errors[-8:]) if errors else "unknown error"
        raise RuntimeError(f"all edge-tts synthesis attempts failed: {details}")

    def _send_to_audio_io(self, wav_path: Path) -> None:
        if not self.audio_io_play_url.strip():
            raise RuntimeError("audio_io_play_url is empty")

        payload = json.dumps({"wav_path": str(wav_path)}).encode("utf-8")
        request = Request(
            self.audio_io_play_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.request_timeout_sec) as response:
                status = int(getattr(response, "status", 200))
                body = response.read()
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
            raise RuntimeError(f"audio-io HTTP {exc.code}: {raw}") from exc
        except URLError as exc:
            raise RuntimeError(f"failed calling audio-io: {exc}") from exc

        if status >= 400:
            raise RuntimeError(f"audio-io returned HTTP {status}")

        if body:
            try:
                decoded = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict) and not bool(decoded.get("ok", True)):
                raise RuntimeError(f"audio-io error: {decoded}")

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        rate: str | None = None,
        pitch: str | None = None,
        volume: str | None = None,
        play: bool = False,
        keep_file: bool | None = None,
    ) -> dict[str, Any]:
        message = text.strip()
        if not message:
            raise ValueError("text must not be empty")

        selected_voice = (voice or self.default_voice).strip()
        selected_rate = (rate or self.default_rate).strip()
        selected_pitch = (pitch or self.default_pitch).strip()
        selected_volume = (volume or self.default_volume).strip()

        clip_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        output_path = self.output_dir / f"{clip_id}.wav"

        try:
            used_format = asyncio.run(
                self._save_audio(
                    text=message,
                    voice=selected_voice,
                    rate=selected_rate,
                    pitch=selected_pitch,
                    volume=selected_volume,
                    output_path=output_path,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            self._emit_event("tts_error", error=self._last_error)
            raise RuntimeError(f"edge-tts synthesis failed: {exc}") from exc

        is_wav = self._is_wav_file(output_path)
        file_size = output_path.stat().st_size if output_path.exists() else 0
        duration_ms = self._wav_duration_ms(output_path) if is_wav else None

        played = False
        play_error = ""
        play_path = output_path
        converted_wav_path: Path | None = None
        if play:
            if not is_wav:
                converted_wav_path = self._convert_to_wav_with_ffmpeg(output_path)
                if converted_wav_path is not None and self._is_wav_file(converted_wav_path):
                    play_path = converted_wav_path
                else:
                    play_error = (
                        "generated audio is not WAV. Set output_format to a RIFF PCM format "
                        "(for example 'riff-24khz-16bit-mono-pcm'), or install ffmpeg for auto-conversion."
                    )
            else:
                try:
                    self._send_to_audio_io(play_path)
                    played = True
                except Exception as exc:  # noqa: BLE001
                    play_error = str(exc)
            if not play_error and not played:
                try:
                    self._send_to_audio_io(play_path)
                    played = True
                except Exception as exc:  # noqa: BLE001
                    play_error = str(exc)

        retain_file = self.keep_files if keep_file is None else bool(keep_file)
        if not retain_file and output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass
        if not retain_file and converted_wav_path is not None and converted_wav_path.exists():
            try:
                converted_wav_path.unlink()
            except OSError:
                pass

        result = TTSResult(
            clip_id=clip_id,
            created_unix=time.time(),
            text=message,
            voice=selected_voice,
            rate=selected_rate,
            pitch=selected_pitch,
            volume=selected_volume,
            output_format=used_format,
            output_path=str(output_path),
            is_wav=is_wav,
            duration_ms=duration_ms,
            file_size_bytes=file_size,
            played=played,
            play_error=play_error,
        )

        with self._lock:
            self._history.append(result)

        self._emit_event(
            "tts_generated",
            clip_id=clip_id,
            play_requested=play,
            played=played,
            play_error=play_error,
            file_size_bytes=file_size,
            is_wav=is_wav,
            output_format=used_format,
        )

        if play and play_error:
            self._last_error = play_error
        elif not play_error:
            self._last_error = ""

        return self._serialize_result(result)

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history)[-limit:]
        return [self._serialize_result(item) for item in reversed(items)]

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]

    async def _list_voices_async(self) -> list[dict[str, Any]]:
        voices = await edge_tts.list_voices()
        out: list[dict[str, Any]] = []
        for voice in voices:
            out.append(
                {
                    "name": voice.get("Name", ""),
                    "short_name": voice.get("ShortName", ""),
                    "locale": voice.get("Locale", ""),
                    "gender": voice.get("Gender", ""),
                }
            )
        return out

    def list_voices(self) -> list[dict[str, Any]]:
        return asyncio.run(self._list_voices_async())

    def get_clip_path(self, clip_id: str) -> Path:
        with self._lock:
            for item in self._history:
                if item.clip_id == clip_id:
                    return Path(item.output_path)
        raise KeyError(f"clip not found: {clip_id}")

    def get_health(self) -> dict[str, Any]:
        with self._lock:
            history_count = len(self._history)
        return {
            "status": "ok" if not self._last_error else "degraded",
            "service_id": self.service_id,
            "listen": {"host": self.host, "port": self.port},
            "defaults": {
                "voice": self.default_voice,
                "rate": self.default_rate,
                "pitch": self.default_pitch,
                "volume": self.default_volume,
                "output_format": self.output_format,
            },
            "output_dir": str(self.output_dir),
            "keep_files": self.keep_files,
            "audio_io_play_url": self.audio_io_play_url,
            "synthesis_retries": self.synthesis_retries,
            "retry_backoff_sec": self.retry_backoff_sec,
            "history_count": history_count,
            "uptime_sec": round(time.time() - self._started_at, 2),
            "last_error": self._last_error,
        }

    @staticmethod
    def _serialize_result(item: TTSResult) -> dict[str, Any]:
        path = Path(item.output_path)
        return {
            "clip_id": item.clip_id,
            "created_unix": item.created_unix,
            "text": item.text,
            "voice": item.voice,
            "rate": item.rate,
            "pitch": item.pitch,
            "volume": item.volume,
            "output_format": item.output_format,
            "output_path": item.output_path,
            "output_exists": path.exists(),
            "is_wav": item.is_wav,
            "duration_ms": item.duration_ms,
            "file_size_bytes": item.file_size_bytes,
            "played": item.played,
            "play_error": item.play_error,
        }

    def serve_forever(self) -> None:
        self._server = TTSHTTPServer((self.host, self.port), EdgeTTSRequestHandler, self)
        print(f"[tts] service_id={self.service_id} listening on http://{self.host}:{self.port}")
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


class TTSHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        service: EdgeTTSService,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.service = service


class EdgeTTSRequestHandler(BaseHTTPRequestHandler):
    server: TTSHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[tts] {self.client_address[0]} - {fmt % args}")

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
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["200"])[0])
            self._json(HTTPStatus.OK, {"events": self.server.service.list_events(limit=limit)})
            return
        if path == "/clips":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["100"])[0])
            self._json(HTTPStatus.OK, {"clips": self.server.service.list_history(limit=limit)})
            return
        if path == "/voices":
            voices = self.server.service.list_voices()
            self._json(HTTPStatus.OK, {"voices": voices})
            return
        if path.startswith("/clips/") and path.endswith(".wav"):
            clip_id = path[len("/clips/") : -len(".wav")]
            self._serve_clip_wav(clip_id)
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()

        try:
            if path == "/tts/synthesize":
                result = self._handle_synthesize(body, default_play=False)
                self._json(HTTPStatus.OK, result)
                return
            if path == "/tts/speak":
                result = self._handle_synthesize(body, default_play=True)
                self._json(HTTPStatus.OK, result)
                return
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except RuntimeError as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _handle_synthesize(self, body: dict[str, Any], default_play: bool) -> dict[str, Any]:
        text = str(body.get("text", ""))
        play = self.server.service._parse_bool(body.get("play"), default_play)
        keep_file = body.get("keep_file")

        result = self.server.service.synthesize(
            text=text,
            voice=(str(body.get("voice", "")).strip() or None),
            rate=(str(body.get("rate", "")).strip() or None),
            pitch=(str(body.get("pitch", "")).strip() or None),
            volume=(str(body.get("volume", "")).strip() or None),
            play=play,
            keep_file=(None if keep_file is None else self.server.service._parse_bool(keep_file, False)),
        )
        return {"ok": True, "result": result}

    def _serve_clip_wav(self, clip_id: str) -> None:
        try:
            clip_path = self.server.service.get_clip_path(clip_id)
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return

        if not clip_path.exists():
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": f"clip file no longer exists (keep_files disabled): {clip_path}"},
            )
            return

        data = clip_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
    <title>Edge TTS Service</title>
    <style>
      body {{ background:#0e1420; color:#dbe8f5; font-family:Segoe UI, Arial, sans-serif; margin:0; padding:24px; }}
      .card {{ max-width:920px; margin:0 auto; background:#132034; border:1px solid #29435f; border-radius:10px; padding:16px; }}
      textarea {{ width:100%; min-height:82px; background:#0c1524; color:#eaf2fa; border:1px solid #29435f; border-radius:8px; padding:8px; }}
      button {{ border:0; border-radius:8px; padding:8px 12px; cursor:pointer; background:#1e8cec; color:#fff; }}
      pre {{ background:#0d1827; border:1px solid #29435f; border-radius:8px; padding:12px; max-height:260px; overflow:auto; }}
      code {{ color:#9dccff; }}
    </style>
  </head>
  <body>
    <div class=\"card\">
      <h1>Edge TTS Service</h1>
      <p><code>service_id={svc.service_id}</code></p>
      <p>
        <a href=\"/health\">/health</a> |
        <a href=\"/events\">/events</a> |
        <a href=\"/clips\">/clips</a>
      </p>
      <textarea id=\"text\">Security system online.</textarea>
      <p><button onclick=\"speak()\">Synthesize + Play</button></p>
      <pre id=\"out\">ready</pre>
    </div>
    <script>
      async function speak() {{
        const text = document.getElementById('text').value;
        const res = await fetch('/tts/speak', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ text }})
        }});
        const data = await res.json();
        document.getElementById('out').textContent = JSON.stringify(data, null, 2);
      }}
    </script>
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
    ap = argparse.ArgumentParser(description="Run Edge TTS service")
    ap.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8093, help="bind port (default: 8093)")
    ap.add_argument("--service-id", default="tts-main", help="logical service id")

    ap.add_argument("--default-voice", default="en-US-AriaNeural")
    ap.add_argument("--default-rate", default="+0%")
    ap.add_argument("--default-pitch", default="+0Hz")
    ap.add_argument("--default-volume", default="+0%")
    ap.add_argument("--output-format", default="audio-24khz-48kbitrate-mono-mp3")

    ap.add_argument("--output-dir", default="/tmp/magen-tts")
    ap.add_argument("--keep-files", action="store_true", help="keep generated files")
    ap.add_argument("--max-history", type=int, default=100)

    ap.add_argument("--audio-io-play-url", default="http://127.0.0.1:8092/speaker/play-wav")
    ap.add_argument("--request-timeout-sec", type=float, default=30.0)
    ap.add_argument("--synthesis-retries", type=int, default=3)
    ap.add_argument("--retry-backoff-sec", type=float, default=0.8)
    return ap


def main() -> None:
    args = _build_arg_parser().parse_args()

    service = EdgeTTSService(
        host=args.host,
        port=args.port,
        service_id=args.service_id,
        default_voice=args.default_voice,
        default_rate=args.default_rate,
        default_pitch=args.default_pitch,
        default_volume=args.default_volume,
        output_format=args.output_format,
        output_dir=args.output_dir,
        keep_files=args.keep_files,
        max_history=args.max_history,
        audio_io_play_url=args.audio_io_play_url,
        request_timeout_sec=args.request_timeout_sec,
        synthesis_retries=args.synthesis_retries,
        retry_backoff_sec=args.retry_backoff_sec,
    )
    service.serve_forever()


if __name__ == "__main__":
    main()
