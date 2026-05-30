const videoStream = document.getElementById("videoStream");
const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const audioBtn = document.getElementById("audioBtn");
const intercomBtn = document.getElementById("intercomBtn");
const audioStatus = document.getElementById("audioStatus");
const intercomStatus = document.getElementById("intercomStatus");
const streamUrl = "/video_feed";

let audioSocket = null;
let audioSocketOpening = null;
let audioContext = null;
let nextAudioTime = 0;
let audioActive = false;
let intercomActive = false;
let intercomStream = null;
let intercomContext = null;
let intercomSource = null;
let intercomProcessor = null;
let intercomStopTimer = null;
const intercomVolume = 1.0;

function setAudioStatus(message) {
    audioStatus.textContent = message;
}

function setIntercomStatus(message) {
    intercomStatus.textContent = message;
}

// Set src to the stream URL with a timestamp to bypass cache
startBtn.addEventListener("click", () => {
    videoStream.src = streamUrl + "?t=" + Date.now();
});

// Clear src to stop the stream and show black background
stopBtn.addEventListener("click", () => {
    videoStream.src = ""; 
});

// Handle connection errors
videoStream.onerror = () => {
    videoStream.src = ""; 
};

function decodePcm16(base64) {
    const raw = atob(base64);
    const out = new Float32Array(raw.length / 2);
    for (let i = 0; i < out.length; i += 1) {
        const lo = raw.charCodeAt(i * 2);
        const hi = raw.charCodeAt(i * 2 + 1);
        let value = (hi << 8) | lo;
        if (value >= 0x8000) {
            value -= 0x10000;
        }
        out[i] = value / 32768;
    }
    return out;
}

function playPcmChunk(pcm, sampleRate) {
    if (!audioContext || !audioActive) {
        return;
    }

    const buffer = audioContext.createBuffer(1, pcm.length, sampleRate);
    buffer.copyToChannel(pcm, 0);

    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioContext.destination);

    const startAt = Math.max(audioContext.currentTime + 0.03, nextAudioTime);
    source.start(startAt);
    nextAudioTime = startAt + buffer.duration;
}

function getAudioSocket() {
    if (audioSocket && audioSocket.readyState === WebSocket.OPEN) {
        return Promise.resolve(audioSocket);
    }
    if (audioSocketOpening) {
        return audioSocketOpening;
    }

    const wsScheme = window.location.protocol === "https:" ? "wss" : "ws";
    audioSocket = new WebSocket(`${wsScheme}://${window.location.host}/audio_ws`);

    audioSocketOpening = new Promise((resolve, reject) => {
        audioSocket.onopen = () => {
            audioSocketOpening = null;
            resolve(audioSocket);
        };

        audioSocket.onerror = () => {
            audioSocketOpening = null;
            reject(new Error("Audio websocket error"));
        };
    });

    audioSocket.onmessage = (event) => {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch (_err) {
            return;
        }
        if (data.type !== "audio_chunk" || !data.pcm16_b64) {
            return;
        }
        playPcmChunk(decodePcm16(data.pcm16_b64), Number(data.sample_rate) || 16000);
    };

    audioSocket.onclose = () => {
        audioSocket = null;
        audioSocketOpening = null;
        if (audioActive) {
            audioActive = false;
            audioBtn.textContent = "Activate Audio";
            setAudioStatus("Audio disconnected");
        }
        if (intercomActive) {
            stopIntercom("Intercom disconnected");
        }
    };

    return audioSocketOpening;
}

function maybeCloseAudioSocket() {
    if (!audioActive && !intercomActive && audioSocket) {
        audioSocket.close();
    }
}

async function activateAudio() {
    if (audioActive) {
        audioActive = false;
        audioBtn.textContent = "Activate Audio";
        audioBtn.classList.remove("is-active");
        setAudioStatus("Audio inactive");
        if (audioSocket && audioSocket.readyState === WebSocket.OPEN) {
            audioSocket.send(JSON.stringify({ type: "unsubscribe_audio" }));
        }
        maybeCloseAudioSocket();
        return;
    }

    audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
    await audioContext.resume();
    nextAudioTime = audioContext.currentTime;
    audioActive = true;
    audioBtn.textContent = "Deactivate Audio";
    audioBtn.classList.add("is-active");
    setAudioStatus("Connecting to microphone audio...");

    try {
        const socket = await getAudioSocket();
        socket.send(JSON.stringify({ type: "subscribe_audio" }));
        setAudioStatus("Audio active");
    } catch (_err) {
        audioActive = false;
        audioBtn.textContent = "Activate Audio";
        audioBtn.classList.remove("is-active");
        setAudioStatus("Audio connection error");
    }
}

audioBtn.addEventListener("click", activateAudio);

function floatToPcm16Base64(input) {
    const bytes = new Uint8Array(input.length * 2);
    for (let i = 0; i < input.length; i += 1) {
        const s = Math.max(-1, Math.min(1, input[i]));
        const value = s < 0 ? s * 0x8000 : s * 0x7fff;
        const intValue = Math.round(value);
        bytes[i * 2] = intValue & 0xff;
        bytes[i * 2 + 1] = (intValue >> 8) & 0xff;
    }

    let binary = "";
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
        binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return btoa(binary);
}

async function startIntercom() {
    if (intercomActive) {
        return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setIntercomStatus("Intercom unavailable");
        return;
    }

    intercomActive = true;
    intercomBtn.textContent = "Release to Stop";
    intercomBtn.classList.add("is-warning");
    setIntercomStatus("Starting intercom...");

    try {
        const socket = await getAudioSocket();
        if (!intercomActive) {
            maybeCloseAudioSocket();
            return;
        }
        intercomStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
            },
            video: false,
        });
        if (!intercomActive) {
            stopIntercom();
            return;
        }

        intercomContext = new (window.AudioContext || window.webkitAudioContext)();
        await intercomContext.resume();
        intercomSource = intercomContext.createMediaStreamSource(intercomStream);
        intercomProcessor = intercomContext.createScriptProcessor(2048, 1, 1);

        intercomProcessor.onaudioprocess = (event) => {
            event.outputBuffer.getChannelData(0).fill(0);
            if (!intercomActive || !audioSocket || audioSocket.readyState !== WebSocket.OPEN) {
                return;
            }
            if (audioSocket.bufferedAmount > 256000) {
                return;
            }
            const samples = event.inputBuffer.getChannelData(0);
            audioSocket.send(JSON.stringify({
                type: "speaker_pcm16",
                sample_rate: intercomContext.sampleRate,
                channels: 1,
                volume: intercomVolume,
                pcm16_b64: floatToPcm16Base64(samples),
            }));
        };

        intercomSource.connect(intercomProcessor);
        intercomProcessor.connect(intercomContext.destination);
        socket.send(JSON.stringify({ type: "ping" }));
        setIntercomStatus("Intercom active");
    } catch (_err) {
        stopIntercom("Intercom failed");
    }
}

function stopIntercom(message = "Intercom inactive") {
    if (intercomStopTimer) {
        clearTimeout(intercomStopTimer);
        intercomStopTimer = null;
    }
    intercomActive = false;
    intercomBtn.textContent = "Hold Intercom";
    intercomBtn.classList.remove("is-warning");

    if (intercomProcessor) {
        intercomProcessor.disconnect();
        intercomProcessor.onaudioprocess = null;
        intercomProcessor = null;
    }
    if (intercomSource) {
        intercomSource.disconnect();
        intercomSource = null;
    }
    if (intercomStream) {
        intercomStream.getTracks().forEach((track) => track.stop());
        intercomStream = null;
    }
    if (intercomContext) {
        intercomContext.close();
        intercomContext = null;
    }
    if (audioSocket && audioSocket.readyState === WebSocket.OPEN) {
        audioSocket.send(JSON.stringify({ type: "speaker_pcm16_stop" }));
    }

    setIntercomStatus(message);
    maybeCloseAudioSocket();
}

function scheduleIntercomStop() {
    if (!intercomActive) {
        return;
    }
    if (intercomStopTimer) {
        clearTimeout(intercomStopTimer);
    }
    intercomStopTimer = setTimeout(() => {
        stopIntercom();
    }, 120);
}

intercomBtn.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    startIntercom();
});

intercomBtn.addEventListener("pointerup", scheduleIntercomStop);
intercomBtn.addEventListener("pointercancel", scheduleIntercomStop);
intercomBtn.addEventListener("pointerleave", scheduleIntercomStop);

intercomBtn.addEventListener("keydown", (event) => {
    if (event.code === "Space" || event.code === "Enter") {
        event.preventDefault();
        startIntercom();
    }
});

intercomBtn.addEventListener("keyup", (event) => {
    if (event.code === "Space" || event.code === "Enter") {
        event.preventDefault();
        scheduleIntercomStop();
    }
});
