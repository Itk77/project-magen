#include <U8g2lib.h>
#include <Wire.h>
#include <RTClib.h>
#include "mbedtls/sha256.h"
#include <WiFi.h>
#include <PubSubClient.h>
#include <WiFiClientSecure.h>
#include <ESPmDNS.h>
#include <time.h>
#include <sys/time.h>
#include "esp_sntp.h"

RTC_DS3231 rtc;

const char* root_ca = \
"-----BEGIN CERTIFICATE-----\n" \
"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\n" \
"-----END CERTIFICATE-----";

const char* MQTT_USERNAME = "sensors_unit";
const char* MQTT_PASSWORD = "1234";

const char* ssid= "Mobile Hotspot"; // Your Wifi name
const char* wifi_Password="12341234"; 
const char* mqttHostName = "raspberrypi.local";
const char* ntpServer1 = "pool.ntp.org";
const char* ntpServer2 = "time.google.com";
const char* timeZone = "IST-2IDT,M3.4.4/26,M10.5.0";
IPAddress mqttServerIp;
bool mqttServerResolved = false;

WiFiClientSecure secureClient;
PubSubClient mqtt(secureClient); //Creating a MQTT client

//Remote auth state 
volatile bool authWaiting = false;
volatile bool authResult = false;
uint32_t authRequestId = 0;
unsigned long authStartTime = 0;

const unsigned long AUTH_TIMEOUT = 2000; // 2 seconds
const unsigned long MQTT_RECONNECT_INTERVAL = 30000;
const unsigned long MQTT_CONNECT_FAIL_BACKOFF = 60000;
const unsigned long SENSOR_STATUS_INTERVAL = 10000;
const unsigned long NTP_RETRY_INTERVAL = 300000;
const unsigned long HEARTBEAT_INTERVAL = 5000;
const unsigned long MAIN_HEARTBEAT_TIMEOUT = 15000;

bool mqttConnected = false;
bool timeSynced = false;
bool ntpTimeSynced = false;
bool mainServerConnected = false;
volatile bool ntpSyncCompleted = false;
unsigned long lastMqttReconnectAttempt = 0;
unsigned long mqttConnectBackoffUntil = 0;
unsigned long lastSensorStatusPublish = 0;
unsigned long lastNtpSyncAttempt = 0;
unsigned long lastHeartbeatPublish = 0;
unsigned long lastMainHeartbeat = 0;
struct disable {
  bool ldr;
  bool pir;
  bool reed;
};

disable sensors = {
  .ldr = true,
  .pir = true,
  .reed = true
};

const uint16_t alarmFreq[] = {3500, 3800, 4100, 3800};
const uint8_t numAlarmFreq = sizeof(alarmFreq) / sizeof(alarmFreq[0]);

const int portNumber=8883; //The standard ESP32 port for TLS over MQTT
int alarmIndex = 0;
unsigned long lastAlarmTime = 0;

const char hexaKeys[4][4] = {
  {'1','2','3','A'},
  {'4','5','6','B'},
  {'7','8','9','C'},
  {'*','0','#','D'}
};

const uint8_t rowPins[4] = {4, 5, 12, 19};  
const uint8_t colPins[4] = {23, 16, 26, 27};

const char password_hash[] = "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4"; 

unsigned long errorStartTime = 0;
bool showingError = false;

char enteredCode[5] = "";
int codeLength = 0;

U8G2_SH1106_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);

const int ldrPin = 34;
const int lightThreshold = 200; // Our definition to "extreme change" 
int lastLdrValue = 0;
unsigned long armedTime = 0;
const unsigned long LDR_IGNORE_TIME = 800; //give some time for the LDR to stabilize
const int laserPin=14;

const int pirPin=17;
const int reedPin=13;
int reedState = HIGH;
int lastReedState = LOW;  // Assuming the door is closed at the start


const int buzzerPin = 32;
const int relayPin=33;
bool relayState = false;     // Relay is orignally OFF

enum SystemState : uint8_t {
  STATE_DEACTIVATED = 0,
  STATE_ARMED = 1,
  STATE_ALARM = 2,
};

SystemState current_state = STATE_DEACTIVATED;
SystemState lastLaserState = STATE_DEACTIVATED;

void sendSystemState(const char* state);
void sendAlarmTrigger(const char* cause);
void sendSensorStatus();
void sendLockState();
void requestStateSync();
void sendHeartbeat();
bool mqttPortReachable();
bool syncTimeFromNtp();
bool rtcLooksValid();
void setSystemTimeFromRtc();
void retryTimeSyncIfNeeded();
void onNtpTimeSync(struct timeval* tv);
bool applyNtpTimeToRtc();

const char* stateName(SystemState state) {
  if (state == STATE_ARMED) return "armed";
  if (state == STATE_ALARM) return "alarm";
  return "deactivated";
}

const char* boolText(bool value) {
  return value ? "true" : "false";
}

void setLockActive(bool active) {
  relayState = active;
  digitalWrite(relayPin, active ? HIGH : LOW);
}

void toggleLockIfAllowed() {
  if (current_state == STATE_ALARM) {
    setLockActive(true);
    return;
  }
  setLockActive(!relayState);
  sendLockState();
}

// Display update throttling so we don't redraw the oled every loop
unsigned long lastDisplayUpdate = 0;
const unsigned long displayInterval = 200; // time in ms between display refreshes (= abt 5 fps)

// Helper to format date and time
void formatDateTime(const DateTime& now, char* dateStr, char* timeStr){
  sprintf(dateStr, "%02d/%02d/%04d", now.day(), now.month(), now.year());
  sprintf(timeStr, "%02d:%02d", now.hour(), now.minute());
}

void drawConnectionIndicators() {
  oled.setFont(u8g2_font_5x8_tf);
  oled.drawStr(0, 63, WiFi.status() == WL_CONNECTED ? "WiFi:OK" : "WiFi:--");
  oled.drawStr(48, 63, mqttConnected ? "MQ:OK" : "MQ:--");
  oled.drawStr(88, 63, mainServerConnected ? "Pi:OK" : "Pi:--");
}

void applyArmState() {
  current_state = STATE_ARMED;
  armedTime = millis();
  lastReedState = digitalRead(reedPin);
  lastLdrValue = analogRead(ldrPin);
  noTone(buzzerPin);
  lastDisplayUpdate = 0;
  
  // נעילה אוטומטית רק במעבר ל-ARMED
  setLockActive(true); 
  sendLockState();
  
  sendSystemState("armed");
}

void applyDisarmState() {
  current_state = STATE_DEACTIVATED;
  noTone(buzzerPin);
  showingError = false;
  codeLength = 0;
  enteredCode[0] = '\0';
  lastDisplayUpdate = 0;
  sendSystemState("deactivated");
}

void applyAlarmState(const char* cause) {
  current_state = STATE_ALARM;
  setLockActive(true);
  lastDisplayUpdate = 0;
  sendSystemState("alarm");
  if (cause != nullptr && cause[0] != '\0') {
    sendAlarmTrigger(cause);
  }
}

bool resolveMqttServer() {
  if (WiFi.status() != WL_CONNECTED) return false;

  mqtt.setServer(mqttHostName, portNumber);
  IPAddress resolved;
  if (WiFi.hostByName(mqttHostName, resolved)) {
    mqttServerIp = resolved;
    Serial.print("[MQTT] Resolved ");
    Serial.print(mqttHostName);
    Serial.print(" -> ");
    Serial.println(mqttServerIp);
    mqttServerResolved = true;
    return true;
  }

  Serial.print("[MQTT] Could not resolve ");
  Serial.println(mqttHostName);
  mqttServerResolved = false;
  return false;
}

bool mqttPortReachable() {
  if (!mqttServerResolved) return false;

  WiFiClient probe;
  bool connected = probe.connect(mqttServerIp, portNumber, 500);
  probe.stop();
  if (!connected) {
    Serial.println("[MQTT] Broker port probe failed; staying offline.");
  }
  return connected;
}

void reconnectMQTT() {
  if (WiFi.status() != WL_CONNECTED) {
    mqttConnected = false;
    return;  //once the WiFi connection fails - the esp doesn't attempt to connect to the broker
  }
  unsigned long nowMs = millis();
  if (mqttConnectBackoffUntil != 0 && (long)(nowMs - mqttConnectBackoffUntil) < 0) {
    return;
  }
  if (!timeSynced) {
    retryTimeSyncIfNeeded();
  }
  if (nowMs - lastMqttReconnectAttempt < MQTT_RECONNECT_INTERVAL) {
    return;
  }
  lastMqttReconnectAttempt = nowMs;

  if (!mqtt.connected()) {
    if (!mqttServerResolved) {
      if (!resolveMqttServer()) {
        mqttConnectBackoffUntil = nowMs + MQTT_CONNECT_FAIL_BACKOFF;
        return;
      }
    }
    if (!mqttPortReachable()) {
      mqttConnected = false;
      mqttServerResolved = false;
      mqttConnectBackoffUntil = nowMs + MQTT_CONNECT_FAIL_BACKOFF;
      return;
    }
    mqtt.setServer(mqttHostName, portNumber);
    
    Serial.print("[MQTT] Attempting connection to ");
    Serial.print(mqttHostName);
    Serial.print("...");
    
	if (mqtt.connect("ESP32_Alarm", MQTT_USERNAME, MQTT_PASSWORD)) {
      Serial.println(" CONNECTED!");
      mqttConnected = true;
      mqttConnectBackoffUntil = 0;
      mqtt.subscribe("alarm/command"); 
      mqtt.subscribe("alarm/sensor");
      mqtt.subscribe("alarm/auth/response");
      mqtt.subscribe("alarm/heartbeat/main");
      Serial.println("[MQTT] Subscribed to topics: alarm/command, alarm/sensor, alarm/auth/response, alarm/heartbeat/main");
      requestStateSync();
      sendSensorStatus();
      sendLockState();
      sendHeartbeat();
    } else {
      Serial.print(" FAILED, rc=");
      Serial.print(mqtt.state());
      Serial.println(" -> Will try again");
      mqttConnected = false;
      mqttServerResolved = false;
      mqttConnectBackoffUntil = nowMs + MQTT_CONNECT_FAIL_BACKOFF;
    }
  }
}

void mqttSend(const char* topic, const char* msg){
  if (!mqttConnected) {
    Serial.println("[MQTT] Warning: Cannot send, MQTT offline.");
    return;   // If mqtt is offline, the system continues normally
  }
      
  Serial.print("[MQTT] PUBLISH -> Topic: ");
  Serial.print(topic);
  Serial.print(" | Message: ");
  Serial.println(msg);
  
  mqtt.publish(topic, msg);
}

void sendSystemState(const char* state) {
  if (mqttConnected) {                     //Only sends when MQTT is available, otherwise ignored
    mqttSend("alarm/state", state);
  }
}

void sendAlarmTrigger(const char* cause) {
  if (mqttConnected) {
    String payload = "{";
    payload += "\"sensor\":\"";
    payload += cause;
    payload += "\",";
    payload += "\"state\":\"";
    payload += stateName(current_state);
    payload += "\",";
    payload += "\"lock_active\":";
    payload += boolText(relayState);
    payload += ",";
    payload += "\"pir_enabled\":";
    payload += boolText(sensors.pir);
    payload += ",";
    payload += "\"ldr_enabled\":";
    payload += boolText(sensors.ldr);
    payload += ",";
    payload += "\"reed_enabled\":";
    payload += boolText(sensors.reed);
    payload += ",";
    payload += "\"millis\":";
    payload += String(millis());
    payload += "}";
    mqttSend("alarm/trigger", payload.c_str());
  }
}

void sendSensorStatus() {
  if (!mqttConnected) return;

  String payload = "{";
  payload += "\"pir\":{\"enabled\":";
  payload += boolText(sensors.pir);
  payload += ",\"active\":";
  payload += boolText(digitalRead(pirPin) == HIGH);
  payload += "},";
  payload += "\"ldr\":{\"enabled\":";
  payload += boolText(sensors.ldr);
  payload += ",\"value\":";
  payload += String(analogRead(ldrPin));
  payload += "},";
  payload += "\"reed\":{\"enabled\":";
  payload += boolText(sensors.reed);
  payload += ",\"open\":";
  payload += boolText(digitalRead(reedPin) == HIGH);
  payload += "},";
  payload += "\"state\":\"";
  payload += stateName(current_state);
  payload += "\",";
  payload += "\"lock_active\":";
  payload += boolText(relayState);
  payload += ",";
  payload += "\"online\":true,";
  payload += "\"millis\":";
  payload += String(millis());
  payload += "}";
  mqttSend("alarm/sensor/status", payload.c_str());
}

void sendLockState() {
  if (!mqttConnected) return;

  String payload = "{";
  payload += "\"active\":";
  payload += boolText(relayState);
  payload += ",";
  payload += "\"state\":\"";
  payload += stateName(current_state);
  payload += "\",";
  payload += "\"millis\":";
  payload += String(millis());
  payload += "}";
  mqttSend("alarm/lock/status", payload.c_str());
}

void requestStateSync() {
  if (!mqttConnected) return;

  String payload = "{";
  payload += "\"state\":\"";
  payload += stateName(current_state);
  payload += "\",";
  payload += "\"lock_active\":";
  payload += boolText(relayState);
  payload += ",";
  payload += "\"millis\":";
  payload += String(millis());
  payload += "}";
  mqttSend("alarm/state/request", payload.c_str());
}

void sendHeartbeat() {
  if (!mqttConnected) return;

  String payload = "{";
  payload += "\"device\":\"esp32-alarm\",";
  payload += "\"state\":\"";
  payload += stateName(current_state);
  payload += "\",";
  payload += "\"lock_active\":";
  payload += boolText(relayState);
  payload += ",";
  payload += "\"wifi_connected\":";
  payload += boolText(WiFi.status() == WL_CONNECTED);
  payload += ",";
  payload += "\"mqtt_connected\":";
  payload += boolText(mqttConnected);
  payload += ",";
  payload += "\"time_synced\":";
  payload += boolText(timeSynced);
  payload += ",";
  payload += "\"millis\":";
  payload += String(millis());
  payload += "}";
  mqttSend("alarm/heartbeat/esp", payload.c_str());
}

void setSensorEnabled(const String& name, bool enabled) {
  if (name == "PIR") sensors.pir = enabled;
  else if (name == "LDR") sensors.ldr = enabled;
  else if (name == "REED") sensors.reed = enabled;
  sendSensorStatus();
}

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String msg;
  for (unsigned int i = 0; i < length; i++) {
    msg += (char)payload[i];
  }
  msg.trim();

  Serial.print("[MQTT] RECEIVE <- Topic: ");
  Serial.print(topic);
  Serial.print(" | Message: ");
  Serial.println(msg);

  //SYSTEM COMMANDS 
  if (String(topic) == "alarm/command") {
    if (msg == "ARM") {
      applyArmState();
    }
    else if (msg == "DISARM") {
      applyDisarmState();
    }
    else if (msg == "ALARM") {
      applyAlarmState("REMOTE");
    }
    return;
  }

  //SENSOR CONTROL
  if (String(topic) == "alarm/sensor") {
    if (msg == "STATUS") sendSensorStatus();
    else if (msg == "PIR OFF") setSensorEnabled("PIR", false);
    else if (msg == "PIR ON") setSensorEnabled("PIR", true);
    else if (msg == "LDR OFF") setSensorEnabled("LDR", false);
    else if (msg == "LDR ON") setSensorEnabled("LDR", true);
    else if (msg == "REED OFF") setSensorEnabled("REED", false);
    else if (msg == "REED ON") setSensorEnabled("REED", true);
    return;
  }

  if (String(topic) == "alarm/heartbeat/main") {
    lastMainHeartbeat = millis();
    mainServerConnected = true;
    lastDisplayUpdate = 0;
    return;
  }

  //RESPONSE FROM PI 
  if (String(topic) == "alarm/auth/response") { // Check if this message is an auth response

    if (!authWaiting) return;  // If we are not waiting for auth, ignore message

    // Look for the "id" field in the message
    int idIndex = msg.indexOf("\"id\":");
    if (idIndex == -1) {
      Serial.println("[MQTT] Auth response rejected: No ID found.");
      return; 
    }

    // Find where the id number ends (comma after the number)
    int commaIndex = msg.indexOf(",", idIndex);

    // Extract the id number and convert it to integer
    uint32_t receivedId = msg.substring(
      idIndex + 5, // Skip '"id":'
      commaIndex
    ).toInt();

    // If this response is for a different request, ignore it
    if (receivedId != authRequestId) {
      Serial.print("[MQTT] Auth response rejected: ID mismatch (Expected ");
      Serial.print(authRequestId);
      Serial.print(", got ");
      Serial.print(receivedId);
      Serial.println(")");
      return;
    }

    // Check if the result is "OK"
    if (msg.indexOf("\"result\":\"OK\"") != -1) {
      authResult = true; // Password is correct
      Serial.println("[MQTT] Auth check: SUCCESS");
    } else {
      authResult = false;// Password is wrong
      Serial.println("[MQTT] Auth check: FAILED");
    }

    authWaiting = false;// Stop waiting for auth response
  }
}

void onNtpTimeSync(struct timeval* tv) {
  (void)tv;
  ntpSyncCompleted = true;
}

bool applyNtpTimeToRtc() {
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo, 1500)) {
    Serial.println("[TIME] NTP synced, but local time read failed; keeping RTC time.");
    ntpSyncCompleted = false;
    return false;
  }

  rtc.adjust(DateTime(
    timeinfo.tm_year + 1900,
    timeinfo.tm_mon + 1,
    timeinfo.tm_mday,
    timeinfo.tm_hour,
    timeinfo.tm_min,
    timeinfo.tm_sec
  ));
  Serial.println("[TIME] RTC synced from NTP.");
  timeSynced = true;
  ntpTimeSynced = true;
  lastDisplayUpdate = 0;
  return true;
}

bool syncTimeFromNtp() {
  if (WiFi.status() != WL_CONNECTED) return false;
  if (millis() - lastNtpSyncAttempt < NTP_RETRY_INTERVAL) return false;
  lastNtpSyncAttempt = millis();

  Serial.println("[TIME] Starting NTP sync...");
  ntpSyncCompleted = false;
  sntp_set_time_sync_notification_cb(onNtpTimeSync);
  configTzTime(timeZone, ntpServer1, ntpServer2);
  return false;
}

bool rtcLooksValid() {
  DateTime now = rtc.now();
  return now.year() >= 2024;
}

void setSystemTimeFromRtc() {
  if (!rtcLooksValid()) return;

  DateTime now = rtc.now();
  struct tm timeinfo = {};
  timeinfo.tm_year = now.year() - 1900;
  timeinfo.tm_mon = now.month() - 1;
  timeinfo.tm_mday = now.day();
  timeinfo.tm_hour = now.hour();
  timeinfo.tm_min = now.minute();
  timeinfo.tm_sec = now.second();
  setenv("TZ", timeZone, 1);
  tzset();
  time_t epoch = mktime(&timeinfo);
  if (epoch <= 0) return;

  timeval tv;
  tv.tv_sec = epoch;
  tv.tv_usec = 0;
  settimeofday(&tv, nullptr);
  timeSynced = true;
  Serial.println("[TIME] System time initialized from RTC.");
}

void retryTimeSyncIfNeeded() {
  if (ntpTimeSynced) return;
  if (ntpSyncCompleted && applyNtpTimeToRtc()) return;
  if (!timeSynced) {
    setSystemTimeFromRtc();
  }
  if (WiFi.status() != WL_CONNECTED) return;
  if (syncTimeFromNtp()) return;
}

void setup(){
  Wire.begin();
  oled.begin();
  Serial.begin(115200);
  rtc.begin();
  //rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
  Serial.println("Connecting to WiFi...");
  WiFi.begin(ssid,wifi_Password); 
  
  // Wait briefly to see if it connects immediately
  delay(1000);
  
  if (WiFi.status()!= WL_CONNECTED){
    Serial.println("WiFi not connected yet; running offline");
  } else {
    Serial.println("WiFi Connected!");
  }
  setSystemTimeFromRtc();
  
  pinMode(relayPin,OUTPUT);
  setLockActive(false);
  pinMode(buzzerPin, OUTPUT);
  pinMode(pirPin,INPUT);
  pinMode(reedPin,INPUT_PULLUP);
  // Keypad init
  for(int i=0;i<4;i++){
    pinMode(rowPins[i],INPUT_PULLUP);
  }
  for (int k=0;k<4;k++){
    pinMode(colPins[k],OUTPUT);
    digitalWrite(colPins[k],HIGH);
  }
  pinMode(laserPin,OUTPUT);
  lastLdrValue = analogRead(ldrPin);

  MDNS.begin("esp32-alarm");
  lastNtpSyncAttempt = millis() - NTP_RETRY_INTERVAL;
  retryTimeSyncIfNeeded();
  mqtt.setCallback(mqttCallback);
  mqtt.setSocketTimeout(1);
  mqtt.setKeepAlive(15);
  secureClient.setCACert(root_ca);
  secureClient.setTimeout(1000);
  secureClient.setHandshakeTimeout(2);
  
  Serial.println("[MQTT] Configuration set, attempting initial connection...");
  lastMqttReconnectAttempt = millis() - MQTT_RECONNECT_INTERVAL;
  reconnectMQTT();
}

void loop(){
  retryTimeSyncIfNeeded();
  if (!mqtt.connected()) {
    mqttConnected = false;
    mainServerConnected = false;
    reconnectMQTT(); 
  } else {
    mqttConnected = true;
    mqtt.loop();
  }
  DateTime now = rtc.now(); 
  char key = scan_key();
  reedState=digitalRead(reedPin);
  int motion= digitalRead(pirPin);
  unsigned long nowMs = millis();
  if (mainServerConnected && nowMs - lastMainHeartbeat > MAIN_HEARTBEAT_TIMEOUT) {
    mainServerConnected = false;
    lastDisplayUpdate = 0;
  }
  bool updateDisplay = false; 
  if (nowMs - lastDisplayUpdate >= displayInterval) {
    updateDisplay = true;
    lastDisplayUpdate = nowMs;
  }

  if (current_state == STATE_DEACTIVATED) {
    deactivateMode(now, updateDisplay);
    if (key == 'A') {
      applyArmState();
    }
    else if (key == 'B') {
      toggleLockIfAllowed();
    }
  }
  else if (current_state == STATE_ARMED) {
    if (key == 'B') {
      toggleLockIfAllowed();
      key = 0;
    }
    armMode(key ,now, updateDisplay);

    // Only read the LDR when the system is armed
    int ldrValue = analogRead(ldrPin);
    
    if (millis() - armedTime <= LDR_IGNORE_TIME) {
      lastLdrValue = ldrValue;
    }
    else if (sensors.ldr && abs(ldrValue - lastLdrValue) > lightThreshold) {
      applyAlarmState("LDR");
    }
    else if (sensors.pir && motion == HIGH) {
      applyAlarmState("PIR");
    }
    else if (sensors.reed && lastReedState == LOW && reedState == HIGH) {
      applyAlarmState("REED");
    }
  }

  if (current_state == STATE_ALARM){
    setLockActive(true);
    alarmMode(key, now, updateDisplay);
  }

  if (mqttConnected && millis() - lastSensorStatusPublish >= SENSOR_STATUS_INTERVAL) {
    lastSensorStatusPublish = millis();
    sendSensorStatus();
  }
  if (mqttConnected && millis() - lastHeartbeatPublish >= HEARTBEAT_INTERVAL) {
    lastHeartbeatPublish = millis();
    sendHeartbeat();
  }
  updateLaser(current_state);

  lastReedState = reedState;
  delay(15);
}

char scan_key(){
  for (int col=0;col<4;col++){
    digitalWrite(colPins[col], LOW);

    for (int row=0;row<4;row++){
      if (digitalRead(rowPins[row]) == LOW){
        char pressed = hexaKeys[row][col];

        while (digitalRead(rowPins[row]) == LOW) {
          // waiting for release
          delay(5);
        }

        digitalWrite(colPins[col], HIGH);
        return pressed;
      }
    }

    digitalWrite(colPins[col], HIGH);
  }
  return 0;
}

void alarmMode(char key, const DateTime& now, bool updateDisplay){
  playAlarm();
  enter_code(key, now, updateDisplay, "Alarm activated!");
}

void armMode(char key, const DateTime& now, bool updateDisplay) {
  enter_code(key, now, updateDisplay, "System Armed!");
}

void playAlarm() { 
  if (millis() - lastAlarmTime >= 120) {
    lastAlarmTime = millis();

    tone(buzzerPin, alarmFreq[alarmIndex]);
    alarmIndex = (alarmIndex + 1) % numAlarmFreq;
  }
}

String sha256_hex(const String &input) {
  byte output[32];

  mbedtls_sha256((const unsigned char*)input.c_str(), input.length(), output, 0);//calculate SHA-256

  char hexString[65];

  for (int i = 0; i < 32; i++) {
    sprintf(hexString + i * 2, "%02x", output[i]); // convert from binary to hex
  }

  hexString[64] = 0;

  return String(hexString);
}

void deactivateMode(const DateTime& now, bool updateDisplay) {

  if (!updateDisplay) {
    return;  
  }

  char dateStr[12];
  char timeStr[10];
  formatDateTime(now, dateStr, timeStr);

  oled.clearBuffer();
  oled.setFont(u8g2_font_6x12_tf);

  oled.drawStr(0, 12, dateStr);
  oled.drawStr(80, 12, timeStr);
  oled.drawStr(0, 40, "System Deactivated!");
  drawConnectionIndicators();

  oled.sendBuffer();
}

void enter_code(char key, const DateTime& now, bool updateDisplay, const char* title) {

  char dateStr[12];
  char timeStr[10];
  formatDateTime(now, dateStr, timeStr);

  if (key == 'D' && codeLength > 0) {
    codeLength--;
    enteredCode[codeLength] = '\0';
  }
  else if (key >= '0' && key <= '9' && codeLength < 4) {
      enteredCode[codeLength] = key;
      codeLength++;
      enteredCode[codeLength] = '\0';
  }

  if (key == '#') {
    String enteredHash = sha256_hex(String(enteredCode));

    if (verifyPassword(enteredHash)) {
      applyDisarmState();
      return;
    } else {
      showingError = true;
      errorStartTime = millis();
      codeLength = 0;
      enteredCode[0] = '\0';
    }
  }

  if (!updateDisplay) return;

  if (showingError) {
    if (millis() - errorStartTime < 1000) {
      oled.clearBuffer();
      oled.setFont(u8g2_font_6x12_tf);
      oled.drawStr(0, 12, dateStr);
      oled.drawStr(80, 12, timeStr);
      oled.drawStr(0, 24, title);
      oled.drawStr(0, 36, "Password:");
      oled.drawStr(0, 48, "Try Again!");
      drawConnectionIndicators();
      oled.sendBuffer();
      return;
    } 
    else {
      showingError = false;
    }
  }

  oled.clearBuffer();
  oled.setFont(u8g2_font_6x12_tf);
  oled.drawStr(0, 12, dateStr);
  oled.drawStr(80, 12, timeStr);
  oled.drawStr(0, 24, title);
  oled.drawStr(0, 36, "Password:");
  oled.drawStr(60, 36, enteredCode);
  drawConnectionIndicators();
  oled.sendBuffer();
}

void updateLaser(SystemState state) {
  if (state == lastLaserState) return;

  if (state == STATE_ARMED) {
    digitalWrite(laserPin, HIGH);   
  } else {
    digitalWrite(laserPin, LOW);    
  }
  lastLaserState = state;
}

bool verifyPassword(const String& enteredHash) {
  authWaiting = false; 
  authResult  = false;
  
  if (mqttConnected) {
    authRequestId++;
    authWaiting = true;
    authStartTime = millis();

    String payload = "{";
    payload += "\"id\":" + String(authRequestId) + ",";
    payload += "\"hash\":\"" + enteredHash + "\"";
    payload += "}";

    mqtt.publish("alarm/auth/request", payload.c_str());
    Serial.println("[MQTT] Auth request sent; local check continues without blocking.");
  }

  
  Serial.println("[AUTH] Performing local hash check...");
  return enteredHash.equalsIgnoreCase(password_hash);
}