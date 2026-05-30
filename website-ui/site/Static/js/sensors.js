const alarmStateEl = document.getElementById("alarmState");
const lockStateEl = document.getElementById("lockState");
const espConnectionEl = document.getElementById("espConnection");
const mainConnectionEl = document.getElementById("mainConnection");
const lastTriggerEl = document.getElementById("lastTrigger");
const updatedAtEl = document.getElementById("updatedAt");
const msgEl = document.getElementById("sensorsMessage");
const refreshBtn = document.getElementById("refreshSensorsBtn");
const requestBtn = document.getElementById("requestEspStatusBtn");
const toggleButtons = Array.from(document.querySelectorAll(".sensor-toggle"));

let latestSensors = {};

async function apiGet(url) {
    const res = await fetch(url, { method: "GET" });
    const text = await res.text();
    const body = text ? JSON.parse(text) : {};
    if (!res.ok) {
        throw new Error(body.error || text || res.statusText);
    }
    return body;
}

async function apiPost(url, payload) {
    const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
    });
    const text = await res.text();
    const body = text ? JSON.parse(text) : {};
    if (!res.ok) {
        throw new Error(body.error || text || res.statusText);
    }
    return body;
}

function formatUpdated(unix) {
    const n = Number(unix);
    if (!n) return "never";
    return new Date(n * 1000).toLocaleTimeString();
}

function sensorLine(name, sensor) {
    const enabled = sensor && sensor.enabled !== false;
    if (name === "pir") {
        return `Enabled: ${enabled ? "yes" : "no"} | Active: ${sensor && sensor.active ? "yes" : "no"}`;
    }
    if (name === "ldr") {
        const value = sensor && sensor.value !== undefined && sensor.value !== null ? sensor.value : "-";
        return `Enabled: ${enabled ? "yes" : "no"} | Value: ${value}`;
    }
    if (name === "visual") {
        const online = sensor && sensor.online ? "online" : "offline";
        const active = sensor && sensor.active ? "person detected" : "clear";
        const health = sensor && sensor.health_status ? sensor.health_status : "unknown";
        return `Enabled: ${enabled ? "yes" : "no"} | ${online} | ${active} | Health: ${health}`;
    }
    const open = sensor && sensor.open ? "open" : "closed";
    return `Enabled: ${enabled ? "yes" : "no"} | Door: ${open}`;
}

function applyStatus(state) {
    const sensors = (state && state.sensors) || {};
    latestSensors = sensors;
    alarmStateEl.textContent = state && state.alarm_state ? state.alarm_state : "unknown";
    lockStateEl.textContent = state && state.lock && state.lock.active ? "active" : "inactive";
    const connectivity = (state && state.connectivity) || {};
    espConnectionEl.textContent = connectivity.esp_online ? "online" : "offline";
    mainConnectionEl.textContent = connectivity.main_mqtt_connected ? "online" : "offline";
    lastTriggerEl.textContent = state && state.last_trigger ? state.last_trigger : "none";
    updatedAtEl.textContent = formatUpdated(state && state.updated_unix);

    document.getElementById("pirDetail").textContent = sensorLine("pir", sensors.pir || {});
    document.getElementById("ldrDetail").textContent = sensorLine("ldr", sensors.ldr || {});
    document.getElementById("reedDetail").textContent = sensorLine("reed", sensors.reed || {});
    document.getElementById("visualDetail").textContent = sensorLine("visual", sensors.visual || {});

    toggleButtons.forEach((btn) => {
        const key = btn.dataset.sensor.toLowerCase();
        const enabled = !sensors[key] || sensors[key].enabled !== false;
        btn.textContent = enabled ? "Disable" : "Enable";
        btn.classList.toggle("is-disabled", !enabled);
    });
}

async function refreshSensors(requestEsp) {
    try {
        const url = requestEsp ? "/api/sensors/status?request=1" : "/api/sensors/status";
        const data = await apiGet(url);
        applyStatus(data.sensors || {});
        msgEl.textContent = requestEsp ? "ESP status requested." : "";
    } catch (err) {
        msgEl.textContent = `Status error: ${err.message}`;
    }
}

async function toggleSensor(sensor) {
    const key = sensor.toLowerCase();
    const current = latestSensors[key] || {};
    const enabled = current.enabled === false;
    msgEl.textContent = `${enabled ? "Enabling" : "Disabling"} ${sensor}...`;
    try {
        const data = await apiPost("/api/sensors/update", { sensor, enabled });
        applyStatus(data.sensors || {});
        msgEl.textContent = `${sensor} ${enabled ? "enabled" : "disabled"}.`;
    } catch (err) {
        msgEl.textContent = `Update failed: ${err.message}`;
    }
}

refreshBtn.addEventListener("click", () => refreshSensors(false));
requestBtn.addEventListener("click", () => refreshSensors(true));
toggleButtons.forEach((btn) => {
    btn.addEventListener("click", () => toggleSensor(btn.dataset.sensor));
});

refreshSensors(false);
setInterval(() => refreshSensors(false), 3000);
