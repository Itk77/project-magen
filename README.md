# ESP32 Alarm Firmware

This firmware runs the sensor/keypad unit for the Magen alarm system.

## Table of Contents

- [Hardware Roles](#hardware-roles)
- [Keypad Controls](#keypad-controls)
- [Alarm States](#alarm-states)
- [OLED Status Indicators](#oled-status-indicators)
- [MQTT Discovery](#mqtt-discovery)
- [Time and TLS](#time-and-tls)
- [MQTT Topics](#mqtt-topics)
- [Offline Behavior](#offline-behavior)
- [Password Hash](#password-hash)

## Hardware Roles

- Keypad: enters the alarm password and local commands.
- PIR, LDR, REED: alarm trigger sensors.
- Relay on GPIO 33: lock output. Active means the door should stay locked.
- Buzzer on GPIO 32: alarm sound.
- Laser on GPIO 14: enabled while the system is armed.

## Keypad Controls

- `A`: arm the system from deactivated mode.
- `#`: submit the currently entered 4 digit numeric code.
- `D`: delete one entered digit.
- `B`: toggle the lock while the system is deactivated or armed.

During password entry, only number keys are added to the password. Other keypad keys are ignored except `D` for delete and `#` for submit.

When the alarm is active, the lock is always forced active and cannot be toggled off.

## Alarm States

- `deactivated`: sensors do not trigger the alarm.
- `armed`: enabled sensors can trigger the alarm. Entering this state automatically activates the lock.
- `alarm`: buzzer runs, lock is forced active, password can disarm.

## OLED Status Indicators

The bottom line of the OLED shows connection health:

- `WiFi:OK` / `WiFi:--`: Wi-Fi connection.
- `MQ:OK` / `MQ:--`: MQTT broker connection.
- `Pi:OK` / `Pi:--`: main server heartbeat over MQTT.

## MQTT Discovery

The firmware no longer hardcodes the Raspberry Pi IP address. After Wi-Fi connects it resolves:

```text
raspberrypi.local
```

This should match the address you get from a LAN machine with:

```text
ping raspberrypi.local
```

The MQTT port is `8883`.

## Time and TLS

Before connecting to MQTT, the ESP syncs its clock over NTP using Wi-Fi. This is required for TLS certificate validation because certificates have valid-from and valid-until dates.

The ESP connects to MQTT using `raspberrypi.local` and trusts the pasted `root_ca` certificate in the firmware. The Mosquitto server certificate should include `DNS:raspberrypi.local`; avoid relying on LAN IP addresses because they can change.

## MQTT Topics

Subscribed topics:

- `alarm/command`: `ARM`, `DISARM`, or `ALARM`
- `alarm/sensor`: `STATUS`, `PIR ON`, `PIR OFF`, `LDR ON`, `LDR OFF`, `REED ON`, `REED OFF`
- `alarm/heartbeat/main`: main server heartbeat used for the Pi connection indicator
- `alarm/auth/response`: `{"id":<number>,"result":"OK"|"FAIL"}`

Published topics:

- `alarm/state`: `armed`, `deactivated`, or `alarm`
- `alarm/state/request`: ESP asks the Pi to send back its desired state after MQTT reconnect.
- `alarm/trigger`: JSON metadata with the triggering sensor, state, lock state, and enabled sensor flags
- `alarm/auth/request`: `{"id":<number>,"hash":"<sha256_of_code>"}`
- `alarm/sensor/status`: JSON status for PIR, LDR, REED, lock, and alarm state
- `alarm/lock/status`: JSON lock status
- `alarm/heartbeat/esp`: ESP heartbeat used by the main server and website connection indicators

## Offline Behavior

The ESP32 keeps working if the Raspberry Pi or MQTT broker is unavailable.

- MQTT reconnects are throttled, so the main loop is not trapped in rapid retry logic.
- Password checking always falls back to the local SHA-256 hash.
- Sensor triggers and lock behavior continue locally.

## Password Hash

The firmware stores a SHA-256 hash, not the plaintext password. The entered code is hashed with `mbedtls_sha256()` and compared locally.

When MQTT is connected, the ESP publishes the hash to the Pi on `alarm/auth/request`; it does not send the plaintext code. The local hash check still decides immediately, so the ESP can disarm even if the Pi or MQTT broker is unavailable.
