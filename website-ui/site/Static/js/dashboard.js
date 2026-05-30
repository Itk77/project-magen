const statusEl = document.getElementById("systemStatus");
const connectionStatusEl = document.getElementById("connectionStatus");
const pwdEl = document.getElementById("systemPassword");
const msgEl = document.getElementById("controlMessage");
const refreshBtn = document.getElementById("refreshStatusBtn");
const armBtn = document.getElementById("armBtn");
const alarmBtn = document.getElementById("alarmBtn");
const disarmBtn = document.getElementById("disarmBtn");

async function apiGet(url) {
    const res = await fetch(url, { method: "GET" });
    const text = await res.text();
    let body;
    try {
        body = JSON.parse(text);
    } catch (_err) {
        body = { raw: text };
    }
    if (!res.ok) {
        throw new Error(typeof body === "object" ? JSON.stringify(body) : String(body));
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
    let body;
    try {
        body = JSON.parse(text);
    } catch (_err) {
        body = { raw: text };
    }
    if (!res.ok) {
        throw new Error(typeof body === "object" ? JSON.stringify(body) : String(body));
    }
    return body;
}

async function refreshStatus() {
    try {
        const data = await apiGet("/api/sensors/status");
        const sensorState = data && data.sensors ? data.sensors : {};
        const connectivity = sensorState.connectivity || {};
        const desiredState = sensorState.desired_alarm_state || "";
        const mqttState = sensorState.alarm_state || "";
        const state = desiredState && desiredState !== "unknown" ? desiredState : mqttState;
        const armed = state === "armed";
        if (state === "alarm") {
            statusEl.textContent = "Status: ALARM ACTIVE";
        } else if (state === "armed" || armed) {
            statusEl.textContent = "Status: ARMED";
        } else if (state === "deactivated" || !armed) {
            statusEl.textContent = "Status: DISARMED";
        } else {
            statusEl.textContent = "Status: unknown";
        }
        connectionStatusEl.textContent = `ESP: ${connectivity.esp_online ? "online" : "offline"} | Main/MQTT: ${connectivity.main_mqtt_connected ? "online" : "offline"}`;
    } catch (err) {
        statusEl.textContent = "Status: unavailable";
        connectionStatusEl.textContent = "ESP: unknown | Main/MQTT: unavailable";
    }
}

async function runAction(action) {
    const password = (pwdEl.value || "").trim();
    if (!password) {
        msgEl.textContent = "Enter system password first.";
        return;
    }
    msgEl.textContent = `${action} request in progress...`;
    try {
        const data = await apiPost(`/api/system/${action}`, { password });
        const resultMsg = data && data.result && data.result.message ? data.result.message : "Done.";
        msgEl.textContent = resultMsg;
        await refreshStatus();
    } catch (err) {
        msgEl.textContent = `${action} failed: ${err.message}`;
    }
}

refreshBtn.addEventListener("click", refreshStatus);
armBtn.addEventListener("click", () => runAction("arm"));
alarmBtn.addEventListener("click", () => runAction("alarm"));
disarmBtn.addEventListener("click", () => runAction("disarm"));

refreshStatus();
setInterval(refreshStatus, 3000);
