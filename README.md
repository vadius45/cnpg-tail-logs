# pgaudit-forwarder

A Kubernetes pod that **tails CloudNativePG database container logs**, extracts `pgaudit` events, persists them to a PVC-backed folder, and forwards them in real-time to a **Thales DSF Agentless Gateway** via rsyslog over TCP syslog.

```
┌────────────────────────────────────────────────────────────────────┐
│  CloudNativePG namespace (e.g. "default")                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐            │
│  │ pg-cluster-1 │  │ pg-cluster-2 │  │ pg-cluster-3 │            │
│  │  (postgres)  │  │  (postgres)  │  │  (postgres)  │            │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘            │
│         │ pod logs (k8s API)  │                │                   │
└─────────┼────────────────────┼────────────────┼───────────────────┘
          │                    │                │
          ▼                    ▼                ▼
┌─────────────────────────────────────────────────────────────────┐
│  pgaudit-forwarder namespace                                    │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                   pgaudit-forwarder pod                   │  │
│  │                                                           │  │
│  │  ┌──────────────────┐    filter      ┌─────────────────┐ │  │
│  │  │  tailer.py       │ ──logger=pgaudit─▶ /audit/pgaudit │ │  │
│  │  │  (Python/k8s)    │                │  *.log (PVC)    │ │  │
│  │  └──────────────────┘                └────────┬────────┘ │  │
│  │                                               │           │  │
│  │  ┌──────────────────┐   imfile (inotify)      │           │  │
│  │  │  rsyslogd        │ ◀───────────────────────┘           │  │
│  │  │  50-pgaudit-dsf  │                                     │  │
│  │  └────────┬─────────┘                                     │  │
│  └───────────┼───────────────────────────────────────────────┘  │
└──────────────┼──────────────────────────────────────────────────┘
               │  TCP syslog (RFC 5424)
               ▼
     ┌──────────────────────┐
     │  Thales DSF          │
     │  Agentless Gateway   │
     │  :514/tcp            │
     └──────────────────────┘
```

---

## Repository layout

```
pgaudit-forwarder/
├── app/
│   ├── tailer.py            # Python log-tailer (Kubernetes watch + stream)
│   └── requirements.txt
├── docker/
│   ├── entrypoint.sh        # Patches rsyslog config, starts rsyslog + tailer
│   └── 50-pgaudit-dsf.conf  # rsyslog drop-in: imfile → omfwd → DSF
├── k8s/
│   ├── 00-namespace.yaml
│   ├── 01-rbac.yaml         # ServiceAccount + ClusterRole for pod log access
│   ├── 02-secret.yaml       # DSF gateway host/port/protocol
│   ├── 03-pvc.yaml          # 10 Gi PVC for audit logs
│   └── 04-deployment.yaml   # Single-replica Deployment
├── Dockerfile
└── README.md
```

---

## Prerequisites

| Component | Requirement |
|-----------|-------------|
| Kubernetes | 1.24+ |
| CloudNativePG | Any version with pgaudit enabled |
| pgaudit | Configured in `postgresql.conf` via CNPG cluster spec |
| Thales DSF | Agentless Gateway reachable from the cluster |
| Image registry | Push access for the built image |

### CloudNativePG pgaudit configuration

Add the following to your `Cluster` spec so pgaudit events appear in pod logs as JSON:

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster
spec:
  postgresql:
    parameters:
      shared_preload_libraries: "pg_stat_statements,pgaudit"
      pgaudit.log: "all"            # or "ddl,write,role" etc.
      pgaudit.log_catalog: "off"
      pgaudit.log_relation: "on"
      pgaudit.log_statement_once: "off"
      log_destination: "jsonlog"    # CloudNativePG default; required for JSON parsing
```

---

## Build & push the image

```bash
# Set your registry
REGISTRY=your-registry.example.com
IMAGE=${REGISTRY}/pgaudit-forwarder:latest

docker build -t ${IMAGE} .
docker push  ${IMAGE}

# Update the image reference in k8s/04-deployment.yaml
sed -i "s|your-registry/pgaudit-forwarder:latest|${IMAGE}|g" k8s/04-deployment.yaml
```

---

## Deploy

### 1 – Edit the Secret

Open `k8s/02-secret.yaml` and replace the placeholder values:

```yaml
stringData:
  DSF_GATEWAY_HOST: "10.0.1.25"   # your DSF Agentless Gateway IP/hostname
  DSF_GATEWAY_PORT: "514"          # syslog port configured in DSF
  DSF_PROTOCOL: "tcp"              # tcp (recommended) or udp
```

Or use `kubectl` directly (skips the YAML file):

```bash
kubectl create secret generic pgaudit-dsf-gateway \
  --namespace pgaudit-forwarder \
  --from-literal=DSF_GATEWAY_HOST=10.0.1.25 \
  --from-literal=DSF_GATEWAY_PORT=514 \
  --from-literal=DSF_PROTOCOL=tcp
```

### 2 – Adjust the PVC

Edit `k8s/03-pvc.yaml`:
- `storageClassName` → your cluster's storage class (`kubectl get storageclass`)
- `storage` → expected audit log volume (default `10Gi`)
- `accessModes` → `ReadWriteOnce` for single-node, `ReadWriteMany` for HA

### 3 – Set the watched namespace

Edit `k8s/04-deployment.yaml`, env var `TARGET_NAMESPACE`:

```yaml
- name: TARGET_NAMESPACE
  value: "production"   # namespace where CloudNativePG runs
```

### 4 – Apply all manifests

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-rbac.yaml
kubectl apply -f k8s/02-secret.yaml
kubectl apply -f k8s/03-pvc.yaml
kubectl apply -f k8s/04-deployment.yaml
```

### 5 – Verify

```bash
# Check pod is running
kubectl get pods -n pgaudit-forwarder

# Stream forwarder logs
kubectl logs -n pgaudit-forwarder -l app.kubernetes.io/name=pgaudit-forwarder -f

# Check audit files are being written
kubectl exec -n pgaudit-forwarder deploy/pgaudit-forwarder -- \
  ls -lh /audit/pgaudit/

# Tail the latest audit file
kubectl exec -n pgaudit-forwarder deploy/pgaudit-forwarder -- \
  tail -f /audit/pgaudit/$(ls /audit/pgaudit/*.log | head -1)
```

---

## Thales DSF Agentless Gateway – rsyslog configuration

The drop-in config `docker/50-pgaudit-dsf.conf` sends events as **RFC 5424 syslog over TCP** with:

| Parameter | Value |
|-----------|-------|
| Facility  | `local0` |
| Severity  | `info` |
| Program   | `pgaudit` |
| Framing   | octet-counted (RFC 6587) |

This matches the Thales DSF PostgreSQL data source configuration.  
On the DSF side, create a **PostgreSQL data source** pointing to the forwarder pod's egress IP (or CIDR) and configure:

- **Protocol**: Syslog / TCP
- **Port**: 514 (or what you set in `DSF_GATEWAY_PORT`)
- **Log format**: Syslog (the gateway auto-parses the pgaudit JSON payload within the syslog message)

Refer to the [Thales DSF PostgreSQL Onboarding Guide](https://docs-cybersec.thalesgroup.com/bundle/onboarding-databases-to-sonar-reference-guide/page/PostgreSQL-Onboarding-Steps_48368185.html) for DSF-side steps.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TARGET_NAMESPACE` | `default` | Namespace where CloudNativePG pods run |
| `POD_LABEL_SELECTOR` | `cnpg.io/cluster` | Label selector for DB pods |
| `CONTAINER_NAME` | `postgres` | Container name inside each pod |
| `AUDIT_DIR` | `/audit/pgaudit` | PVC mount path for audit files |
| `RECONNECT_DELAY` | `5` | Seconds to wait before retrying a dropped stream |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`) |
| `DSF_GATEWAY_HOST` | *(required)* | Thales DSF Agentless Gateway hostname or IP |
| `DSF_GATEWAY_PORT` | `514` | DSF syslog port |
| `DSF_PROTOCOL` | `tcp` | `tcp` or `udp` |

---

## Resilience

- **Pod restarts**: rsyslog persists its file-position state in `/var/spool/rsyslog/pgaudit-fwd.*`; mount the spool dir on the PVC if you want cross-restart persistence.
- **Gateway unavailable**: rsyslog queues up to 10 000 events in memory (configurable) and saves to disk on shutdown (`queue.saveOnShutdown="on"`).
- **New DB pods**: the Kubernetes watch loop automatically detects new `Running` pods matching the label selector and starts a tailer thread.
- **Pod deletion**: tailer threads self-terminate when the pod returns a 404.

---

## Security considerations

- The `ClusterRole` grants `pods/log` read access cluster-wide.  
  For a single-namespace deployment, replace it with a `Role` + `RoleBinding` scoped to `TARGET_NAMESPACE`.
- DSF gateway credentials are stored in a Kubernetes `Secret`; consider sealing it with [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) or [External Secrets Operator](https://external-secrets.io/).
- The forwarder pod drops all Linux capabilities except `NET_BIND_SERVICE` and `DAC_OVERRIDE` (required by rsyslog).

---

## License

MIT
