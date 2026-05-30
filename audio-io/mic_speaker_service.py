#!/usr/bin/env python3
"""Mic + speaker service for manual recording and local playback.

Features:
- Continuous microphone capture
- Button/manual recording into WAV segments exposed over HTTP
- Speaker test endpoints (tone and WAV file playback)
"""

from __future__ import annotations

import asyncio
import argparse
import base64
import collections
import io
import json
import math
import queue
import threading
import time
import wave
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np

try:
    import sounddevice as sd
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency 'sounddevice'. Install with: pip install sounddevice") from exc
except OSError as exc:  # pragma: no cover
    raise SystemExit(
        "PortAudio library not found for 'sounddevice'. "
        "On Debian/Raspbian/Ubuntu install system packages, e.g.: "
        "sudo apt update && sudo apt install -y libportaudio2 portaudio19-dev"
    ) from exc

try:
    import websockets
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency 'websockets'. Install with: pip install websockets") from exc


@dataclass(slots=True)
class AudioSegment:
    segment_id: int
    started_unix: float
    ended_unix: float
    duration_ms: int
    sample_rate: int
    channels: int
    peak_dbfs: float
    rms_dbfs: float
    pcm16: bytes


class MicSpeakerService:
    """Mic + speaker service with manual recording and local playback helpers."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8092,
        service_id: str = "mic-speaker-1",
        input_device: str | None = None,
        output_device: str | None = None,
        sample_rate: int = 16000,
        chunk_samples: int = 512,
        max_segments: int = 50,
        ws_enabled: bool = True,
        ws_host: str = "0.0.0.0",
        ws_port: int = 8095,
    ) -> None:
        if sample_rate not in (8000, 16000):
            raise ValueError("sample_rate must be 8000 or 16000")
        if chunk_samples <= 0:
            raise ValueError("chunk_samples must be positive")
        if ws_enabled and ws_port <= 0:
            raise ValueError("ws_port must be positive")

        self.host = host
        self.port = int(port)
        self.service_id = service_id
        self.sample_rate = int(sample_rate)
        self.chunk_samples = int(chunk_samples)

        self.ws_enabled = bool(ws_enabled)
        self.ws_host = ws_host
        self.ws_port = int(ws_port)

        self.input_device = self._parse_device(input_device)
        self.output_device = self._parse_device(output_device)

        self._stop_event = threading.Event()
        self._mic_thread: threading.Thread | None = None
        self._server: MicSpeakerHTTPServer | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_server: Any = None
        self._ws_clients: dict[Any, bool] = {}
        self._ws_audio_clients: set[Any] = set()
        self._busy_tone_thread: threading.Thread | None = None
        self._busy_tone_stop = threading.Event()
        self._busy_tone_lock = threading.Lock()
        self._speaker_lock = threading.RLock()
        self._speaker_stream_lock = threading.RLock()
        self._speaker_stream: Any = None
        self._speaker_stream_format: tuple[int, int] | None = None
        self._speaker_stream_chunks: collections.deque[np.ndarray] = collections.deque()
        self._speaker_stream_buffer: np.ndarray | None = None
        self._speaker_stream_queued_frames = 0

        self._chunk_ms = (self.chunk_samples / self.sample_rate) * 1000.0

        self._manual_recording = False
        self._current_segment_chunks: list[bytes] = []
        self._current_segment_started = 0.0

        self._segments: collections.deque[AudioSegment] = collections.deque(maxlen=max_segments)
        self._next_segment_id = 1
        self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=500)
        self._next_event_id = 1
        self._event_subscribers: set[queue.Queue[dict[str, Any]]] = set()

        self._last_error = ""
        self._started_at = time.time()

        self._lock = threading.Lock()

    @staticmethod
    def _parse_device(raw: str | None) -> str | int | None:
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except ValueError:
            return raw

    @staticmethod
    def _peak_dbfs_from_pcm16(pcm16: bytes) -> float:
        x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        peak = float(np.max(np.abs(x)) + 1e-12)
        return 20.0 * math.log10(peak)

    @staticmethod
    def _rms_dbfs_from_pcm16(pcm16: bytes) -> float:
        x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x) + 1e-12))
        return 20.0 * math.log10(rms + 1e-12)

    @staticmethod
    def _pcm16_to_wav_bytes(pcm16: bytes, sample_rate: int, channels: int = 1) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16)
        return buf.getvalue()

    def _emit_event(self, event_type: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            evt = {
                "id": self._next_event_id,
                "timestamp": time.time(),
                "event": event_type,
                **data,
            }
            self._next_event_id += 1
            self._events.append(evt)
            subscribers = list(self._event_subscribers)

        for q in subscribers:
            try:
                q.put_nowait(evt)
            except queue.Full:
                pass
        self._ws_broadcast_event(evt)
        return evt

    def _ws_broadcast_event(self, evt: dict[str, Any]) -> None:
        if not self.ws_enabled:
            return
        loop = self._ws_loop
        if loop is None:
            return
        payload = {"type": "event", "event": evt}
        raw = json.dumps(payload)
        try:
            loop.call_soon_threadsafe(asyncio.create_task, self._ws_broadcast_raw(raw))
        except RuntimeError:
            return

    async def _ws_broadcast_raw(self, raw: str) -> None:
        stale: list[Any] = []
        for ws, subscribed in list(self._ws_clients.items()):
            if not subscribed:
                continue
            try:
                await ws.send(raw)
            except Exception:  # noqa: BLE001
                stale.append(ws)
        for ws in stale:
            self._ws_clients.pop(ws, None)
            self._ws_audio_clients.discard(ws)

    def _ws_broadcast_audio(self, pcm16: bytes) -> None:
        if not self.ws_enabled:
            return
        loop = self._ws_loop
        if loop is None or not self._ws_audio_clients:
            return
        payload = {
            "type": "audio_chunk",
            "sample_rate": self.sample_rate,
            "channels": 1,
            "pcm16_b64": base64.b64encode(pcm16).decode("ascii"),
        }
        raw = json.dumps(payload)
        try:
            loop.call_soon_threadsafe(asyncio.create_task, self._ws_broadcast_audio_raw(raw))
        except RuntimeError:
            return

    async def _ws_broadcast_audio_raw(self, raw: str) -> None:
        stale: list[Any] = []
        for ws in list(self._ws_audio_clients):
            try:
                await ws.send(raw)
            except Exception:  # noqa: BLE001
                stale.append(ws)
        for ws in stale:
            self._ws_audio_clients.discard(ws)
            self._ws_clients.pop(ws, None)

    async def _ws_send_response(self, ws: Any, payload: dict[str, Any]) -> None:
        await ws.send(json.dumps(payload))

    async def _ws_handle_message(self, ws: Any, payload: dict[str, Any]) -> dict[str, Any]:
        msg_type = str(payload.get("type", "")).strip()
        request_id = payload.get("request_id")

        def response(ok: bool, **kwargs: Any) -> dict[str, Any]:
            out: dict[str, Any] = {
                "type": "response",
                "ok": ok,
            }
            if request_id is not None:
                out["request_id"] = request_id
            out.update(kwargs)
            return out

        try:
            if msg_type == "ping":
                return response(True, result={"pong": True, "unix": time.time()})

            if msg_type == "subscribe_events":
                self._ws_clients[ws] = True
                return response(True, result={"subscribed": True})

            if msg_type == "unsubscribe_events":
                self._ws_clients[ws] = False
                return response(True, result={"subscribed": False})

            if msg_type == "subscribe_audio":
                self._ws_audio_clients.add(ws)
                return response(
                    True,
                    result={
                        "subscribed_audio": True,
                        "sample_rate": self.sample_rate,
                        "channels": 1,
                    },
                )

            if msg_type == "unsubscribe_audio":
                self._ws_audio_clients.discard(ws)
                return response(True, result={"subscribed_audio": False})

            if msg_type == "get_health":
                health = await asyncio.to_thread(self.get_health)
                return response(True, result=health)

            if msg_type == "get_segment_wav":
                segment_id = int(payload.get("segment_id", 0))
                if segment_id <= 0:
                    return response(False, error="segment_id must be positive")
                wav_bytes = await asyncio.to_thread(self.get_segment_wav, segment_id)
                return response(
                    True,
                    result={
                        "segment_id": segment_id,
                        "wav_b64": base64.b64encode(wav_bytes).decode("ascii"),
                    },
                )

            if msg_type == "speaker_busy_start":
                await asyncio.to_thread(
                    self.speaker_busy_start,
                    float(payload.get("frequency_hz", 880.0)),
                    int(payload.get("duration_ms", 140)),
                    float(payload.get("interval_sec", 0.9)),
                    float(payload.get("volume", 0.25)),
                )
                return response(True, result={"started": True})

            if msg_type == "speaker_busy_stop":
                await asyncio.to_thread(self.speaker_busy_stop)
                return response(True, result={"stopped": True})

            if msg_type == "speaker_tone":
                await asyncio.to_thread(
                    self.speaker_play_tone,
                    float(payload.get("frequency_hz", 880.0)),
                    int(payload.get("duration_ms", 300)),
                    float(payload.get("volume", 0.2)),
                    True,
                )
                return response(True, result={"played": True})

            if msg_type == "speaker_pcm16":
                pcm_b64 = str(payload.get("pcm16_b64", "")).strip()
                if not pcm_b64:
                    return response(False, error="pcm16_b64 is required")
                pcm16 = base64.b64decode(pcm_b64)
                queued = await asyncio.to_thread(
                    self.speaker_queue_pcm16,
                    pcm16,
                    int(payload.get("sample_rate", self.sample_rate)),
                    int(payload.get("channels", 1)),
                    float(payload.get("volume", 1.0)),
                )
                return response(True, result={"queued": True, "bytes": len(pcm16), "queued_frames": queued})

            if msg_type == "speaker_pcm16_stop":
                await asyncio.to_thread(self._speaker_stream_stop)
                return response(True, result={"stopped": True})

            return response(False, error=f"unsupported message type: {msg_type!r}")
        except Exception as exc:  # noqa: BLE001
            return response(False, error=str(exc))

    async def _ws_handler(self, ws: Any) -> None:
        self._ws_clients[ws] = False
        try:
            await self._ws_send_response(
                ws,
                {
                    "type": "welcome",
                    "ok": True,
                    "service_id": self.service_id,
                },
            )
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await self._ws_send_response(ws, {"type": "response", "ok": False, "error": "invalid JSON"})
                    continue
                if not isinstance(payload, dict):
                    await self._ws_send_response(
                        ws,
                        {"type": "response", "ok": False, "error": "message must be a JSON object"},
                    )
                    continue
                response = await self._ws_handle_message(ws, payload)
                await self._ws_send_response(ws, response)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._ws_clients.pop(ws, None)
            self._ws_audio_clients.discard(ws)

    async def _ws_shutdown_async(self) -> None:
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws_clients.clear()
        self._ws_audio_clients.clear()
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None

    def _ws_run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._ws_loop = loop
        asyncio.set_event_loop(loop)
        try:
            async def _start_ws_server() -> Any:
                return await websockets.serve(
                    self._ws_handler,
                    self.ws_host,
                    self.ws_port,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=8 * 1024 * 1024,
                )

            self._ws_server = loop.run_until_complete(_start_ws_server())
            self._emit_event("ws_started", host=self.ws_host, port=self.ws_port)
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._ws_shutdown_async())
            except Exception:  # noqa: BLE001
                pass
            self._ws_loop = None
            loop.close()

    def _ws_start(self) -> None:
        if not self.ws_enabled:
            return
        if self._ws_thread is not None:
            return
        self._ws_thread = threading.Thread(target=self._ws_run_loop, daemon=True)
        self._ws_thread.start()

    def _ws_stop(self) -> None:
        if self._ws_thread is None:
            return
        loop = self._ws_loop
        if loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._ws_shutdown_async(), loop)
                fut.result(timeout=2.5)
            except Exception:  # noqa: BLE001
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
        self._ws_thread.join(timeout=2.5)
        self._ws_thread = None

    def _segment_finalize(self, ended_unix: float) -> AudioSegment | None:
        if not self._current_segment_chunks:
            return None

        pcm16 = b"".join(self._current_segment_chunks)
        duration_ms = int((len(pcm16) / 2 / self.sample_rate) * 1000.0)

        segment = AudioSegment(
            segment_id=self._next_segment_id,
            started_unix=self._current_segment_started,
            ended_unix=ended_unix,
            duration_ms=duration_ms,
            sample_rate=self.sample_rate,
            channels=1,
            peak_dbfs=round(self._peak_dbfs_from_pcm16(pcm16), 2),
            rms_dbfs=round(self._rms_dbfs_from_pcm16(pcm16), 2),
            pcm16=pcm16,
        )
        self._next_segment_id += 1

        with self._lock:
            self._segments.append(segment)

        self._emit_event(
            "segment_saved",
            segment_id=segment.segment_id,
            duration_ms=segment.duration_ms,
            peak_dbfs=segment.peak_dbfs,
            rms_dbfs=segment.rms_dbfs,
        )

        self._current_segment_chunks = []
        return segment

    def manual_record_start(self) -> dict[str, Any]:
        with self._lock:
            if self._manual_recording:
                return {"ok": True, "recording": True, "already_recording": True}
            self._manual_recording = True
            self._current_segment_started = time.time()
            self._current_segment_chunks = []
        self._emit_event("manual_record_start")
        return {"ok": True, "recording": True}

    def manual_record_stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._manual_recording:
                return {"ok": True, "recording": False, "segment": None}
            self._manual_recording = False
        ended = time.time()
        segment = self._segment_finalize(ended)
        self._emit_event(
            "manual_record_stop",
            segment_id=segment.segment_id if segment else None,
            duration_ms=segment.duration_ms if segment else 0,
        )
        return {
            "ok": True,
            "recording": False,
            "segment": None
            if segment is None
            else {
                "segment_id": segment.segment_id,
                "duration_ms": segment.duration_ms,
                "started_unix": segment.started_unix,
                "ended_unix": segment.ended_unix,
                "peak_dbfs": segment.peak_dbfs,
                "rms_dbfs": segment.rms_dbfs,
            },
        }

    def _mic_loop(self) -> None:
        self._emit_event("mic_started", sample_rate=self.sample_rate, chunk_samples=self.chunk_samples)

        try:
            with sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.chunk_samples,
                dtype="int16",
                channels=1,
                device=self.input_device,
            ) as stream:
                while not self._stop_event.is_set():
                    data, overflowed = stream.read(self.chunk_samples)
                    pcm = bytes(data)
                    self._ws_broadcast_audio(pcm)

                    with self._lock:
                        manual_recording = self._manual_recording
                        if manual_recording:
                            self._current_segment_chunks.append(pcm)

                    if overflowed:
                        self._emit_event("mic_overflow")

        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            self._emit_event("mic_error", error=self._last_error)

    def _speaker_play_float32(self, audio: np.ndarray, sample_rate: int) -> None:
        with self._speaker_lock:
            # PortAudio/sounddevice isn't safe to drive concurrently from the
            # HTTP handler threads and the busy-tone worker. Serialize all
            # speaker playback so streams don't overlap or tear each other down.
            self._speaker_stream_stop()
            sd.stop()
            sd.play(audio, samplerate=sample_rate, device=self.output_device, blocking=True)
            sd.stop()

    def _speaker_stream_callback(self, outdata: np.ndarray, frames: int, _time_info: Any, status: Any) -> None:
        if status:
            self._last_error = str(status)

        with self._speaker_stream_lock:
            channels = int(outdata.shape[1])
            if self._speaker_stream_buffer is None:
                self._speaker_stream_buffer = np.empty((0, channels), dtype=np.float32)

            while self._speaker_stream_buffer.shape[0] < frames and self._speaker_stream_chunks:
                chunk = self._speaker_stream_chunks.popleft()
                self._speaker_stream_queued_frames = max(0, self._speaker_stream_queued_frames - chunk.shape[0])
                if self._speaker_stream_buffer.size:
                    self._speaker_stream_buffer = np.concatenate((self._speaker_stream_buffer, chunk), axis=0)
                else:
                    self._speaker_stream_buffer = chunk

            available = min(frames, self._speaker_stream_buffer.shape[0])
            if available > 0:
                outdata[:available] = self._speaker_stream_buffer[:available]
                self._speaker_stream_buffer = self._speaker_stream_buffer[available:]
            if available < frames:
                outdata[available:] = 0.0

    def _speaker_stream_close(self, stream: Any) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()

    def _speaker_stream_take_locked(self) -> Any:
        stream = self._speaker_stream
        self._speaker_stream = None
        self._speaker_stream_format = None
        self._speaker_stream_chunks.clear()
        self._speaker_stream_buffer = None
        self._speaker_stream_queued_frames = 0
        return stream

    def _speaker_stream_start(self, sample_rate: int, channels: int) -> None:
        fmt = (int(sample_rate), int(channels))
        stream_to_close: Any = None
        with self._speaker_stream_lock:
            if self._speaker_stream is not None and self._speaker_stream_format == fmt:
                return
            stream_to_close = self._speaker_stream_take_locked()

        self._speaker_stream_close(stream_to_close)

        stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="float32",
            device=self.output_device,
            blocksize=0,
            callback=self._speaker_stream_callback,
        )
        with self._speaker_stream_lock:
            self._speaker_stream_buffer = np.empty((0, channels), dtype=np.float32)
            self._speaker_stream_chunks.clear()
            self._speaker_stream_queued_frames = 0
            self._speaker_stream = stream
            self._speaker_stream_format = fmt
        stream.start()

    def _speaker_stream_stop(self) -> None:
        with self._speaker_stream_lock:
            stream = self._speaker_stream_take_locked()
        self._speaker_stream_close(stream)

    def _busy_tone_loop(
        self,
        *,
        frequency_hz: float,
        duration_ms: int,
        interval_sec: float,
        volume: float,
    ) -> None:
        while not self._busy_tone_stop.is_set():
            try:
                self.speaker_play_tone(
                    frequency_hz=frequency_hz,
                    duration_ms=duration_ms,
                    volume=volume,
                    emit_event=False,
                )
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                self._emit_event("speaker_busy_error", error=self._last_error)
                break
            self._busy_tone_stop.wait(max(0.03, interval_sec))

    def speaker_busy_start(
        self,
        frequency_hz: float = 880.0,
        duration_ms: int = 140,
        interval_sec: float = 0.9,
        volume: float = 0.25,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("interval_sec must be positive")

        # Restart on repeated start so caller can update parameters without a stale loop.
        self.speaker_busy_stop()

        with self._busy_tone_lock:
            self._busy_tone_stop.clear()
            self._busy_tone_thread = threading.Thread(
                target=self._busy_tone_loop,
                kwargs={
                    "frequency_hz": frequency_hz,
                    "duration_ms": duration_ms,
                    "interval_sec": interval_sec,
                    "volume": volume,
                },
                daemon=True,
            )
            self._busy_tone_thread.start()

        self._emit_event(
            "speaker_busy_start",
            frequency_hz=frequency_hz,
            duration_ms=duration_ms,
            interval_sec=interval_sec,
            volume=volume,
        )

    def speaker_busy_stop(self) -> None:
        with self._busy_tone_lock:
            thread = self._busy_tone_thread
            if thread is None:
                return
            self._busy_tone_stop.set()
            self._busy_tone_thread = None

        thread.join(timeout=1.2)
        self._busy_tone_stop.clear()
        self._emit_event("speaker_busy_stop")

    def speaker_play_tone(
        self,
        frequency_hz: float = 880.0,
        duration_ms: int = 400,
        volume: float = 0.2,
        emit_event: bool = True,
    ) -> None:
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        if frequency_hz <= 0:
            raise ValueError("frequency_hz must be positive")
        if not (0.0 < volume <= 1.0):
            raise ValueError("volume must be in (0,1]")

        n = int(self.sample_rate * (duration_ms / 1000.0))
        t = np.arange(n, dtype=np.float32) / self.sample_rate
        audio = (volume * np.sin(2.0 * np.pi * frequency_hz * t)).astype(np.float32)

        self._speaker_play_float32(audio, self.sample_rate)
        if emit_event:
            self._emit_event("speaker_tone", frequency_hz=frequency_hz, duration_ms=duration_ms, volume=volume)

    def speaker_play_wav_path(self, wav_path: str) -> None:
        path = Path(wav_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"wav file not found: {path}")

        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())

        if sampwidth != 2:
            raise ValueError("only 16-bit PCM WAV is supported")
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels == 2:
            audio = audio.reshape(-1, 2)

        self._speaker_play_float32(audio, sample_rate)
        self._emit_event("speaker_wav", path=str(path), sample_rate=sample_rate, channels=channels)

    def speaker_queue_pcm16(
        self,
        pcm16: bytes,
        sample_rate: int,
        channels: int = 1,
        volume: float = 1.0,
    ) -> int:
        if not pcm16:
            raise ValueError("pcm16 audio is empty")
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if channels not in (1, 2):
            raise ValueError("channels must be 1 or 2")
        if not (0.0 < volume <= 2.0):
            raise ValueError("volume must be in (0,2]")

        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if channels == 2:
            usable = (audio.size // 2) * 2
            audio = audio[:usable].reshape(-1, 2)
        else:
            audio = audio.reshape(-1, 1)
        audio = np.clip(audio * float(volume), -1.0, 1.0)
        audio = np.ascontiguousarray(audio, dtype=np.float32)

        self._speaker_stream_start(int(sample_rate), int(channels))

        with self._speaker_stream_lock:
            max_queue_frames = max(int(sample_rate * 0.45), audio.shape[0] * 2)
            while (
                self._speaker_stream_queued_frames + audio.shape[0] > max_queue_frames
                and self._speaker_stream_chunks
            ):
                dropped = self._speaker_stream_chunks.popleft()
                self._speaker_stream_queued_frames = max(0, self._speaker_stream_queued_frames - dropped.shape[0])
            self._speaker_stream_chunks.append(audio)
            self._speaker_stream_queued_frames += audio.shape[0]
            return self._speaker_stream_queued_frames

    def get_health(self) -> dict[str, Any]:
        with self._lock:
            segment_count = len(self._segments)
            manual_recording = self._manual_recording

        return {
            "status": "ok" if not self._last_error else "degraded",
            "service_id": self.service_id,
            "listen": {"host": self.host, "port": self.port},
            "sample_rate": self.sample_rate,
            "chunk_samples": self.chunk_samples,
            "chunk_ms": round(self._chunk_ms, 2),
            "mic": {
                "input_device": self.input_device,
                "manual_recording": manual_recording,
            },
            "speaker": {
                "output_device": self.output_device,
                "busy_tone_active": self._busy_tone_thread is not None,
                "stream_active": self._speaker_stream is not None,
                "stream_queued_frames": self._speaker_stream_queued_frames,
            },
            "ws": {
                "enabled": self.ws_enabled,
                "listen": {
                    "host": self.ws_host,
                    "port": self.ws_port,
                    "url": f"ws://{self.ws_host}:{self.ws_port}",
                },
                "connected_clients": len(self._ws_clients),
                "audio_clients": len(self._ws_audio_clients),
            },
            "segments_count": segment_count,
            "uptime_sec": round(time.time() - self._started_at, 2),
            "last_error": self._last_error,
        }

    @staticmethod
    def list_devices() -> list[dict[str, Any]]:
        devices = sd.query_devices()
        out: list[dict[str, Any]] = []
        for idx, d in enumerate(devices):
            item = dict(d)
            item["index"] = idx
            out.append(item)
        return out

    def list_segments(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._segments)

        return [
            {
                "segment_id": s.segment_id,
                "started_unix": s.started_unix,
                "ended_unix": s.ended_unix,
                "duration_ms": s.duration_ms,
                "sample_rate": s.sample_rate,
                "channels": s.channels,
                "peak_dbfs": s.peak_dbfs,
                "rms_dbfs": s.rms_dbfs,
            }
            for s in reversed(items)
        ]

    def get_segment_wav(self, segment_id: int) -> bytes:
        with self._lock:
            for s in self._segments:
                if s.segment_id == segment_id:
                    return self._pcm16_to_wav_bytes(s.pcm16, s.sample_rate, s.channels)
        raise KeyError(f"segment {segment_id} not found")

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]

    def subscribe_events(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        with self._lock:
            self._event_subscribers.add(q)
        return q

    def unsubscribe_events(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._event_subscribers.discard(q)

    def serve_forever(self) -> None:
        self._stop_event.clear()

        self._mic_thread = threading.Thread(target=self._mic_loop, daemon=True)
        self._mic_thread.start()
        self._ws_start()

        self._server = MicSpeakerHTTPServer((self.host, self.port), MicSpeakerHandler, self)
        print(
            f"[audio-io] service_id={self.service_id} listening on "
            f"http://{self.host}:{self.port}"
        )
        try:
            self._server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.speaker_busy_stop()
        self._speaker_stream_stop()
        self._ws_stop()
        self._stop_event.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._mic_thread is not None:
            self._mic_thread.join(timeout=1.5)


class MicSpeakerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        service: MicSpeakerService,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.service = service


class MicSpeakerHandler(BaseHTTPRequestHandler):
    server: MicSpeakerHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[audio-io] {self.client_address[0]} - {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in {"/", "/index.html"}:
            self._json(
                HTTPStatus.OK,
                {
                    "service": "mic-speaker",
                    "service_id": self.server.service.service_id,
                    "note": "No web UI. Use terminal logs and API endpoints.",
                    "endpoints": [
                        "/health",
                        "/devices",
                        "/events",
                        "/stream/events",
                        "/segments",
                        "/segments/{id}.wav",
                        "/speaker/tone",
                        "/speaker/busy/start",
                        "/speaker/busy/stop",
                        "/speaker/play-wav",
                        "ws:speaker_pcm16",
                        "ws:speaker_pcm16_stop",
                        "/mic/record/start",
                        "/mic/record/stop",
                        f"ws://{self.server.service.ws_host}:{self.server.service.ws_port}",
                    ],
                },
            )
            return
        if path == "/health":
            self._json(HTTPStatus.OK, self.server.service.get_health())
            return
        if path == "/events":
            self._serve_events()
            return
        if path == "/devices":
            self._json(HTTPStatus.OK, {"devices": self.server.service.list_devices()})
            return
        if path == "/stream/events":
            self._stream_events()
            return
        if path == "/segments":
            self._json(HTTPStatus.OK, {"segments": self.server.service.list_segments()})
            return
        if path.startswith("/segments/") and path.endswith(".wav"):
            self._serve_segment_wav(path)
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()

        try:
            if path == "/speaker/tone":
                self.server.service.speaker_play_tone(
                    frequency_hz=float(body.get("frequency_hz", 880.0)),
                    duration_ms=int(body.get("duration_ms", 400)),
                    volume=float(body.get("volume", 0.2)),
                )
                self._json(HTTPStatus.OK, {"ok": True})
                return

            if path == "/speaker/busy/start":
                self.server.service.speaker_busy_start(
                    frequency_hz=float(body.get("frequency_hz", 880.0)),
                    duration_ms=int(body.get("duration_ms", 140)),
                    interval_sec=float(body.get("interval_sec", 0.9)),
                    volume=float(body.get("volume", 0.25)),
                )
                self._json(HTTPStatus.OK, {"ok": True})
                return

            if path == "/speaker/busy/stop":
                self.server.service.speaker_busy_stop()
                self._json(HTTPStatus.OK, {"ok": True})
                return

            if path == "/speaker/play-wav":
                wav_path = str(body.get("wav_path", "")).strip()
                if not wav_path:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "wav_path is required"})
                    return
                self.server.service.speaker_play_wav_path(wav_path)
                self._json(HTTPStatus.OK, {"ok": True})
                return

            if path == "/mic/record/start":
                self._json(HTTPStatus.OK, self.server.service.manual_record_start())
                return

            if path == "/mic/record/stop":
                self._json(HTTPStatus.OK, self.server.service.manual_record_stop())
                return

        except FileNotFoundError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_events(self) -> None:
        self._json(HTTPStatus.OK, {"events": self.server.service.list_events()})

    def _serve_segment_wav(self, path: str) -> None:
        sid_part = path[len("/segments/") : -len(".wav")]
        try:
            segment_id = int(sid_part)
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid segment id"})
            return

        try:
            wav_bytes = self.server.service.get_segment_wav(segment_id)
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav_bytes)))
        self.end_headers()
        self.wfile.write(wav_bytes)

    def _stream_events(self) -> None:
        q = self.server.service.subscribe_events()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    evt = q.get(timeout=15)
                    payload = f"data: {json.dumps(evt)}\\n\\n".encode("utf-8")
                except queue.Empty:
                    payload = b": keepalive\n\n"

                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.server.service.unsubscribe_events(q)

    def _serve_index(self) -> None:
        svc = self.server.service
        body = f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Mic + Speaker Service</title>
    <style>
      body {{ background:#0f1520; color:#dfe8f3; font-family:Segoe UI, Arial, sans-serif; margin:0; padding:24px; }}
      .card {{ max-width:920px; margin:0 auto; background:#152032; border:1px solid #2a3f5f; border-radius:10px; padding:16px; }}
      code {{ color:#9ed0ff; }}
      button {{ border:0; border-radius:8px; padding:8px 12px; cursor:pointer; background:#1f8fed; color:#fff; }}
      pre {{ background:#0d1726; border:1px solid #2a3f5f; padding:12px; border-radius:8px; overflow:auto; max-height:280px; }}
    </style>
  </head>
  <body>
    <div class=\"card\">
      <h1>Mic + Speaker Service</h1>
      <p><code>service_id={svc.service_id}</code></p>
      <p>
        <a href=\"/health\">/health</a> |
        <a href=\"/devices\">/devices</a> |
        <a href=\"/events\">/events</a> |
        <a href=\"/segments\">/segments</a>
      </p>
      <button onclick=\"testTone()\">Play Test Tone</button>
      <pre id=\"out\">loading...</pre>
    </div>
    <script>
      async function load() {{
        const h = await fetch('/health').then(r => r.json());
        const e = await fetch('/events').then(r => r.json());
        document.getElementById('out').textContent = JSON.stringify({{health:h, events:e.events.slice(-10)}}, null, 2);
      }}
      async function testTone() {{
        await fetch('/speaker/tone', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{frequency_hz:880,duration_ms:300,volume:0.2}})}});
        await load();
      }}
      load();
      setInterval(load, 1500);
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
    ap = argparse.ArgumentParser(description="Run mic + speaker service")
    ap.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8092, help="bind port (default: 8092)")
    ap.add_argument("--service-id", default="mic-speaker-main", help="logical service id")

    ap.add_argument("--input-device", default=None, help="input device index or name")
    ap.add_argument("--output-device", default=None, help="output device index or name")
    ap.add_argument("--sample-rate", type=int, default=16000, choices=[8000, 16000])
    ap.add_argument("--chunk-samples", type=int, default=512, help="chunk size (default: 512 @16k)")

    ap.add_argument("--max-segments", type=int, default=50)
    ap.add_argument("--ws-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--ws-host", default="0.0.0.0", help="ws bind host (default: 0.0.0.0)")
    ap.add_argument("--ws-port", type=int, default=8095, help="ws bind port (default: 8095)")
    return ap


def main() -> None:
    args = _build_arg_parser().parse_args()

    svc = MicSpeakerService(
        host=args.host,
        port=args.port,
        service_id=args.service_id,
        input_device=args.input_device,
        output_device=args.output_device,
        sample_rate=args.sample_rate,
        chunk_samples=args.chunk_samples,
        max_segments=args.max_segments,
        ws_enabled=args.ws_enabled,
        ws_host=args.ws_host,
        ws_port=args.ws_port,
    )
    svc.serve_forever()


if __name__ == "__main__":
    main()
