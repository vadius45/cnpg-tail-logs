#!/usr/bin/env python3
"""
pgaudit-forwarder — watches CloudNativePG database pods ONLY within the same 
namespace as this pod, tails their container logs, extracts lines where 
logger=pgaudit, and writes them to daily rotating files under /audit/pgaudit/ 
(served by a PVC).

This variant automatically discovers the namespace from the in-cluster 
service account and only monitors cnpg pods in that namespace.

rsyslog (running in the same container) picks those files up and forwards
them to the Thales DSF Agentless Gateway via TCP syslog.
"""

import os
import re
import sys
import time
import signal
import logging
import threading
from datetime import date
from pathlib import Path
from typing import Dict, Set

from kubernetes import client, config, watch

# ─── Auto-discover namespace from service account ─────────────────────────────
def get_namespace_from_serviceaccount() -> str:
    """
    Read the namespace from the in-cluster service account.
    Falls back to 'default' if not running in-cluster.
    """
    sa_namespace_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if sa_namespace_path.exists():
        return sa_namespace_path.read_text().strip()
    return "default"


# ─── Configuration (overridable via env vars) ─────────────────────────────────
NAMESPACE          = get_namespace_from_serviceaccount()
AUDIT_DIR          = Path(os.environ.get("AUDIT_DIR", "/audit/pgaudit"))
POD_LABEL_SELECTOR = os.environ.get(
    "POD_LABEL_SELECTOR",
    "cnpg.io/cluster"          # present on every CloudNativePG pod
)
CONTAINER_NAME     = os.environ.get("CONTAINER_NAME", "postgres")
RECONNECT_DELAY    = int(os.environ.get("RECONNECT_DELAY", "5"))
LOG_LEVEL          = os.environ.get("LOG_LEVEL", "INFO")

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pgaudit-forwarder")

# Matches any CloudNativePG JSON log line that carries logger=pgaudit.
# CloudNativePG ships logs as JSON:  {"logger":"pgaudit", ...}
# We accept both JSON ({"logger":"pgaudit"}) and plain key=value pairs.
PGAUDIT_RE = re.compile(r'"logger"\s*:\s*"pgaudit"|logger=pgaudit', re.IGNORECASE)

# Shutdown flag
_stop = threading.Event()


def _sigterm(*_):
    logger.info("SIGTERM received – shutting down")
    _stop.set()


signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT,  _sigterm)


# ─── Audit file writer ────────────────────────────────────────────────────────

def audit_file_for(pod_name: str) -> Path:
    """Return the current daily audit file path for a given pod."""
    today = date.today().isoformat()
    return AUDIT_DIR / f"{pod_name}_{today}.log"


def write_audit_line(pod_name: str, line: str) -> None:
    """Append a pgaudit line to the pod's daily audit file."""
    path = audit_file_for(pod_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line if line.endswith("\n") else line + "\n")


# ─── Per-pod log tailer ───────────────────────────────────────────────────────

def tail_pod(pod_name: str, v1: client.CoreV1Api) -> None:
    """Stream logs from a single pod/container, filtering for pgaudit lines."""
    pod_logger = logging.getLogger(f"pod.{pod_name}")
    pod_logger.info("Starting log stream")
    w = watch.Watch()
    try:
        for raw in w.stream(
            v1.read_namespaced_pod_log,
            name=pod_name,
            namespace=NAMESPACE,
            container=CONTAINER_NAME,
            follow=True,
            _preload_content=True,
        ):
            if _stop.is_set():
                break
            if not raw:
                continue
            line = raw.strip()
            if PGAUDIT_RE.search(line):
                write_audit_line(pod_name, line)
                pod_logger.debug("pgaudit: %s", line[:200])
    except Exception as exc:
        pod_logger.warning("Stream ended: %s", exc)
    finally:
        w.stop()
        pod_logger.info("Log stream stopped")


def tail_pod_with_retry(pod_name: str, v1: client.CoreV1Api) -> None:
    """Retry tail_pod with back-off until the pod disappears or stop is set."""
    while not _stop.is_set():
        try:
            # Verify the pod still exists before streaming
            v1.read_namespaced_pod(pod_name, NAMESPACE)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                logger.info("Pod %s no longer exists – stopping thread", pod_name)
                return
            logger.warning("API error checking pod %s: %s", pod_name, exc)

        tail_pod(pod_name, v1)

        if not _stop.is_set():
            logger.info("Reconnecting to %s in %ds …", pod_name, RECONNECT_DELAY)
            _stop.wait(RECONNECT_DELAY)


# ─── Pod watcher / thread manager ─────────────────────────────────────────────

def watch_pods(v1: client.CoreV1Api) -> None:
    """Watch for CloudNativePG pod events and manage per-pod tailer threads."""
    active: Dict[str, threading.Thread] = {}
    known_pods: Set[str] = set()

    def _start(pod_name: str) -> None:
        if pod_name in active and active[pod_name].is_alive():
            return
        t = threading.Thread(
            target=tail_pod_with_retry,
            args=(pod_name, v1),
            name=f"tail-{pod_name}",
            daemon=True,
        )
        active[pod_name] = t
        t.start()
        logger.info("Started tailer thread for pod %s", pod_name)

    w = watch.Watch()
    try:
        for event in w.stream(
            v1.list_namespaced_pod,
            namespace=NAMESPACE,
            label_selector=POD_LABEL_SELECTOR,
            timeout_seconds=0,
        ):
            if _stop.is_set():
                break

            evt_type: str  = event["type"]
            pod: client.V1Pod = event["object"]
            pod_name: str  = pod.metadata.name
            phase: str     = (pod.status.phase or "").lower()

            if evt_type in ("ADDED", "MODIFIED") and phase == "running":
                if pod_name not in known_pods:
                    known_pods.add(pod_name)
                    _start(pod_name)

            elif evt_type == "DELETED":
                known_pods.discard(pod_name)
                logger.info("Pod %s deleted", pod_name)

    except Exception as exc:
        if not _stop.is_set():
            logger.error("Pod watch stream error: %s", exc)
    finally:
        w.stop()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(
        "pgaudit-forwarder starting | namespace=%s selector=%s auditDir=%s",
        NAMESPACE, POD_LABEL_SELECTOR, AUDIT_DIR,
    )

    # Load in-cluster config; fall back to kubeconfig for local dev
    try:
        config.load_incluster_config()
        logger.info("Using in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Using kubeconfig (local dev mode)")

    v1 = client.CoreV1Api()

    while not _stop.is_set():
        try:
            watch_pods(v1)
        except Exception as exc:
            logger.error("Unhandled error in watch loop: %s", exc)
        if not _stop.is_set():
            logger.info("Restarting pod watch in %ds …", RECONNECT_DELAY)
            _stop.wait(RECONNECT_DELAY)

    logger.info("pgaudit-forwarder stopped")


if __name__ == "__main__":
    main()
