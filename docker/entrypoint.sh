#!/bin/bash
# entrypoint.sh – patches rsyslog config with env-supplied DSF gateway
# coordinates, then launches rsyslog and the Python tailer side-by-side.
#
# Required environment variables:
#   DSF_GATEWAY_HOST   IP or hostname of the Thales DSF Agentless Gateway
#   DSF_GATEWAY_PORT   Syslog port on the gateway  (default: 514)
#   DSF_PROTOCOL       tcp or udp                  (default: tcp)
#   TARGET_NAMESPACE   Kubernetes namespace to watch (default: default)
#
# Optional:
#   AUDIT_DIR          Where to write pgaudit files (default: /audit/pgaudit)
#   POD_LABEL_SELECTOR Kubernetes label selector    (default: cnpg.io/cluster)
#   CONTAINER_NAME     Postgres container name       (default: postgres)
#   RECONNECT_DELAY    Seconds between retries       (default: 5)
#   LOG_LEVEL          Python log level              (default: INFO)

set -euo pipefail

: "${DSF_GATEWAY_HOST:?ERROR: DSF_GATEWAY_HOST must be set}"
: "${DSF_GATEWAY_PORT:=514}"
: "${DSF_PROTOCOL:=tcp}"
: "${AUDIT_DIR:=/audit/pgaudit}"

echo "[entrypoint] DSF gateway: ${DSF_PROTOCOL}://${DSF_GATEWAY_HOST}:${DSF_GATEWAY_PORT}"

# ── Patch rsyslog config ───────────────────────────────────────────────────
RSYSLOG_CONF="/etc/rsyslog.d/50-pgaudit-dsf.conf"

sed -i \
  -e "s|__DSF_GATEWAY_HOST__|${DSF_GATEWAY_HOST}|g" \
  -e "s|__DSF_GATEWAY_PORT__|${DSF_GATEWAY_PORT}|g" \
  -e "s|__DSF_PROTOCOL__|${DSF_PROTOCOL}|g" \
  "${RSYSLOG_CONF}"

echo "[entrypoint] rsyslog config patched"

# ── Ensure required directories exist ─────────────────────────────────────
mkdir -p "${AUDIT_DIR}"
mkdir -p /var/spool/rsyslog

# ── Start rsyslog in foreground (background via &) ─────────────────────────
rsyslogd -n &
RSYSLOG_PID=$!
echo "[entrypoint] rsyslogd started (pid ${RSYSLOG_PID})"

# Give rsyslog a moment to initialise before the tailer starts writing
sleep 2

# ── Start the Python log tailer ────────────────────────────────────────────
exec python3 /app/tailer.py &
TAILER_PID=$!
echo "[entrypoint] tailer started (pid ${TAILER_PID})"

# ── Wait and propagate signals ─────────────────────────────────────────────
_term() {
  echo "[entrypoint] Caught signal – stopping children"
  kill -TERM "${TAILER_PID}" 2>/dev/null || true
  kill -TERM "${RSYSLOG_PID}" 2>/dev/null || true
}
trap _term SIGTERM SIGINT

# Exit when either child exits
wait -n "${TAILER_PID}" "${RSYSLOG_PID}"
EXIT_CODE=$?

# Clean up the other child
kill "${TAILER_PID}"  2>/dev/null || true
kill "${RSYSLOG_PID}" 2>/dev/null || true

echo "[entrypoint] exiting with code ${EXIT_CODE}"
exit "${EXIT_CODE}"
