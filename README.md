# os-backup-scheduler

OpenStack backup engine — the **container image** half of the OpenStack backup ecosystem.

This repo packages the Python scripts (`openstack-backup.py`, `openstack-verify.py`) and the Zabbix template that the [`e-door-ch/os-backup-central`](https://github.com/e-door-ch/os-backup-central) reusable workflows execute. The image is published to GHCR (with Docker Hub and Quay mirrors when their creds are configured) and tagged on every release.

> **Where to read what**
> - **How a daily backup actually runs** → see `os-backup-central/README.md` (workflow orchestration) and its [`docs/adr/`](https://github.com/e-door-ch/os-backup-central/tree/main/docs/adr) (architectural decisions).
> - **Per-tenant configuration & cron** → see each `e-door-ch/PCP-<id>-Backup` README.
> - **Day-to-day operations / incident playbooks / Zabbix items** → the Outline tree `Infrastructure / OpenStack Backup`.

## What the image does

- 🔍 Iterates OpenStack resources tagged `autoBackup=true` (Glance `Property` or Cinder `Metadata`).
- 💾 Creates a server backup for boot-from-image instances and a Cinder backup for volumes.
- 🧠 Detects boot-from-volume servers automatically and skips them (back up the volume directly).
- ⚡ Parallelises volume backups with a thread pool (`BACKUP_CONCURRENCY`).
- 🌍 One run per OpenStack region (matrix-parallel from the workflow side).
- 📊 Emits structured trapper items to a Zabbix server via the `zabbix_utils` Python lib.
- ✅ A companion verify run (`openstack-verify.py`) reconciles state ~7 h later: detects stuck/failed backups and cleans up `temp_*` snapshot/volume intermediates.

The Zabbix template that ships in `zabbix_template.yaml` is the source of truth for the items, triggers and value maps. Re-import it on the Zabbix server whenever it changes.

## Image

| | Value |
|---|---|
| Source | this repo |
| Builder | `.github/workflows/docker-build.yml` (push on `v*` tags, cosign keyless signing + SBOM attestation) |
| GHCR | `ghcr.io/stackopshq/os-backup-scheduler:vX.Y.Z` (and `:latest`, `@sha256:…`) |
| Docker Hub (mirror) | `docker.io/stackopshq/os-backup-scheduler` (best-effort, `continue-on-error`) |
| Quay (mirror) | `quay.io/stackopshq/os-backup-scheduler` (best-effort, `continue-on-error`) |
| Latest release | **v2.4.0** (2026-05-16) |

`os-backup-central` pins the image **by sha256 digest** (`@sha256:…`) since its `v3.2.0`. Bumping the image is a deliberate two-step:

1. Tag a new `vX.Y.Z` here → CI publishes & signs.
2. PR on `os-backup-central` updates the digest in every reusable, tag a new central `vN.N.N`.

Tenants then pick up the new image when they bump their caller pin to the new central tag.

## Recent releases

| Tag | Headline change |
|-----|-----------------|
| **v2.4.0** | `ZABBIX_SERVER` accepts a comma-separated failover cluster (e.g. `proxy-a,proxy-b`). Single-host inputs unchanged. |
| v2.3.0 | Early `run_started_at` heartbeat (sent before OpenStack auth) + defensive `_wait_backup` loop. |
| v2.2.0 | `verify.temp_count` and `verify.temp_gb` items (orphan storage tracking). `units: unixtime` on heartbeat items. |
| v2.1.0 | `list-temp` workflow on the central side; image unchanged. |
| **v2.0.0** | Repo + image registry moved from `net-architect-cloud` to `stackopshq`. Old image at `ghcr.io/net-architect-cloud/os-backup-scheduler` remains pullable (frozen). |

## Environment variables

The scripts read everything from the environment. The reusable workflows in `os-backup-central` are responsible for wiring secrets and variables into the container; this list is the contract.

### OpenStack auth (required)

| Var | Notes |
|-----|-------|
| `OS_AUTH_URL` | Keystone endpoint, e.g. `https://api.pub1.infomaniak.cloud/identity` |
| `OS_USERNAME` | OpenStack username (one *Backup User* per tenant in the E-Door fleet) |
| `OS_PASSWORD` | OpenStack password (shared org-level secret in the E-Door fleet, [ADR 0001 retention rationale also applies here for sharing-vs-per-tenant](https://github.com/e-door-ch/os-backup-central/tree/main/docs/adr)) |
| `OS_PROJECT_NAME` | OpenStack project name (`PCP-<id>` in the fleet) |
| `OS_USER_DOMAIN_NAME` | `Default` |
| `OS_PROJECT_DOMAIN_NAME` | `default` |
| `OS_IDENTITY_API_VERSION` | `3` |
| `OS_REGION_NAME` | Set per matrix entry (e.g. `dc3-a`, `dc4-a`) |

### Behaviour tuning

| Var | Default | Notes |
|-----|---------|-------|
| `RETENTION_DAYS` | `14` | Number of *days* of volume backups to keep; for instances, Nova uses this as `rotation` (= number of backups). |
| `USE_SNAPSHOT_METHOD` | `true` | Attached-volume backups go via Cinder snapshot → temp volume → backup → cleanup. Avoids `--force` quirks on Infomaniak. |
| `WAIT_FOR_BACKUP` | `false` | Async (default): fire and exit; the verify run reconciles later. Set `true` to wait synchronously. |
| `BACKUP_CONCURRENCY` | `5` | Worker count for volume backups (thread pool). |
| `RESOURCE_TIMEOUT` | `3600` | Max seconds to wait for snapshot/temp-volume state transitions. |
| `BACKUP_TIMEOUT` | `86400` | Max seconds to wait for a Cinder backup to finish (when `WAIT_FOR_BACKUP=true`). |

### Zabbix trapper (optional)

| Var | Notes |
|-----|-------|
| `ZABBIX_SERVER` | Single host (e.g. `e-door.zabbix.cloud`) **or** comma-separated failover cluster (e.g. `proxy-a:10051,proxy-b:10051`) since v2.4.0. Empty value → trapper output is silently skipped. |
| `ZABBIX_HOST` | Host name prefix; the script appends `-<region>` (e.g. `backup-PCP-CZERPPH-dc3-a`). |

## Alerting

Alerting is the responsibility of Zabbix triggers on the `tpl-os-backup` template (defined in `zabbix_template.yaml`). The container itself never sends Slack/Discord/Teams/Telegram messages. The in-workflow `notify.yml` reusable that fanned out to those channels was removed in `os-backup-central` v3.0.0 ([ADR 0003](https://github.com/e-door-ch/os-backup-central/blob/main/docs/adr/0003-zabbix-as-single-alert-source.md)).

## Backup conventions

| Resource type | Backup method |
|---------------|---------------|
| Boot-from-image instance | `openstack server backup create` (Nova-driven image snapshot) |
| Boot-from-volume instance | Skipped — back up the boot **volume** instead |
| Detached volume | Direct `openstack volume backup create` |
| Attached volume | Snapshot → temp volume → backup → cleanup (default, `USE_SNAPSHOT_METHOD=true`) |
| Attached volume (legacy) | `--force` backup if `USE_SNAPSHOT_METHOD=false` |

Naming: `autoBackup_<YYYY-MM-DD_HHMMSS>_<resource-name>`. Volumes without a name fall back to `<attached-instance>_vol`.

## Local development

```bash
podman build -t os-backup-scheduler:dev .
podman run --rm \
  -e OS_AUTH_URL=… -e OS_USERNAME=… -e OS_PASSWORD=… -e OS_PROJECT_NAME=… \
  -e OS_USER_DOMAIN_NAME=Default -e OS_PROJECT_DOMAIN_NAME=default \
  -e OS_IDENTITY_API_VERSION=3 -e OS_REGION_NAME=dc3-a \
  -e RETENTION_DAYS=1 \
  os-backup-scheduler:dev
```

Tests live under `tests/` and run with `pytest`. The CI workflow runs `pytest`, `ruff check`, `ruff format --check`, `pip-audit` and `actionlint` on every PR.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Credits

Originally based on [houtknots/Openstack-Automatic-Snapshot](https://github.com/houtknots/Openstack-Automatic-Snapshot).
