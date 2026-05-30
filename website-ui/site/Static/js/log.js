let count = 0;
let history_loaded = false;

const wsScheme = window.location.protocol === "https:" ? "wss" : "ws";
const socket = new WebSocket(`${wsScheme}://${window.location.host}/ws`); //creating a WebSocket connection using tcp protocol
socket.onopen = () => {
  console.log("Connected to the Raspberry Pi!"); // verifying connection
  socket.send(JSON.stringify({ type: "get_log_history" }));
};

function display_history(entries) {
  if (history_loaded) return;
  history_loaded = true;
  const table = document.getElementById('logTable');
  entries.forEach(entry => {
    const row = table.insertRow(-1);
    const cell1 = row.insertCell(0);
    const cell2 = row.insertCell(1);
    const cell3 = row.insertCell(2);
    const cell4 = row.insertCell(3);

    cell1.textContent = entry.count;
    cell2.textContent = entry.date;
    cell3.textContent = entry.time;
    cell4.textContent = entry.message;

    if (entry.count > count) count = entry.count; // Ensure count is updated correctly

  });
}
socket.onmessage = (event) => {
    const data = JSON.parse(event.data);

    if (data.type === "log_history") {
      display_history(data.data);
      return;
    }
    else if (data.type === "new_log_entry") {
    count += 1;
    const table = document.getElementById('logTable');
    const row = table.insertRow(1);
    const cell1 = row.insertCell(0);
    const cell2 = row.insertCell(1);
    const cell3 = row.insertCell(2);
    const cell4 = row.insertCell(3);

    const now = new Date();
    const date = now.toLocaleDateString();
    const time = now.toLocaleTimeString();

    cell1.textContent = count;
    cell2.textContent = date;
    cell3.textContent = time;
    cell4.textContent = data.message;

    }
};
socket.onclose = () => {
    console.log("Connection lost. Reloading...");
    setTimeout(() => location.reload(), 5000); // Reload the page after 5 seconds
};
