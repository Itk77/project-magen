#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# TLS certificate setup only (Raspberry Pi / Debian)
#
# What this script does:
# 1) Creates / refreshes the Mosquitto cert directory
# 2) Generates a local CA certificate
# 3) Generates a server key + CSR
# 4) Signs the server certificate with SANs
#
# What this script does NOT do:
# - install packages
# - create MQTT users/passwords
# - write mosquitto config
# - restart services
#
# Notes:
# - Clients must trust /etc/mosquitto/certs/ca.crt.
# - By default, SANs include:
#     DNS: localhost, DNS: <hostname>, DNS: <hostname>.local
# - Clients should connect using one of those DNS names instead of a changing IP.
###############################################################################

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

BROKER_HOSTNAME="${BROKER_HOSTNAME:-$(hostname)}"
CERT_DIR="${CERT_DIR:-/etc/mosquitto/certs}"
CA_NOT_BEFORE="${CA_NOT_BEFORE:-}"
CA_NOT_AFTER="${CA_NOT_AFTER:-}"
SERVER_NOT_BEFORE="${SERVER_NOT_BEFORE:-}"
SERVER_NOT_AFTER="${SERVER_NOT_AFTER:-}"

CA_KEY="${CERT_DIR}/ca.key"
CA_CRT="${CERT_DIR}/ca.crt"
SERVER_KEY="${CERT_DIR}/server.key"
SERVER_CSR="${CERT_DIR}/server.csr"
SERVER_CRT="${CERT_DIR}/server.crt"
SAN_CNF="${CERT_DIR}/san.cnf"

log() { echo "[cert-setup] $*"; }

append_validity_args() {
  local -n out_ref=$1
  local not_before=$2
  local not_after=$3

  if [[ -n "${not_before}" ]]; then
    out_ref+=("-not_before" "${not_before}")
  fi
  if [[ -n "${not_after}" ]]; then
    out_ref+=("-not_after" "${not_after}")
  fi
}

print_esp_ca_block() {
  local cert_path=$1

  echo "ESP32 root_ca block:"
  echo 'const char* root_ca = \'
  awk '{printf "\"%s\\n\" \\\n", $0}' "${cert_path}"
  echo ';'
}

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Please run as root: sudo $0"
    exit 1
  fi
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

need_root
need_cmd openssl
need_cmd hostname
need_cmd awk
BROKER_LOCAL_HOSTNAME="${BROKER_LOCAL_HOSTNAME:-${BROKER_HOSTNAME}.local}"

log "Setting up cert directory ${CERT_DIR}..."
mkdir -p "${CERT_DIR}"
chown root:mosquitto "${CERT_DIR}" 2>/dev/null || true
chmod 755 "${CERT_DIR}"

if [[ "${FORCE}" -eq 1 ]]; then
  log "Removing existing certificate artifacts..."
  rm -f "${CA_KEY}" "${CA_CRT}" "${SERVER_KEY}" "${SERVER_CSR}" "${SERVER_CRT}" "${SAN_CNF}" "${CERT_DIR}/ca.srl"
fi

if [[ ! -f "${CA_KEY}" || ! -f "${CA_CRT}" ]]; then
  log "Creating CA key/cert..."
  ca_validity_args=()
  append_validity_args ca_validity_args "${CA_NOT_BEFORE}" "${CA_NOT_AFTER}"
  openssl genrsa -out "${CA_KEY}" 4096
  openssl req -x509 -new -nodes -key "${CA_KEY}" -sha256 -days 3650 -out "${CA_CRT}" \
    "${ca_validity_args[@]}" \
    -subj "/C=IL/O=LocalMQTT/OU=CA/CN=LocalMQTT-CA"
else
  log "Keeping existing CA key/cert."
fi

if [[ ! -f "${SERVER_KEY}" || ! -f "${SERVER_CSR}" ]]; then
  log "Creating server key/CSR (CN=${BROKER_HOSTNAME})..."
  openssl genrsa -out "${SERVER_KEY}" 2048
  openssl req -new -key "${SERVER_KEY}" -out "${SERVER_CSR}" \
    -subj "/C=IL/O=LocalMQTT/OU=Server/CN=${BROKER_HOSTNAME}"
else
  log "Keeping existing server key/CSR."
fi

log "Writing SAN config to ${SAN_CNF}..."
cat > "${SAN_CNF}" <<EOF
[ v3_req ]
subjectAltName = @alt_names

[ alt_names ]
DNS.1 = localhost
DNS.2 = ${BROKER_HOSTNAME}
DNS.3 = ${BROKER_LOCAL_HOSTNAME}
EOF

log "Signing server certificate with SANs (includes ${BROKER_HOSTNAME} and ${BROKER_LOCAL_HOSTNAME})..."
server_validity_args=()
append_validity_args server_validity_args "${SERVER_NOT_BEFORE}" "${SERVER_NOT_AFTER}"
openssl x509 -req -in "${SERVER_CSR}" -CA "${CA_CRT}" -CAkey "${CA_KEY}" -CAcreateserial \
  -out "${SERVER_CRT}" -days 825 -sha256 \
  "${server_validity_args[@]}" \
  -extfile "${SAN_CNF}" -extensions v3_req

chown root:mosquitto "${SERVER_KEY}" 2>/dev/null || true
chmod 640 "${SERVER_KEY}" 2>/dev/null || true
chmod 644 "${CA_CRT}" "${SERVER_CRT}" "${SAN_CNF}"

log "Done."
log "CA cert: ${CA_CRT}"
log "Server cert: ${SERVER_CRT}"
log "Server key: ${SERVER_KEY}"
if [[ -n "${CA_NOT_BEFORE}${CA_NOT_AFTER}" ]]; then
  log "CA validity override: not_before='${CA_NOT_BEFORE:-default}' not_after='${CA_NOT_AFTER:-default}'"
fi
if [[ -n "${SERVER_NOT_BEFORE}${SERVER_NOT_AFTER}" ]]; then
  log "Server validity override: not_before='${SERVER_NOT_BEFORE:-default}' not_after='${SERVER_NOT_AFTER:-default}'"
fi
print_esp_ca_block "${CA_CRT}"
