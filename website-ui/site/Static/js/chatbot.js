const wsScheme = window.location.protocol === "https:" ? "wss" : "ws";
const socket = new WebSocket(`${wsScheme}://${window.location.host}/ws`);
const chatBox = document.getElementById("chat-box");
const userInput = document.getElementById("user-input");
const sendButton = document.getElementById("send-btn");

let history_loaded = false;
let wait_response = false;

function redactSensitiveText(text) {
    return String(text || "").replace(/(\bpassword\b\s*(?:is|=|:)?\s*)([^\s,.;]+)/gi, "$1***");
}

socket.onopen = () => {
    console.log("Connected to the Raspberry Pi!");
    // Ask for history as soon as we connect
    socket.send(JSON.stringify({ type: "get_chat_history" }));
};

// Function to show the chat history
function display_history(data) {
    if (history_loaded) return;
    history_loaded = true;
    
    // We don't clear the chatBox entirely to keep the welcome message
    data.messages.forEach(msg => {
        appendMessage(msg.sender, msg.text);
    });
}

socket.onmessage = (event) => {
    const data = JSON.parse(event.data);

    if (data.type === "chat_history") {
        display_history(data);
    } 
    else if (data.type === "reply") {
        // Remove the "thinking" indicator
        const typing = document.getElementById('typing');
        if (typing) typing.remove();

        // Add the AI response
        appendMessage("Bot", data.text);
        
        // Unlock the UI
        wait_response = false;
        sendButton.disabled = false;
        userInput.disabled = false;
    }
};

// Helper function to add messages to the screen
function appendMessage(sender, text) {
    const msgDiv = document.createElement("div");
    msgDiv.className = sender === "You" ? "user-message" : "bot-message";

    const senderEl = document.createElement("span");
    senderEl.className = "message-sender";
    senderEl.textContent = `${sender}:`;

    const textEl = document.createElement("span");
    textEl.className = "message-text";
    textEl.dir = "auto";
    textEl.textContent = redactSensitiveText(text);

    msgDiv.appendChild(senderEl);
    msgDiv.appendChild(textEl);
    chatBox.appendChild(msgDiv);
    chatBox.scrollTop = chatBox.scrollHeight;
}

function handleMessage() {
    if (wait_response) return;

    let text = userInput.value.trim();
    if (text === "") return;

    // Send to Pi
    socket.send(JSON.stringify({ type: "chat_message", text: text }));

    // Show your message and a loading indicator
    appendMessage("You", text);
    const typing = document.createElement("i");
    typing.id = "typing";
    typing.textContent = "Bot is thinking...";
    chatBox.appendChild(typing);
    chatBox.scrollTop = chatBox.scrollHeight;

    // Lock the UI
    userInput.value = "";
    wait_response = true;
    sendButton.disabled = true;
    userInput.disabled = true;
}

sendButton.addEventListener("click", handleMessage);
userInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleMessage();
});

socket.onclose = () => {
    setTimeout(() => location.reload(), 5000);
};
