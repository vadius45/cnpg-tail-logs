# ─── pgaudit-forwarder ────────────────────────────────────────────────────────
# Ubuntu-based container that:
#   1. Tails CloudNativePG pod logs ONLY in its own namespace
#   2. Extracts pgaudit events and writes them to a PVC-backed folder
#   3. Ships those events to a Thales DSF Agentless Gateway via rsyslog/TCP syslog
# ─────────────────────────────────────────────────────────────────────────────

FROM ubuntu:24.04

LABEL org.opencontainers.image.title="pgaudit-forwarder" \
      org.opencontainers.image.description="Forwards CloudNativePG pgaudit events (same namespace only) to Thales DSF" \
      org.opencontainers.image.source="https://github.com/your-org/pgaudit-forwarder"

# ── System packages ───────────────────────────────────────────────────────────
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        rsyslog \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*


# ── Python dependencies ───────────────────────────────────────────────────────
COPY app/requirements.txt /tmp/requirements.txt
RUN apt-get remove -y python3-pip python3-setuptools python3-wheel || true && \
    rm -f /usr/lib/python*/EXTERNALLY-MANAGED && \
    curl -sSL https://bootstrap.pypa.io/get-pip.py | python3 && \
    python3 -m pip install --no-cache-dir setuptools>=78.1.1 wheel>=0.46.2 kubernetes>=28.1.0

# ── Application code ──────────────────────────────────────────────────────────
COPY app/tailer.py /app/tailer.py

# ── rsyslog base config hardening ─────────────────────────────────────────────
# Disable modules we don't need (imudp / imtcp listeners – we only *send*)
RUN sed -i \
        -e '/^module(load="imudp")/d' \
        -e '/^input(type="imudp"/d' \
        -e '/^module(load="imtcp")/d' \
        -e '/^input(type="imtcp"/d' \
    /etc/rsyslog.conf 2>/dev/null || true

# ── Drop-in rsyslog configuration for pgaudit → DSF ──────────────────────────
COPY docker/50-pgaudit-dsf.conf /etc/rsyslog.d/50-pgaudit-dsf.conf

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Runtime directories ───────────────────────────────────────────────────────
# /audit/pgaudit is normally a PVC mountPoint; create it so the container
# starts even without the PVC for local testing.
RUN mkdir -p /audit/pgaudit /var/spool/rsyslog

# ── Non-root user ─────────────────────────────────────────────────────────────
# rsyslog needs to read /etc/rsyslog* (root-owned) and write to /var/spool/rsyslog.
# We grant the app user access to spool dir via group membership.
RUN groupadd -r appgroup && \
    useradd  -r -g appgroup -s /sbin/nologin appuser && \
    chown -R appuser:appgroup /audit /var/spool/rsyslog && \
    # rsyslogd itself still runs as root inside the container by design
    # (required to bind privileged syslog port and read /etc/rsyslog.conf).
    # The Python tailer runs as appuser (launched by entrypoint via exec).
    true

ENV AUDIT_DIR=/audit/pgaudit \
    TARGET_NAMESPACE=default \
    POD_LABEL_SELECTOR=cnpg.io/cluster \
    CONTAINER_NAME=postgres \
    RECONNECT_DELAY=5 \
    LOG_LEVEL=INFO

ENTRYPOINT ["/entrypoint.sh"]
