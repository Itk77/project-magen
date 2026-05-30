#!/usr/bin/env python3
"""Pluggable Kokoro TTS service provider.

Synthesizes speech with kokoro-onnx and can forward WAV output to the local
audio I/O service for speaker playback.
"""

from __future__ import annotations

import argparse
import collections
import json
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

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency 'numpy'. Install with: pip install numpy") from exc

try:
    from kokoro_onnx import Kokoro
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency 'kokoro-onnx'. Install with: pip install kokoro-onnx") from exc


@dataclass(slots=True)
class TTSResult:
    clip_id: str
    created_unix: float
    text: str
    voice: str
    lang: str
    speed: float
    output_path: str
    duration_ms: int
    sample_rate: int
    channels: int
    file_size_bytes: int
    played: bool
    play_error: str


class KokoroTTSService:
    """Synthesize speech with kokoro-onnx and optionally play via audio-io."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8093,
        service_id: str = "tts-main",
        model_path: str = "kokoro-v1.0.onnx",
        voices_path: str = "voices-v1.0.bin",
        default_voice: str = "af_sarah",
        default_lang: str = "en-us",
        default_speed: float = 1.0,
        output_dir: str = "/tmp/magen-tts",
        keep_files: bool = False,
        max_history: int = 100,
        audio_io_play_url: str = "http://127.0.0.1:8092/speaker/play-wav",
        request_timeout_sec: float = 30.0,
    ) -> None:
        if port <= 0:
            raise ValueError("port must be positive")
        if max_history <= 0:
            raise ValueError("max_history must be positive")
        if request_timeout_sec <= 0:
            raise ValueError("request_timeout_sec must be positive")

        self.host = host
        self.port = int(port)
        self.service_id = service_id

        self.model_path = str(Path(model_path).expanduser().resolve())
        self.voices_path = str(Path(voices_path).expanduser().resolve())

        self.default_voice = default_voice
        self.default_lang = default_lang
        self.default_speed = float(default_speed)

        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.keep_files = bool(keep_files)
        self.audio_io_play_url = audio_io_play_url
        self.request_timeout_sec = float(request_timeout_sec)

        self._kokoro: Kokoro | None = None
        self._history: collections.deque[TTSResult] = collections.deque(maxlen=max_history)
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=500)
        self._lock = threading.Lock()

        self._server: TTSHTTPServer | None = None
        self._started_at = time.time()
        self._last_error = ""

    def _get_engine(self) -> Kokoro:
        if self._kokoro is not None:
            return self._kokoro

        model_file = Path(self.model_path)
        voices_file = Path(self.voices_path)
        if not model_file.exists():
            raise FileNotFoundError(f"Kokoro model not found: {model_file}")
        if not voices_file.exists():
            raise FileNotFoundError(f"Kokoro voices file not found: {voices_file}")

        self._kokoro = Kokoro(self.model_path, self.voices_path)
        return self._kokoro

    @staticmethod
    def _parse_bool(raw: Any, default: bool) -> bool:
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)

    @staticmethod
    def _float32_to_pcm16(audio: np.ndarray) -> bytes:
        x = np.asarray(audio)
        if x.dtype == np.int16:
            return x.tobytes()

        f = x.astype(np.float32, copy=False)
        max_abs = float(np.max(np.abs(f))) if f.size else 0.0
        if max_abs > 1.0:
            f = f / max(max_abs, 1e-6)
        f = np.clip(f, -1.0, 1.0)
        return (f * 32767.0).astype(np.int16).tobytes()

    @staticmethod
    def _write_wav(path: Path, pcm16: bytes, sample_rate: int, channels: int) -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16)

    @staticmethod
    def _duration_ms(samples_count: int, sample_rate: int) -> int:
        if sample_rate <= 0:
            return 0
        return int((samples_count / sample_rate) * 1000.0)

    def _emit_event(self, event_type: str, **data: Any) -> None:
        evt = {
            "id": len(self._events) + 1,
            "timestamp": time.time(),
            "event": event_type,
            **data,
        }
        with self._lock:
            self._events.append(evt)

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

    def _synthesize_kokoro(
        self,
        text: str,
        voice: str,
        lang: str,
        speed: float,
    ) -> tuple[np.ndarray, int, int]:
        engine = self._get_engine()

        result = engine.create(text=text, voice=voice, speed=float(speed), lang=lang)

        if isinstance(result, tuple) and len(result) >= 2:
            audio = np.asarray(result[0])
            sample_rate = int(result[1])
        elif isinstance(result, dict):
            audio = np.asarray(result.get("audio") or result.get("samples"))
            sample_rate = int(result.get("sample_rate", 24000))
        else:
            raise RuntimeError("unexpected Kokoro output format")

        if audio.size == 0:
            raise RuntimeError("Kokoro returned empty audio")

        if audio.ndim == 1:
            channels = 1
        elif audio.ndim == 2:
            # normalize to (samples, channels)
            if audio.shape[0] <= 8 and audio.shape[0] < audio.shape[1]:
                audio = audio.T
            channels = int(audio.shape[1])
        else:
            audio = audio.reshape(-1)
            channels = 1

        return audio, sample_rate, channels

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        lang: str | None = None,
        speed: float | None = None,
        play: bool = False,
        keep_file: bool | None = None,
    ) -> dict[str, Any]:
        message = text.strip()
        if not message:
            raise ValueError("text must not be empty")

        selected_voice = (voice or self.default_voice).strip()
        selected_lang = (lang or self.default_lang).strip()
        selected_speed = float(self.default_speed if speed is None else speed)

        clip_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        output_path = self.output_dir / f"{clip_id}.wav"

        try:
            audio, sample_rate, channels = self._synthesize_kokoro(
                text=message,
                voice=selected_voice,
                lang=selected_lang,
                speed=selected_speed,
            )
            pcm16 = self._float32_to_pcm16(audio)
            self._write_wav(output_path, pcm16, sample_rate=sample_rate, channels=channels)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            self._emit_event("tts_error", error=self._last_error)
            raise RuntimeError(f"kokoro synthesis failed: {exc}") from exc

        sample_count = int(audio.shape[0]) if audio.ndim > 1 else int(audio.size)
        duration_ms = self._duration_ms(sample_count, sample_rate)
        file_size = output_path.stat().st_size if output_path.exists() else 0

        played = False
        play_error = ""
        if play:
            try:
                self._send_to_audio_io(output_path)
                played = True
            except Exception as exc:  # noqa: BLE001
                play_error = str(exc)

        retain_file = self.keep_files if keep_file is None else bool(keep_file)
        if not retain_file and output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass

        result = TTSResult(
            clip_id=clip_id,
            created_unix=time.time(),
            text=message,
            voice=selected_voice,
            lang=selected_lang,
            speed=selected_speed,
            output_path=str(output_path),
            duration_ms=duration_ms,
            sample_rate=sample_rate,
            channels=channels,
            file_size_bytes=file_size,
            played=played,
            play_error=play_error,
        )

        with self._lock:
            self._history.append(result)

        self._emit_event(
            "tts_generated",
            clip_id=clip_id,
            engine="kokoro",
            played=played,
            play_error=play_error,
            duration_ms=duration_ms,
            file_size_bytes=file_size,
            sample_rate=sample_rate,
            channels=channels,
        )

        if play and play_error:
            self._last_error = play_error

        return self._serialize_result(result)

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history)[-limit:]
        return [self._serialize_result(item) for item in reversed(items)]

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]

    def list_voices(self) -> list[dict[str, Any]]:
        voices: list[dict[str, Any]] = []
        try:
            engine = self._get_engine()
        except Exception as exc:  # noqa: BLE001
            return [{"voice": self.default_voice, "note": f"voice list unavailable: {exc}"}]

        raw: Any = None
        if hasattr(engine, "get_voices"):
            try:
                raw = engine.get_voices()
            except Exception:  # noqa: BLE001
                raw = None
        if raw is None and hasattr(engine, "voices"):
            raw = getattr(engine, "voices")

        if isinstance(raw, dict):
            for key, value in raw.items():
                voices.append({"voice": str(key), "meta": value})
        elif isinstance(raw, (list, tuple, set)):
            for item in raw:
                if isinstance(item, str):
                    voices.append({"voice": item})
                elif isinstance(item, dict):
                    voice_name = str(item.get("voice", item.get("name", ""))).strip()
                    entry = {"voice": voice_name or str(item)}
                    entry.update(item)
                    voices.append(entry)
                else:
                    voices.append({"voice": str(item)})

        if not voices:
            voices = [{"voice": self.default_voice, "note": "voice list not exposed by kokoro backend"}]
        return voices

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
            "engine": "kokoro",
            "listen": {"host": self.host, "port": self.port},
            "defaults": {
                "voice": self.default_voice,
                "lang": self.default_lang,
                "speed": self.default_speed,
            },
            "model_path": self.model_path,
            "voices_path": self.voices_path,
            "output_dir": str(self.output_dir),
            "keep_files": self.keep_files,
            "audio_io_play_url": self.audio_io_play_url,
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
            "lang": item.lang,
            "speed": item.speed,
            "output_path": item.output_path,
            "output_exists": path.exists(),
            "duration_ms": item.duration_ms,
            "sample_rate": item.sample_rate,
            "channels": item.channels,
            "file_size_bytes": item.file_size_bytes,
            "played": item.played,
            "play_error": item.play_error,
        }

    def serve_forever(self) -> None:
        self._server = TTSHTTPServer((self.host, self.port), KokoroTTSRequestHandler, self)
        print(
            f"[tts] service_id={self.service_id} engine=kokoro "
            f"listening on http://{self.host}:{self.port}"
        )
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
        service: KokoroTTSService,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.service = service


class KokoroTTSRequestHandler(BaseHTTPRequestHandler):
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
            self._json(HTTPStatus.OK, {"voices": self.server.service.list_voices()})
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
        speed_raw = body.get("speed")
        speed = None if speed_raw is None else float(speed_raw)

        result = self.server.service.synthesize(
            text=text,
            voice=(str(body.get("voice", "")).strip() or None),
            lang=(str(body.get("lang", "")).strip() or None),
            speed=speed,
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
    <title>Kokoro TTS Service</title>
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
      <h1>Kokoro TTS Service</h1>
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
    ap = argparse.ArgumentParser(description="Run Kokoro TTS service")
    ap.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8093, help="bind port (default: 8093)")
    ap.add_argument("--service-id", default="tts-main", help="logical service id")

    ap.add_argument("--model-path", default="kokoro-v1.0.onnx")
    ap.add_argument("--voices-path", default="voices-v1.0.bin")
    ap.add_argument("--default-voice", default="af_sarah")
    ap.add_argument("--default-lang", default="en-us")
    ap.add_argument("--default-speed", type=float, default=1.0)

    ap.add_argument("--output-dir", default="/tmp/magen-tts")
    ap.add_argument("--keep-files", action="store_true", help="keep generated files")
    ap.add_argument("--max-history", type=int, default=100)

    ap.add_argument("--audio-io-play-url", default="http://127.0.0.1:8092/speaker/play-wav")
    ap.add_argument("--request-timeout-sec", type=float, default=30.0)
    return ap


def main() -> None:
    args = _build_arg_parser().parse_args()

    service = KokoroTTSService(
        host=args.host,
        port=args.port,
        service_id=args.service_id,
        model_path=args.model_path,
        voices_path=args.voices_path,
        default_voice=args.default_voice,
        default_lang=args.default_lang,
        default_speed=args.default_speed,
        output_dir=args.output_dir,
        keep_files=args.keep_files,
        max_history=args.max_history,
        audio_io_play_url=args.audio_io_play_url,
        request_timeout_sec=args.request_timeout_sec,
    )
    service.serve_forever()


if __name__ == "__main__":
    main()
