#!/usr/bin/env python3
"""Raspberry Pi camera service backed by rpicam-vid/libcamera."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


PLACEHOLDER_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010101006000600000ffdb0043000302020302020303030304"
    "030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d0e110e0b0b101610"
    "1113141515150c0f171816141812141514ffdb00430103040405040509050509140d0b0d"
    "14141414141414141414141414141414141414141414141414141414141414141414141414"
    "14141414141414141414141414141414141414ffc000110800010001030122000211010311"
    "01ffc4001400010000000000000000000000000000000000000008ffc40014100100000000"
    "0000000000000000000000000000ffda000c03010002110311003f00b2c001ffd9"
)


class PiCameraService:
    """Serve Camera Module frames as MJPEG and snapshots over HTTP."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8081,
        width: int = 1280,
        height: int = 720,
        fps: int = 10,
        camera_id: str = "pi-camera",
        camera_index: int = 0,
        rpicam_path: str = "rpicam-vid",
        jpeg_quality: int = 80,
        autofocus_mode: str = "continuous",
        restart_delay_sec: float = 2.0,
        stale_after_sec: float = 5.0,
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        if fps <= 0:
            raise ValueError("fps must be positive")
        if not (1 <= jpeg_quality <= 100):
            raise ValueError("jpeg_quality must be in [1, 100]")
        if restart_delay_sec <= 0:
            raise ValueError("restart_delay_sec must be positive")
        if stale_after_sec <= 0:
            raise ValueError("stale_after_sec must be positive")

        self.host = host
        self.port = int(port)
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.camera_id = camera_id
        self.camera_index = int(camera_index)
        self.rpicam_path = rpicam_path
        self.jpeg_quality = int(jpeg_quality)
        self.autofocus_mode = autofocus_mode
        self.restart_delay_sec = float(restart_delay_sec)
        self.stale_after_sec = float(stale_after_sec)
        self.frame_interval = 1.0 / self.fps

        self._stop_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame = PLACEHOLDER_JPEG
        self._latest_frame_id = 0
        self._last_frame_at = 0.0
        self._started_at = time.time()
        self._last_error = ""
        self._stderr_lines: deque[str] = deque(maxlen=8)
        self._process: subprocess.Popen[bytes] | None = None
        self._server: PiCameraHTTPServer | None = None

    @property
    def snapshot_path(self) -> str:
        return "/snapshot.jpg"

    @property
    def content_type(self) -> str:
        return "image/jpeg"

    def _command(self) -> list[str]:
        binary = shutil.which(self.rpicam_path) or self.rpicam_path
        return [
            binary,
            "--camera",
            str(self.camera_index),
            "--nopreview",
            "--timeout",
            "0",
            "--codec",
            "mjpeg",
            "--width",
            str(self.width),
            "--height",
            str(self.height),
            "--framerate",
            str(self.fps),
            "--quality",
            str(self.jpeg_quality),
            "--autofocus-mode",
            self.autofocus_mode,
            "--flush",
            "--output",
            "-",
        ]

    def _stderr_loop(self, stream: Any) -> None:
        try:
            for raw_line in iter(stream.readline, b""):
                if self._stop_event.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr_lines.append(line)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _pop_jpeg(buffer: bytearray) -> bytes | None:
        start = buffer.find(b"\xff\xd8")
        if start < 0:
            if len(buffer) > 1_000_000:
                del buffer[:-2]
            return None
        if start > 0:
            del buffer[:start]

        end = buffer.find(b"\xff\xd9", 2)
        if end < 0:
            return None

        frame = bytes(buffer[: end + 2])
        del buffer[: end + 2]
        return frame

    def _stop_process(self) -> None:
        proc = self._process
        if proc is None:
            return
        self._process = None

        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def _producer_once(self) -> None:
        cmd = self._command()
        self._last_error = ""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        self._process = proc

        if proc.stderr is not None:
            threading.Thread(target=self._stderr_loop, args=(proc.stderr,), daemon=True).start()

        if proc.stdout is None:
            raise RuntimeError("rpicam-vid stdout pipe was not created")

        buffer = bytearray()
        while not self._stop_event.is_set():
            chunk = proc.stdout.read(16384)
            if not chunk:
                break
            buffer.extend(chunk)

            while True:
                frame = self._pop_jpeg(buffer)
                if frame is None:
                    break
                now = time.time()
                with self._frame_lock:
                    self._latest_frame = frame
                    self._latest_frame_id += 1
                    self._last_frame_at = now

        exit_code = proc.poll()
        if exit_code is None:
            self._stop_process()
        elif not self._stop_event.is_set():
            stderr_tail = " | ".join(self._stderr_lines)
            raise RuntimeError(f"rpicam-vid exited with code {exit_code}: {stderr_tail}")

    def _producer_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._producer_once()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                self._stop_process()
                self._stop_event.wait(timeout=self.restart_delay_sec)

    def get_latest_frame(self) -> tuple[bytes, int, float]:
        with self._frame_lock:
            return self._latest_frame, self._latest_frame_id, self._last_frame_at

    def get_health_payload(self) -> dict[str, Any]:
        _, frame_id, last_frame_at = self.get_latest_frame()
        now = time.time()
        stale = frame_id == 0 or (now - last_frame_at) > self.stale_after_sec
        payload: dict[str, Any] = {
            "status": "degraded" if self._last_error or stale else "ok",
            "camera_id": self.camera_id,
            "source": "rpicam-vid",
            "camera_index": self.camera_index,
            "listen": {"host": self.host, "port": self.port},
            "stream_path": "/stream",
            "snapshot_path": self.snapshot_path,
            "content_type": self.content_type,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "latest_frame_id": frame_id,
            "last_frame_unix": last_frame_at,
            "uptime_sec": round(now - self._started_at, 2),
            "command": self._command(),
        }
        if stale:
            payload["last_error"] = self._last_error or "no recent camera frame"
        elif self._last_error:
            payload["last_error"] = self._last_error
        if self._stderr_lines:
            payload["rpicam_log_tail"] = list(self._stderr_lines)
        return payload

    def serve_forever(self) -> None:
        producer_thread = threading.Thread(target=self._producer_loop, daemon=True)
        producer_thread.start()

        self._server = PiCameraHTTPServer((self.host, self.port), PiCameraRequestHandler, self)
        print(
            f"[pi-camera] camera_id={self.camera_id} serving on "
            f"http://{self.host}:{self.port}/stream"
        )
        try:
            self._server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
            producer_thread.join(timeout=2.0)

    def shutdown(self) -> None:
        self._stop_event.set()
        self._stop_process()
        if self._server is not None:
            self._server.server_close()
            self._server = None


class PiCameraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        camera_service: PiCameraService,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.camera_service = camera_service


class PiCameraRequestHandler(BaseHTTPRequestHandler):
    server: PiCameraHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[pi-camera] {self.client_address[0]} - {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            self._serve_index()
        elif path == "/health":
            self._serve_health()
        elif path == "/snapshot.jpg":
            self._serve_snapshot()
        elif path == "/stream":
            self._serve_stream()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def _serve_index(self) -> None:
        camera = self.server.camera_service
        body = f"""<!doctype html>
<html>
<head><title>Pi Camera</title></head>
<body>
  <h1>Pi Camera</h1>
  <p><code>camera_id={camera.camera_id} source=rpicam-vid</code></p>
  <img src="/stream" alt="Pi camera stream">
  <p>Snapshot: <a href="/snapshot.jpg">/snapshot.jpg</a></p>
  <p>Health: <a href="/health">/health</a></p>
</body>
</html>
""".encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_health(self) -> None:
        payload = json.dumps(self.server.camera_service.get_health_payload(), indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_snapshot(self) -> None:
        camera = self.server.camera_service
        frame, _, _ = camera.get_latest_frame()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", camera.content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)

    def _serve_stream(self) -> None:
        camera = self.server.camera_service
        self.send_response(HTTPStatus.OK)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()

        last_frame_id = -1
        try:
            while True:
                frame, frame_id, frame_at = camera.get_latest_frame()
                if frame_id == last_frame_id:
                    time.sleep(min(camera.frame_interval, 0.1))
                    continue
                last_frame_id = frame_id
                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n".encode("ascii")
                    + f"X-Frame-Id: {frame_id}\r\n".encode("ascii")
                    + f"X-Frame-Time: {frame_at:.6f}\r\n\r\n".encode("ascii")
                )
                self.wfile.write(header)
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Raspberry Pi camera service.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--camera-id", default="pi-camera")
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--rpicam-path", default="rpicam-vid")
    parser.add_argument("--jpeg-quality", type=int, default=80)
    parser.add_argument("--autofocus-mode", default="continuous")
    parser.add_argument("--restart-delay-sec", type=float, default=2.0)
    parser.add_argument("--stale-after-sec", type=float, default=5.0)
    args = parser.parse_args()

    service = PiCameraService(
        host=args.host,
        port=args.port,
        width=args.width,
        height=args.height,
        fps=args.fps,
        camera_id=args.camera_id,
        camera_index=args.camera_index,
        rpicam_path=args.rpicam_path,
        jpeg_quality=args.jpeg_quality,
        autofocus_mode=args.autofocus_mode,
        restart_delay_sec=args.restart_delay_sec,
        stale_after_sec=args.stale_after_sec,
    )
    service.serve_forever()


if __name__ == "__main__":
    main()
