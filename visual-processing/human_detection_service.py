#!/usr/bin/env python3
"""Human detection processing service.

Reads frames from an input video feed, runs human detection, and serves
annotated frames + detection metadata over HTTP.
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

try:
    import cv2
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency 'opencv-python'. Install with: pip install opencv-python") from exc

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency 'numpy'. Install with: pip install numpy") from exc


@dataclass(slots=True)
class Detection:
    x: int
    y: int
    w: int
    h: int
    confidence: float


class HumanDetector:
    def detect(self, frame: Any, min_confidence: float) -> list[Detection]:
        raise NotImplementedError


class YoloHumanDetector(HumanDetector):
    def __init__(self, model_path: str) -> None:
        try:
            from ultralytics import YOLO
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "YOLO detector requires 'ultralytics'. Install with: pip install ultralytics"
            ) from exc

        self.model_path = model_path
        try:
            self._model = YOLO(model_path)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Failed to load YOLO model. Provide a local model path via "
                "--yolo-model or VISUAL_YOLO_MODEL (e.g. models/visual-processing/yolo11n_openvino_model)."
            ) from exc

    def detect(self, frame: Any, min_confidence: float) -> list[Detection]:
        # class 0 in COCO is 'person'
        results = self._model.predict(
            source=frame,
            conf=min_confidence,
            classes=[0],
            verbose=False,
        )
        if not results:
            return []

        result = results[0]
        boxes = getattr(result, "boxes", None)
        if boxes is None or boxes.xyxy is None:
            return []

        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy() if boxes.conf is not None else None

        detections: list[Detection] = []
        for idx, box in enumerate(xyxy):
            x1, y1, x2, y2 = [float(v) for v in box]
            conf = float(confs[idx]) if confs is not None else 0.0
            x = max(0, int(x1))
            y = max(0, int(y1))
            w = max(0, int(x2 - x1))
            h = max(0, int(y2 - y1))
            detections.append(Detection(x=x, y=y, w=w, h=h, confidence=conf))
        return detections


class HogHumanDetector(HumanDetector):
    def __init__(self) -> None:
        hog = cv2.HOGDescriptor()
        hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self._hog = hog

    def detect(self, frame: Any, min_confidence: float) -> list[Detection]:
        boxes, weights = self._hog.detectMultiScale(
            frame,
            winStride=(8, 8),
            padding=(8, 8),
            scale=1.05,
        )

        detections: list[Detection] = []
        for (x, y, w, h), weight in zip(boxes, weights):
            conf = float(weight[0]) if hasattr(weight, "__len__") else float(weight)
            if conf < min_confidence:
                continue
            detections.append(
                Detection(
                    x=int(x),
                    y=int(y),
                    w=int(w),
                    h=int(h),
                    confidence=conf,
                )
            )
        return detections


class MotionDetector:
    def __init__(
        self,
        pixel_ratio_threshold: float,
        min_contour_area: int,
        blur_size: int = 7,
        threshold_value: int = 20,
    ) -> None:
        if not (0.0 <= pixel_ratio_threshold <= 1.0):
            raise ValueError("pixel_ratio_threshold must be in [0.0, 1.0]")
        if min_contour_area < 0:
            raise ValueError("min_contour_area must be >= 0")

        self.pixel_ratio_threshold = float(pixel_ratio_threshold)
        self.min_contour_area = int(min_contour_area)
        self.blur_size = int(max(3, blur_size | 1))
        self.threshold_value = int(max(1, threshold_value))
        self._background: np.ndarray | None = None

    def detect(self, frame: Any) -> tuple[bool, float, float]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (self.blur_size, self.blur_size), 0)

        if self._background is None:
            self._background = gray.astype("float32")
            return False, 0.0, 0.0

        cv2.accumulateWeighted(gray, self._background, 0.08)
        background_u8 = cv2.convertScaleAbs(self._background)
        diff = cv2.absdiff(gray, background_u8)

        _, binary = cv2.threshold(diff, self.threshold_value, 255, cv2.THRESH_BINARY)
        binary = cv2.dilate(binary, None, iterations=2)

        changed_pixels = float(cv2.countNonZero(binary))
        total_pixels = float(binary.shape[0] * binary.shape[1])
        pixel_ratio = changed_pixels / total_pixels if total_pixels > 0 else 0.0

        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        max_area = max((float(cv2.contourArea(c)) for c in contours), default=0.0)

        motion = pixel_ratio >= self.pixel_ratio_threshold or max_area >= float(self.min_contour_area)
        return motion, pixel_ratio, max_area


class HumanDetectionProcessingService:
    """Process a video feed and stream annotated human detections."""

    def __init__(
        self,
        input_source: str = "http://127.0.0.1:8081/stream",
        input_mode: str = "stream",
        host: str = "0.0.0.0",
        port: int = 8091,
        fps: int = 6,
        min_confidence: float = 0.35,
        detector: str = "yolo",
        yolo_model: str = "models/visual-processing/yolo11n_openvino_model",
        service_id: str = "human-detector-1",
        motion_gated_yolo: bool = False,
        motion_pixel_ratio_threshold: float = 0.01,
        motion_min_contour_area: int = 900,
        motion_hold_sec: float = 2.0,
        direct_camera_index: int = 0,
        direct_camera_width: int = 1280,
        direct_camera_height: int = 720,
        direct_camera_fps: int = 5,
        direct_camera_rpicam_path: str = "rpicam-vid",
        direct_camera_jpeg_quality: int = 80,
        direct_camera_autofocus_mode: str = "continuous",
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be positive")
        if not (0.0 <= min_confidence <= 1.0):
            raise ValueError("min_confidence must be in [0.0, 1.0]")
        if motion_hold_sec < 0:
            raise ValueError("motion_hold_sec must be >= 0")
        self.input_source = str(input_source)
        self.input_mode = input_mode.strip().lower()
        if self.input_mode not in {"stream", "direct"}:
            raise ValueError("input_mode must be 'stream' or 'direct'")
        self.host = host
        self.port = int(port)
        self.fps = int(fps)
        self.min_confidence = float(min_confidence)
        self.detector_name = detector.strip().lower()
        self.yolo_model = yolo_model
        self.service_id = service_id

        self.motion_gated_yolo = bool(motion_gated_yolo)
        self.motion_pixel_ratio_threshold = float(motion_pixel_ratio_threshold)
        self.motion_min_contour_area = int(motion_min_contour_area)
        self.motion_hold_sec = float(motion_hold_sec)

        self.direct_camera_index = int(direct_camera_index)
        self.direct_camera_width = int(direct_camera_width)
        self.direct_camera_height = int(direct_camera_height)
        self.direct_camera_fps = int(direct_camera_fps)
        self.direct_camera_rpicam_path = direct_camera_rpicam_path
        self.direct_camera_jpeg_quality = int(direct_camera_jpeg_quality)
        self.direct_camera_autofocus_mode = direct_camera_autofocus_mode

        self.frame_interval = 1.0 / self.fps

        if self.detector_name == "yolo":
            self._detector: HumanDetector = YoloHumanDetector(model_path=self.yolo_model)
        elif self.detector_name == "hog":
            self._detector = HogHumanDetector()
        else:
            raise ValueError("detector must be one of: yolo, hog")

        self._motion_detector = MotionDetector(
            pixel_ratio_threshold=self.motion_pixel_ratio_threshold,
            min_contour_area=self.motion_min_contour_area,
        )

        self._server: DetectionHTTPServer | None = None
        self._direct_process: subprocess.Popen[bytes] | None = None
        self._direct_buffer = bytearray()
        self._stop_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame_jpeg = self._build_placeholder_jpeg()
        self._latest_detections: list[dict[str, Any]] = []
        self._latest_motion_detected = False
        self._latest_motion_ratio = 0.0
        self._latest_motion_max_area = 0.0
        self._latest_yolo_active = not self.motion_gated_yolo
        self._last_motion_unix = 0.0
        self._latest_frame_id = 0
        self._latest_frame_at = time.time()
        self._started_at = self._latest_frame_at
        self._last_error = ""

    @staticmethod
    def _build_placeholder_jpeg() -> bytes:
        blank = np.zeros((240, 320, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", blank)
        if not ok:
            raise RuntimeError("failed to create placeholder frame")
        return buf.tobytes()

    def _open_stream_capture(self) -> Any:
        source: str | int
        source = int(self.input_source) if self.input_source.isdigit() else self.input_source
        capture = cv2.VideoCapture(source)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"unable to open input source: {self.input_source}")
        return capture

    def _direct_camera_command(self) -> list[str]:
        binary = shutil.which(self.direct_camera_rpicam_path) or self.direct_camera_rpicam_path
        return [
            binary,
            "--camera",
            str(self.direct_camera_index),
            "--nopreview",
            "--timeout",
            "0",
            "--codec",
            "mjpeg",
            "--width",
            str(self.direct_camera_width),
            "--height",
            str(self.direct_camera_height),
            "--framerate",
            str(self.direct_camera_fps),
            "--quality",
            str(self.direct_camera_jpeg_quality),
            "--autofocus-mode",
            self.direct_camera_autofocus_mode,
            "--flush",
            "--output",
            "-",
        ]

    def _stop_direct_camera(self) -> None:
        proc = self._direct_process
        self._direct_process = None
        self._direct_buffer.clear()
        if proc is None:
            return
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def _start_direct_camera(self) -> None:
        self._stop_direct_camera()
        self._direct_process = subprocess.Popen(
            self._direct_camera_command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

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

    def _read_direct_frame(self) -> Any:
        if self._direct_process is None or self._direct_process.poll() is not None:
            self._start_direct_camera()
        proc = self._direct_process
        if proc is None or proc.stdout is None:
            raise RuntimeError("direct camera process did not expose stdout")

        while not self._stop_event.is_set():
            jpeg = self._pop_jpeg(self._direct_buffer)
            if jpeg is not None:
                arr = np.frombuffer(jpeg, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    raise RuntimeError("failed to decode direct camera JPEG frame")
                return frame

            chunk = proc.stdout.read(16384)
            if not chunk:
                code = proc.poll()
                self._stop_direct_camera()
                raise RuntimeError(f"direct camera stream ended rc={code}")
            self._direct_buffer.extend(chunk)

        raise RuntimeError("direct camera stopped")

    def _annotate_frame(
        self,
        frame: Any,
        detections: list[Detection],
        motion_detected: bool,
        motion_ratio: float,
        show_motion: bool,
    ) -> Any:
        frame_h, frame_w = frame.shape[:2]
        label_scale = 0.6
        status_scale = 0.62
        box_thickness = 2
        text_thickness = 2
        pad = 8
        line_gap = 8

        for d in detections:
            cv2.rectangle(frame, (d.x, d.y), (d.x + d.w, d.y + d.h), (0, 220, 0), box_thickness)
            label = f"person {d.confidence:.2f}"
            (label_w, label_h), baseline = cv2.getTextSize(
                label,
                cv2.FONT_HERSHEY_SIMPLEX,
                label_scale,
                text_thickness,
            )
            label_x = max(0, min(d.x, max(0, frame_w - label_w - pad)))
            label_y = max(label_h + pad, d.y - pad)
            if label_y > d.y and d.y + d.h + label_h + pad < frame_h:
                label_y = d.y + d.h + label_h + pad
            bg_tl = (max(0, label_x - pad // 2), max(0, label_y - label_h - pad // 2))
            bg_br = (
                min(frame_w - 1, label_x + label_w + pad // 2),
                min(frame_h - 1, label_y + baseline + pad // 2),
            )
            cv2.rectangle(frame, bg_tl, bg_br, (0, 80, 0), -1)
            cv2.putText(
                frame,
                label,
                (label_x, label_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                label_scale,
                (30, 255, 30),
                text_thickness,
                cv2.LINE_AA,
            )

        status_parts = [f"Humans: {len(detections)}"]
        if show_motion:
            status_parts.append(f"Motion: {'yes' if motion_detected else 'no'} ({motion_ratio:.3f})")
        lines: list[str] = []
        current = ""
        max_text_w = max(40, frame_w - (pad * 2))
        for part in status_parts:
            candidate = part if not current else f"{current} | {part}"
            (candidate_w, _), _ = cv2.getTextSize(
                candidate,
                cv2.FONT_HERSHEY_SIMPLEX,
                status_scale,
                text_thickness,
            )
            if candidate_w <= max_text_w or not current:
                current = candidate
            else:
                lines.append(current)
                current = part
        if current:
            lines.append(current)

        text_sizes = [
            cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, status_scale, text_thickness)
            for line in lines
        ]
        status_h = sum(size[0][1] + size[1] for size in text_sizes) + line_gap * max(0, len(lines) - 1) + pad
        cv2.rectangle(frame, (0, 0), (frame_w - 1, min(frame_h - 1, status_h)), (0, 0, 0), -1)
        y = pad
        for line, ((_, text_h), baseline) in zip(lines, text_sizes):
            y += text_h
            cv2.putText(
                frame,
                line,
                (pad, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                status_scale,
                (30, 240, 240),
                text_thickness,
                cv2.LINE_AA,
            )
            y += baseline + line_gap
        return frame

    def _producer_loop(self) -> None:
        target = time.perf_counter()
        capture = None
        frame_id = 0

        while not self._stop_event.is_set():
            if self.input_mode == "direct":
                if capture is not None:
                    capture.release()
                    capture = None
                try:
                    frame = self._read_direct_frame()
                    self._last_error = ""
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc)
                    self._stop_direct_camera()
                    self._stop_event.wait(timeout=1.0)
                    continue
            else:
                if capture is None:
                    try:
                        capture = self._open_stream_capture()
                        self._last_error = ""
                    except Exception as exc:  # noqa: BLE001
                        self._last_error = str(exc)
                        self._stop_event.wait(timeout=1.0)
                        continue

                ok, frame = capture.read()
                if not ok or frame is None:
                    self._last_error = "failed to read frame from source"
                    capture.release()
                    capture = None
                    self._stop_event.wait(timeout=0.5)
                    continue

            now = time.time()
            motion_detected = False
            motion_ratio = 0.0
            motion_max_area = 0.0
            yolo_active = True
            if self.motion_gated_yolo:
                motion_detected, motion_ratio, motion_max_area = self._motion_detector.detect(frame)
                if motion_detected:
                    self._last_motion_unix = now
                yolo_active = motion_detected or ((now - self._last_motion_unix) <= self.motion_hold_sec)

            detections: list[Detection] = []
            if yolo_active:
                detections = self._detector.detect(frame, self.min_confidence)

            annotated = self._annotate_frame(
                frame=frame,
                detections=detections,
                motion_detected=motion_detected,
                motion_ratio=motion_ratio,
                show_motion=self.motion_gated_yolo,
            )

            enc_ok, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not enc_ok:
                self._last_error = "failed to encode frame"
                continue

            frame_id += 1
            det_json = [
                {
                    "x": d.x,
                    "y": d.y,
                    "w": d.w,
                    "h": d.h,
                    "confidence": round(d.confidence, 4),
                }
                for d in detections
            ]

            with self._frame_lock:
                self._latest_frame_jpeg = encoded.tobytes()
                self._latest_detections = det_json
                self._latest_motion_detected = motion_detected
                self._latest_motion_ratio = motion_ratio
                self._latest_motion_max_area = motion_max_area
                self._latest_yolo_active = yolo_active
                self._latest_frame_id = frame_id
                self._latest_frame_at = now

            target += self.frame_interval
            sleep_for = target - time.perf_counter()
            if sleep_for > 0:
                self._stop_event.wait(timeout=sleep_for)
            else:
                target = time.perf_counter()

        if capture is not None:
            capture.release()

    def get_latest(self) -> tuple[bytes, dict[str, Any]]:
        with self._frame_lock:
            return self._latest_frame_jpeg, {
                "detections": list(self._latest_detections),
                "frame_id": self._latest_frame_id,
                "frame_time_unix": self._latest_frame_at,
                "motion_detected": self._latest_motion_detected,
                "motion_ratio": self._latest_motion_ratio,
                "motion_max_area": self._latest_motion_max_area,
                "yolo_active": self._latest_yolo_active,
            }

    def get_health_payload(self) -> dict[str, Any]:
        _, latest = self.get_latest()
        now = time.time()
        payload = {
            "status": "ok" if not self._last_error else "degraded",
            "service_id": self.service_id,
            "detector": self.detector_name,
            "yolo_model": self.yolo_model if self.detector_name == "yolo" else "",
            "input_mode": self.input_mode,
            "input_source": self.input_source,
            "direct_camera": {
                "camera_index": self.direct_camera_index,
                "width": self.direct_camera_width,
                "height": self.direct_camera_height,
                "fps": self.direct_camera_fps,
                "rpicam_path": self.direct_camera_rpicam_path,
            },
            "listen": {"host": self.host, "port": self.port},
            "stream_path": "/stream",
            "snapshot_path": "/snapshot.jpg",
            "detections_path": "/detections",
            "fps": self.fps,
            "min_confidence": self.min_confidence,
            "motion_gated_yolo": self.motion_gated_yolo,
            "motion_pixel_ratio_threshold": self.motion_pixel_ratio_threshold,
            "motion_min_contour_area": self.motion_min_contour_area,
            "motion_hold_sec": self.motion_hold_sec,
            "latest_frame_id": latest["frame_id"],
            "latest_frame_unix": latest["frame_time_unix"],
            "latest_human_count": len(latest["detections"]),
            "latest_motion_detected": latest["motion_detected"],
            "latest_motion_ratio": round(float(latest["motion_ratio"]), 6),
            "latest_yolo_active": latest["yolo_active"],
            "uptime_sec": round(now - self._started_at, 2),
        }
        if self._last_error:
            payload["last_error"] = self._last_error
        return payload

    def serve_forever(self) -> None:
        self._server = DetectionHTTPServer((self.host, self.port), DetectionRequestHandler, self)
        producer_thread = threading.Thread(target=self._producer_loop, daemon=True)
        producer_thread.start()
        print(
            f"[visual-processing] service_id={self.service_id} detector={self.detector_name} "
            f"serving on http://{self.host}:{self.port}/stream"
        )
        try:
            self._server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
            producer_thread.join(timeout=1.0)

    def shutdown(self) -> None:
        self._stop_event.set()
        self._stop_direct_camera()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


class DetectionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        service: HumanDetectionProcessingService,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.service = service


class DetectionRequestHandler(BaseHTTPRequestHandler):
    server: DetectionHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._serve_index()
            return
        if path == "/health":
            self._serve_health()
            return
        if path == "/detections":
            self._serve_detections()
            return
        if path == "/snapshot.jpg":
            self._serve_snapshot()
            return
        if path == "/stream":
            self._serve_stream()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[visual-processing] {self.client_address[0]} - {fmt % args}")

    def _serve_index(self) -> None:
        service = self.server.service
        body = f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Human Detection Stream</title>
    <style>
      body {{
        background: #0d1117;
        color: #dfe7ef;
        font-family: \"Segoe UI\", Arial, sans-serif;
        margin: 0;
        padding: 24px;
      }}
      .card {{
        max-width: 980px;
        margin: 0 auto;
        border: 1px solid #2c3f56;
        border-radius: 10px;
        background: #132030;
        padding: 16px;
      }}
      img {{
        width: 100%;
        border-radius: 8px;
        border: 1px solid #2c3f56;
      }}
      code {{ color: #8fc3ff; }}
    </style>
  </head>
  <body>
    <div class=\"card\">
      <h1>Human Detection Output</h1>
      <p><code>
        service_id={service.service_id}
        detector={service.detector_name}
        motion_gated_yolo={service.motion_gated_yolo}
      </code></p>
      <img src=\"/stream\" alt=\"annotated detection stream\">
      <p>Health: <a href=\"/health\">/health</a> | Detections: <a href=\"/detections\">/detections</a></p>
    </div>
  </body>
</html>
"""
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_health(self) -> None:
        payload = json.dumps(self.server.service.get_health_payload()).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_detections(self) -> None:
        _, latest = self.server.service.get_latest()
        payload = json.dumps(
            {
                "frame_id": latest["frame_id"],
                "frame_time_unix": latest["frame_time_unix"],
                "human_count": len(latest["detections"]),
                "motion_detected": latest["motion_detected"],
                "motion_ratio": latest["motion_ratio"],
                "motion_max_area": latest["motion_max_area"],
                "yolo_active": latest["yolo_active"],
                "detections": latest["detections"],
            }
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_snapshot(self) -> None:
        frame, _ = self.server.service.get_latest()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)

    def _serve_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()

        try:
            while True:
                frame, latest = self.server.service.get_latest()
                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n".encode("ascii")
                    + f"X-Frame-Id: {latest['frame_id']}\r\n".encode("ascii")
                    + f"X-Frame-Time: {latest['frame_time_unix']:.6f}\r\n".encode("ascii")
                    + f"X-Human-Count: {len(latest['detections'])}\r\n".encode("ascii")
                    + f"X-Motion: {int(bool(latest['motion_detected']))}\r\n".encode("ascii")
                    + f"X-Yolo-Active: {int(bool(latest['yolo_active']))}\r\n\r\n".encode("ascii")
                )
                self.wfile.write(header)
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(self.server.service.frame_interval)
        except (BrokenPipeError, ConnectionResetError):
            return


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run human detection processing service.")
    parser.add_argument(
        "--input-source",
        default="http://127.0.0.1:8081/stream",
        help="input video source (URL, file path, or camera index string)",
    )
    parser.add_argument(
        "--input-mode",
        choices=["stream", "direct"],
        default="stream",
        help="stream reads --input-source; direct uses rpicam-vid inside this service",
    )
    parser.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8091, help="bind port (default: 8091)")
    parser.add_argument("--fps", type=int, default=6, help="processing FPS target (default: 6)")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.35,
        help="minimum confidence threshold in [0,1] (default: 0.35)",
    )
    parser.add_argument(
        "--detector",
        default="yolo",
        choices=["yolo", "hog"],
        help="human detector backend (default: yolo)",
    )
    parser.add_argument(
        "--yolo-model",
        default="models/visual-processing/yolo11n_openvino_model",
        help="YOLO model path/name (default: models/visual-processing/yolo11n_openvino_model)",
    )
    parser.add_argument(
        "--motion-gated-yolo",
        action="store_true",
        help="run YOLO only when motion was detected recently",
    )
    parser.add_argument(
        "--motion-pixel-ratio-threshold",
        type=float,
        default=0.01,
        help="motion threshold as changed-pixel ratio [0,1] (default: 0.01)",
    )
    parser.add_argument(
        "--motion-min-contour-area",
        type=int,
        default=900,
        help="motion threshold as minimum moving contour area in pixels (default: 900)",
    )
    parser.add_argument(
        "--motion-hold-sec",
        type=float,
        default=2.0,
        help="keep YOLO active this many seconds after motion (default: 2.0)",
    )
    parser.add_argument(
        "--service-id",
        default="human-detector-1",
        help="logical service id (default: human-detector-1)",
    )
    parser.add_argument("--direct-camera-index", type=int, default=0)
    parser.add_argument("--direct-camera-width", type=int, default=1280)
    parser.add_argument("--direct-camera-height", type=int, default=720)
    parser.add_argument("--direct-camera-fps", type=int, default=5)
    parser.add_argument("--direct-camera-rpicam-path", default="rpicam-vid")
    parser.add_argument("--direct-camera-jpeg-quality", type=int, default=80)
    parser.add_argument("--direct-camera-autofocus-mode", default="continuous")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    service = HumanDetectionProcessingService(
        input_source=args.input_source,
        input_mode=args.input_mode,
        host=args.host,
        port=args.port,
        fps=args.fps,
        min_confidence=args.min_confidence,
        detector=args.detector,
        yolo_model=args.yolo_model,
        service_id=args.service_id,
        motion_gated_yolo=args.motion_gated_yolo,
        motion_pixel_ratio_threshold=args.motion_pixel_ratio_threshold,
        motion_min_contour_area=args.motion_min_contour_area,
        motion_hold_sec=args.motion_hold_sec,
        direct_camera_index=args.direct_camera_index,
        direct_camera_width=args.direct_camera_width,
        direct_camera_height=args.direct_camera_height,
        direct_camera_fps=args.direct_camera_fps,
        direct_camera_rpicam_path=args.direct_camera_rpicam_path,
        direct_camera_jpeg_quality=args.direct_camera_jpeg_quality,
        direct_camera_autofocus_mode=args.direct_camera_autofocus_mode,
    )
    service.serve_forever()


if __name__ == "__main__":
    main()
