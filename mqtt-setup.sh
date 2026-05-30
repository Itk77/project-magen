#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Mosquitto MQTT + TLS setup (Raspberry Pi / Debian)
#
# What this script does:
# 1) Installs mosquitto, clients, openssl
# 2) Creates username/password auth file
# 3) Creates a local CA + server certificate (TLS for port 8883)
# 4) Writes mosquitto config in /etc/mosquitto/conf.d/
# 5) Enables + restarts the mosquitto service
# 6) Runs a small TLS pub/sub self-test on localhost
#
# Notes:
# - This creates a local CA. Clients must trust /etc/mosquitto/certs/ca.crt.
# - By default, TLS cert SANs include:
#     DNS: localhost, DNS: <hostname>, DNS: <hostname>.local
#   Clients should connect using one of those DNS names instead of a changing IP.
#
# Security note:
# - The password is hardcoded by default to match your example ("1234").
#   This is weak. For real deployments, change MQTT_PASS_* vars or export them
#   when running the script.
# - This script uses `mosquitto_passwd -b` (batch mode), which can briefly expose
#   the password in the process list while the command runs. For a one-time setup
#   this is usually fine, but not ideal. If you care, switch to interactive mode.
###############################################################################

# --- args ---
FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

# --- configurable variables (override by exporting before running) ---
MQTT_USER_1="${MQTT_USER_1:-main_server}"
MQTT_PASS_1="${MQTT_PASS_1:-1234}"
MQTT_USER_2="${MQTT_USER_2:-sensors_unit}"
MQTT_PASS_2="${MQTT_PASS_2:-1234}"

# Certificate identity / SANs
BROKER_HOSTNAME="${BROKER_HOSTNAME:-$(hostname)}"
BROKER_LOCAL_HOSTNAME="${BROKER_LOCAL_HOSTNAME:-${BROKER_HOSTNAME}.local}"

# Listener addresses
# - Plaintext is bound to 127.0.0.1 by default (local only).
# - TLS binds to 0.0.0.0 by default (accessible from LAN if Pi is reachable).
ENABLE_PLAINTEXT="${ENABLE_PLAINTEXT:-true}"
PLAINTEXT_BIND="${PLAINTEXT_BIND:-127.0.0.1}"
TLS_BIND="${TLS_BIND:-0.0.0.0}"

# Paths
PASSWD_FILE="/etc/mosquitto/passwd"
CERT_DIR="/etc/mosquitto/certs"
CONF_DIR="/etc/mosquitto/conf.d"
CONF_FILE="${CONF_DIR}/mosquitto-tls.conf"

CA_KEY="${CERT_DIR}/ca.key"
CA_CRT="${CERT_DIR}/ca.crt"
SERVER_KEY="${CERT_DIR}/server.key"
SERVER_CSR="${CERT_DIR}/server.csr"
SERVER_CRT="${CERT_DIR}/server.crt"
SAN_CNF="${CERT_DIR}/san.cnf"

# --- helpers ---
log() { echo "[setup] $*"; }

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Please run as root: sudo $0"
    exit 1
  fi
}

###############################################################################
# Start
###############################################################################
need_root

log "Installing packages..."
apt update
apt install -y mosquitto mosquitto-clients openssl

log "Creating mosquitto password file at ${PASSWD_FILE}..."
# -c only on first creation; if --force, recreate the file
if [[ "${FORCE}" -eq 1 && -f "${PASSWD_FILE}" ]]; then
  rm -f "${PASSWD_FILE}"
fi

# Create password file (first user with -c, second without -c)
if [[ ! -f "${PASSWD_FILE}" ]]; then
  mosquitto_passwd -b -c "${PASSWD_FILE}" "${MQTT_USER_1}" "${MQTT_PASS_1}"
else
  mosquitto_passwd -b "${PASSWD_FILE}" "${MQTT_USER_1}" "${MQTT_PASS_1}"
fi
mosquitto_passwd -b "${PASSWD_FILE}" "${MQTT_USER_2}" "${MQTT_PASS_2}"

chown root:mosquitto "${PASSWD_FILE}"
chmod 640 "${PASSWD_FILE}"

log "Setting up cert directory ${CERT_DIR}..."
mkdir -p "${CERT_DIR}"

# The CA cert is public material and clients may need to read it even when they
# are not in the mosquitto group, so the directory must be traversable.
chown root:mosquitto "${CERT_DIR}"
chmod 755 "${CERT_DIR}"

log "Generating CA + server certs (skip if already exist; use --force to regenerate)..."
if [[ "${FORCE}" -eq 1 ]]; then
  rm -f "${CA_KEY}" "${CA_CRT}" "${SERVER_KEY}" "${SERVER_CSR}" "${SERVER_CRT}" "${SAN_CNF}" "${CERT_DIR}/ca.srl"
fi

# 1) CA
if [[ ! -f "${CA_KEY}" || ! -f "${CA_CRT}" ]]; then
  log "Creating CA key/cert..."
  openssl genrsa -out "${CA_KEY}" 4096
  openssl req -x509 -new -nodes -key "${CA_KEY}" -sha256 -days 3650 -out "${CA_CRT}" \
    -subj "/C=IL/O=LocalMQTT/OU=CA/CN=LocalMQTT-CA"
fi

# 2) Server key + CSR
if [[ ! -f "${SERVER_KEY}" || ! -f "${SERVER_CSR}" ]]; then
  log "Creating server key/CSR (CN=${BROKER_HOSTNAME})..."
  openssl genrsa -out "${SERVER_KEY}" 2048
  openssl req -new -key "${SERVER_KEY}" -out "${SERVER_CSR}" \
    -subj "/C=IL/O=LocalMQTT/OU=Server/CN=${BROKER_HOSTNAME}"
fi

# 3) SAN config (what names the cert is valid for)
# Add more DNS entries if clients connect via a different stable hostname.
cat > "${SAN_CNF}" <<EOF
[ v3_req ]
subjectAltName = @alt_names

[ alt_names ]
DNS.1 = localhost
DNS.2 = ${BROKER_HOSTNAME}
DNS.3 = ${BROKER_LOCAL_HOSTNAME}
EOF

# 4) Sign server cert. This refreshes the broker certificate while keeping the
# existing CA, so clients do not need a new CA unless --force regenerated it.
log "Signing server certificate with SANs (includes ${BROKER_HOSTNAME} and ${BROKER_LOCAL_HOSTNAME})..."
openssl x509 -req -in "${SERVER_CSR}" -CA "${CA_CRT}" -CAkey "${CA_KEY}" -CAcreateserial \
  -out "${SERVER_CRT}" -days 825 -sha256 \
  -extfile "${SAN_CNF}" -extensions v3_req

# File permissions: mosquitto must be able to read server.key
chown root:mosquitto "${SERVER_KEY}"
chmod 640 "${SERVER_KEY}"
chmod 644 "${CA_CRT}" "${SERVER_CRT}"

log "Writing mosquitto config to ${CONF_FILE}..."
mkdir -p "${CONF_DIR}"

# Build config with optional plaintext listener
{
  echo "per_listener_settings true"
  echo
  if [[ "${ENABLE_PLAINTEXT}" == "true" ]]; then
    echo "# Plain MQTT (local only by default)"
    echo "listener 1883 ${PLAINTEXT_BIND}"
    echo "protocol mqtt"
    echo "allow_anonymous false"
    echo "password_file ${PASSWD_FILE}"
    echo
  fi
  echo "# TLS MQTT"
  echo "listener 8883 ${TLS_BIND}"
  echo "protocol mqtt"
  echo "cafile ${CA_CRT}"
  echo "certfile ${SERVER_CRT}"
  echo "keyfile ${SERVER_KEY}"
  echo "tls_version tlsv1.2"
  echo "allow_anonymous false"
  echo "password_file ${PASSWD_FILE}"
  echo
  echo "log_type error"
  echo "log_type warning"
  echo "log_type notice"
  echo "log_type information"
} > "${CONF_FILE}"

# Ensure main config includes conf.d (usually already true on Debian/Ubuntu)
if ! grep -qE '^\s*include_dir\s+/etc/mosquitto/conf\.d\s*$' /etc/mosquitto/mosquitto.conf; then
  log "Adding include_dir /etc/mosquitto/conf.d to /etc/mosquitto/mosquitto.conf ..."
  echo "include_dir /etc/mosquitto/conf.d" >> /etc/mosquitto/mosquitto.conf
fi

log "Enabling + restarting mosquitto..."
systemctl enable mosquitto >/dev/null 2>&1 || true
systemctl restart mosquitto

log "Mosquitto status:"
systemctl status mosquitto --no-pager -l || true

log "Checking listeners (1883/8883)..."
ss -lntp | grep -E ':(1883|8883)\b' || true

###############################################################################
# Self-test (TLS on localhost)
###############################################################################
log "Running TLS self-test on localhost:8883..."

TEST_OUT="$(mktemp)"
# Subscribe in background, exit after 1 message, with a timeout
timeout 8 mosquitto_sub -h localhost -p 8883 -t test/topic -v -C 1 \
  --cafile "${CA_CRT}" \
  -u "${MQTT_USER_1}" -P "${MQTT_PASS_1}" \
  > "${TEST_OUT}" 2>/dev/null &

SUB_PID=$!
sleep 0.6

mosquitto_pub -h localhost -p 8883 -t test/topic -m "hello over TLS" \
  --cafile "${CA_CRT}" \
  -u "${MQTT_USER_1}" -P "${MQTT_PASS_1}" \
  >/dev/null 2>&1 || true

wait "${SUB_PID}" >/dev/null 2>&1 || true

if grep -q "test/topic hello over TLS" "${TEST_OUT}"; then
  log "Self-test OK: TLS pub/sub works."
else
  log "Self-test FAILED (did not see expected message)."
  log "Output was:"
  cat "${TEST_OUT}" || true
  log "Check logs with: sudo journalctl -u mosquitto -b --no-pager | tail -n 200"
fi

rm -f "${TEST_OUT}"

log "Done."
log "Client CA cert to copy to clients: ${CA_CRT}"
