let count = 0;
let history_loaded = false;

const wsScheme = window.location.protocol === "https:" ? "wss" : "ws";
const socket = new WebSocket(`${wsScheme}://${window.location.host}/ws`); //creating a WebSocket connection using tcp protocol
socket.onopen = () => {
  console.log("Connected to the Raspberry Pi!"); // verifying connection
  socket.send(JSON.stringify({ type: "get_log_history" }));
};

function sourceLabel(source) {
  const labels = {
    api: "Website",
    assistant_api: "Assistant",
    assistant_voice: "Voice assistant",
    assistant_ws: "Assistant",
    chat: "Chat",
    mqtt: "ESP / MQTT",
    startup: "Startup",
    visual: "Camera",
    voice: "Voice",
    website: "Website",
  };
  return labels[String(source || "").toLowerCase()] || (source ? String(source) : "System");
}

function friendlyMessage(raw) {
  const message = String(raw || "");
  if (/mqtt connected/i.test(message)) return "Connected to the ESP message broker.";
  if (/mqtt disconnected/i.test(message)) return "Disconnected from the ESP message broker.";
  if (/mqtt connect failed/i.test(message)) return "Could not connect to the ESP message broker.";
  if (/mqtt rx alarm\/sensor\/status/i.test(message)) return "Received an updated sensor reading from the ESP.";
  if (/mqtt rx alarm\/heartbeat\/esp/i.test(message)) return "ESP heartbeat received.";
  if (/mqtt rx alarm\/trigger/i.test(message)) return "Alarm trigger received.";
  if (/mqtt tx alarm\/command: ARM/i.test(message)) return "Arm command sent to the ESP.";
  if (/mqtt tx alarm\/command: DISARM/i.test(message)) return "Disarm command sent to the ESP.";
  if (/mqtt tx alarm\/command: ALARM/i.test(message)) return "Alarm activation command sent to the ESP.";
  if (/main server listening/i.test(message)) return "Main server started and is listening for website requests.";
  if (/website websocket connected/i.test(message)) return "Website connected for live updates.";
  if (/website websocket disconnected/i.test(message)) return "Website live update connection closed.";
  if (/assistant reply/i.test(message)) return message.replace(/^assistant reply(?:\(api\))?:\s*/i, "Assistant replied: ");
  if (/user chat/i.test(message)) return "User sent a chat message.";
  if (/voice recording started/i.test(message)) return "Voice button recording started.";
  if (/voice recording stopped/i.test(message)) return "Voice button recording finished.";
  if (/voice segment \d+ processed/i.test(message)) return "Voice command processed.";
  if (/visual detection triggered alarm/i.test(message)) return "Camera detection triggered the alarm.";
  return message;
}

function formatWhen(entry) {
  const unix = Number(entry && entry.timestamp_unix);
  if (unix) {
    return new Date(unix * 1000).toLocaleString();
  }
  return `${entry.date || ""} ${entry.time || ""}`.trim() || "-";
}

function addRow(entry, index) {
  const table = document.getElementById("logTable");
  const row = table.insertRow(index);
  row.className = `log-row log-source-${String(entry.source || "system").toLowerCase().replace(/[^a-z0-9_-]/g, "-")}`;
  row.title = String(entry.message || "");
  const cell1 = row.insertCell(0);
  const cell2 = row.insertCell(1);
  const cell3 = row.insertCell(2);
  const cell4 = row.insertCell(3);

  cell1.textContent = entry.count || "";
  cell2.textContent = formatWhen(entry);
  cell3.textContent = sourceLabel(entry.source);
  cell4.textContent = friendlyMessage(entry.message);
  cell4.className = "log-message-cell";

  if (Number(entry.count) > count) count = Number(entry.count);
}

function display_history(entries) {
  if (history_loaded) return;
  history_loaded = true;
  (entries || []).forEach(entry => addRow(entry, -1));
}
socket.onmessage = (event) => {
    const data = JSON.parse(event.data);

    if (data.type === "log_history") {
      display_history(data.data);
      return;
    }
    else if (data.type === "new_log_entry") {
      addRow(data.entry || { count: count + 1, message: data.message, timestamp_unix: Date.now() / 1000 }, 1);
    }
};
socket.onclose = () => {
    console.log("Connection lost. Reloading...");
    setTimeout(() => location.reload(), 5000); // Reload the page after 5 seconds
};
