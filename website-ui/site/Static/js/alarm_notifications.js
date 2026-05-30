(function () {
    const POLL_MS = 3000;
    const DEDUPE_KEY = "magen:lastAlarmNotificationKey";
    let lastAlarmActive = false;
    let initialized = false;
    let permissionRequested = false;
    let latestSensorState = null;
    let promptEl = null;
    let statusEl = null;

    function notificationsSupported() {
        return "Notification" in window;
    }

    function currentPermission() {
        return notificationsSupported() ? Notification.permission : "unsupported";
    }

    function isIos() {
        return /iPad|iPhone|iPod/.test(navigator.userAgent)
            || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    }

    function isStandalone() {
        return window.matchMedia("(display-mode: standalone)").matches
            || window.navigator.standalone === true;
    }

    function permissionMessage(permission) {
        if (!window.isSecureContext) {
            return "Notifications need HTTPS or localhost.";
        }
        if (!notificationsSupported()) {
            if (isIos() && !isStandalone()) {
                return "On iPhone, open this site in Safari, add it to Home Screen, then open it from the Home Screen icon.";
            }
            return "This browser does not support website notifications.";
        }
        if (permission === "granted") {
            return "Alarm notifications enabled.";
        }
        if (permission === "denied") {
            return "Notifications are blocked in this browser's site settings.";
        }
        if (isIos() && !isStandalone()) {
            return "On iPhone, add this site to Home Screen first, then enable notifications from the installed app.";
        }
        return "Enable alarm notifications for this device.";
    }

    function updatePrompt() {
        if (!promptEl || !statusEl) {
            return;
        }
        const permission = currentPermission();
        statusEl.textContent = permissionMessage(permission);
        promptEl.hidden = permission === "granted";
        const button = promptEl.querySelector("button");
        if (button) {
            button.disabled = permission === "denied" || permission === "unsupported" || !window.isSecureContext;
        }
    }

    function createPrompt() {
        promptEl = document.createElement("div");
        promptEl.className = "notification-prompt";
        promptEl.innerHTML = [
            '<span class="notification-prompt__text"></span>',
            '<button type="button">Enable Notifications</button>',
        ].join("");
        statusEl = promptEl.querySelector(".notification-prompt__text");
        const button = promptEl.querySelector("button");
        if (button) {
            button.addEventListener("click", () => {
                requestPermissionIfNeeded(true);
            });
        }
        document.body.appendChild(promptEl);
        updatePrompt();
    }

    async function requestPermissionIfNeeded(force) {
        if (!notificationsSupported() || permissionRequested || Notification.permission !== "default") {
            updatePrompt();
            return currentPermission();
        }
        if (!force && isIos() && !isStandalone()) {
            updatePrompt();
            return currentPermission();
        }
        permissionRequested = true;
        try {
            const permission = await Notification.requestPermission();
            if (permission === "granted" && latestSensorState && effectiveAlarmState(latestSensorState) === "alarm") {
                sendAlarmNotification(latestSensorState);
            }
            updatePrompt();
            return permission;
        } catch (_err) {
            updatePrompt();
            return currentPermission();
        }
    }

    function requestOnFirstUserGesture() {
        if (currentPermission() !== "default") {
            return;
        }
        const request = () => {
            requestPermissionIfNeeded(false);
            window.removeEventListener("click", request);
            window.removeEventListener("keydown", request);
            window.removeEventListener("touchstart", request);
        };
        window.addEventListener("click", request, { once: true });
        window.addEventListener("keydown", request, { once: true });
        window.addEventListener("touchstart", request, { once: true });
    }

    function effectiveAlarmState(sensorState) {
        const desired = String(sensorState && sensorState.desired_alarm_state ? sensorState.desired_alarm_state : "").toLowerCase();
        const mqtt = String(sensorState && sensorState.alarm_state ? sensorState.alarm_state : "").toLowerCase();
        return desired && desired !== "unknown" ? desired : mqtt;
    }

    function notificationKey(sensorState) {
        const updated = sensorState && sensorState.updated_unix ? String(sensorState.updated_unix) : "";
        const trigger = sensorState && sensorState.last_trigger ? String(sensorState.last_trigger) : "unknown";
        return `${trigger}:${updated}`;
    }

    function triggerText(sensorState) {
        const trigger = sensorState && sensorState.last_trigger ? String(sensorState.last_trigger) : "unknown sensor";
        const metadata = sensorState && sensorState.last_trigger_metadata && typeof sensorState.last_trigger_metadata === "object"
            ? sensorState.last_trigger_metadata
            : {};
        if (metadata.human_count) {
            return `${trigger} detected ${metadata.human_count} person${Number(metadata.human_count) === 1 ? "" : "s"}`;
        }
        return `Triggered by ${trigger}`;
    }

    function sendAlarmNotification(sensorState) {
        if (currentPermission() !== "granted") {
            return;
        }
        const key = notificationKey(sensorState);
        if (localStorage.getItem(DEDUPE_KEY) === key) {
            return;
        }
        localStorage.setItem(DEDUPE_KEY, key);
        const notification = new Notification("MAGEN alarm active", {
            body: triggerText(sensorState),
            tag: "magen-alarm-active",
            requireInteraction: true,
        });
        notification.onclick = () => {
            window.focus();
            notification.close();
        };
    }

    async function pollAlarmState() {
        try {
            const res = await fetch("/api/sensors/status", { method: "GET", cache: "no-store" });
            if (!res.ok) {
                return;
            }
            const data = await res.json();
            const sensorState = data && data.sensors ? data.sensors : {};
            latestSensorState = sensorState;
            const alarmActive = effectiveAlarmState(sensorState) === "alarm";
            if (!initialized) {
                initialized = true;
                lastAlarmActive = alarmActive;
                return;
            }
            if (alarmActive && !lastAlarmActive) {
                sendAlarmNotification(sensorState);
            }
            lastAlarmActive = alarmActive;
        } catch (_err) {
            return;
        }
    }

    requestOnFirstUserGesture();
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", createPrompt, { once: true });
    } else {
        createPrompt();
    }
    if (currentPermission() === "granted") {
        pollAlarmState();
    }
    setInterval(pollAlarmState, POLL_MS);
})();
