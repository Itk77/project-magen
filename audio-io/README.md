# Mic + Speaker Service

This module provides local microphone capture, manual WAV segment recording, speaker playback, and browser-to-Pi audio streaming.

## Run

```bash
python3 audio-io/mic_speaker_service.py
```

Or via the main-server config loader:

```bash
python3 main-server/run_audio_io_service.py
```

## Recording Model

The service continuously reads microphone chunks, but it only saves a segment while manual recording is active. In the main system this is driven by the physical voice button on the Pi.

## HTTP Endpoints

- `GET /health`
- `GET /devices`
- `GET /events`
- `GET /stream/events` (SSE)
- `GET /segments`
- `GET /segments/{id}.wav`
- `POST /speaker/tone`
- `POST /speaker/busy/start`
- `POST /speaker/busy/stop`
- `POST /speaker/play-wav`
- `POST /mic/record/start`
- `POST /mic/record/stop`

## WebSocket

The WebSocket service is enabled by default on port `8095`.

Useful message types:

- `subscribe_audio`
- `unsubscribe_audio`
- `speaker_pcm16`
- `speaker_pcm16_stop`
- `speaker_tone`
- `speaker_busy_start`
- `speaker_busy_stop`

## Dependencies

Install from:

```bash
pip install -r audio-io/requirements.txt
```
