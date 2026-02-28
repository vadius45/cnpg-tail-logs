# pgaudit-forwarder

A Kubernetes pod that **tails CloudNativePG database container logs** in its own namespace, extracts `pgaudit` events, persists them to a PVC-backed folder, and forwards them in real-time to a **Thales DSF Agentless Gateway** via rsyslog over TCP syslog.

**Key feature**: The forwarder automatically discovers its namespace from the in-cluster service account, so it monitors CNPG pods only in the same namespace where it's deployed. No need to configure `TARGET_NAMESPACE`.

---

## Deployment

1. **Namespace**  
   Create or use any namespace you want. All manifests are now namespace-agnostic. Example:
   ```sh
   kubectl create namespace my-audit-ns
   ```

2. **Install**  
   Apply each manifest with your chosen namespace:
   ```sh
   kubectl apply -n my-audit-ns -f 00-namespace.yaml   # optional, if you want to create the namespace
   kubectl apply -n my-audit-ns -f 01-rbac.yaml
   kubectl apply -n my-audit-ns -f 02-secret.yaml
   kubectl apply -n my-audit-ns -f 03-pvc.yaml
   kubectl apply -n my-audit-ns -f 04-deployment.yaml
   ```
   Or edit the `namespace:` field in each manifest before applying.

3. **Configuration**  
   - The forwarder auto-discovers its namespace.
   - No `TARGET_NAMESPACE` env var is needed.
   - It only tails CNPG pods in its own namespace.

---

## Security

- All RBAC is **namespace-scoped**.
- No cluster-admin or cross-namespace access is required.

---

## Vulnerabilities

- All Python dependencies are up-to-date and patched for known HIGH CVEs.
- Remaining vulnerabilities are only in system packages with no available fixes.

---

## Cleanup

- Only the same-namespace version is present.
- No duplicate or legacy files.

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
│   ├── 01-rbac.yaml         # ServiceAccount + Role for pod log access (namespace-scoped)
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

### 3 – Deploy

The pod automatically discovers its namespace from the in-cluster service account, so it will monitor CNPG pods only in that namespace. No configuration needed!

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
kube4 – Apply all manifestsn pgaudit-forwarder -l app.kubernetes.io/name=pgaudit-forwarder -f

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
| `POD_LABEL_SELECTOR` | `cnpg.io/cluster` | Label selector for DB pods |
| `CONTAINER_NAME` | `postgres` | Container name inside each pod |
| `AUDIT_DIR` | `/audit/pgaudit` | PVC mount path for audit files |
| `RECONNECT_DELAY` | `5` | Seconds to wait before retrying a dropped stream |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`) |
| `DSF_GATEWAY_HOST` | *(required)* | Thales DSF Agentless Gateway hostname or IP |
| `DSF_GATEWAY_PORT` | `514` | DSF syslog port |
| `DSF_PROTOCOL` | `tcp` | `tcp` or `udp` |

**Note**: `TARGET_NAMESPACE` is no longer needed. The pod automatically discovers its namespace from the in-cluster service account and monitors CNPG pods only in that namespace.

---

## Resilience

- **Pod restarts**: rsyslog persists its file-position state in `/var/spool/rsyslog/pgaudit-fwd.*`; mount the spool dir on the PVC if you want cross-restart persistence.
- **Gateway unavailable**: rsyslog queues up to 10 000 events in memory (configurable) and saves to disk on shutdown (`queue.saveOnShutdown="on"`).
- **New DB pods**: the Kubernetes watch loop automatically detects new `Running` pods matching the label selector and starts a tailer thread.
- **Pod deletion**: tailer threads self-terminate when the pod returns a 404.

---

## Security considerations

- The single-namespace deployments, replace it with a `Role` + `RoleBinding` scoped to the forwarder's namespace (since it only watches pods in its own namespace)
  For a single-namespace deployment, replace it with a `Role` + `RoleBinding` scoped to `TARGET_NAMESPACE`.
- DSF gateway credentials are stored in a Kubernetes `Secret`; consider sealing it with [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) or [External Secrets Operator](https://external-secrets.io/).
- The forwarder pod drops all Linux capabilities except `NET_BIND_SERVICE` and `DAC_OVERRIDE` (required by rsyslog).

---

## License

MIT
