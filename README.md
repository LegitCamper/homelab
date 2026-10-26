# Overview
## Servers
- Gateway: A Pangolin server running on a vps like Digital Ocean
- Homeserver: A server behind a nat
## Playbooks
- `homeserver.yml` configures homeserver with the compose files
- `dns.yml` configures vps with blocky dns and letsencrypt

# Homeserver ucore distro setup
## Install Ucore on homeserver
- create a simple [butane configuration](https://github.com/ublue-os/ucore/blob/main/examples/ucore-autorebase.butane)
- transpile it to an [ignition file](https://coreos.github.io/butane/getting-started/#container-image)
- validate the ignition file `podman run --pull=always --rm -i quay.io/coreos/ignition-validate:release - < ucore.ign`
> This example uses podman, but docker can be used too
- start an http server to host the ingnition file and prepare for install `sudo podman run --rm --privileged -p 8080:5000 --security-opt label=disable -v $PWD/ucore.ign:/html/ucore.ign  ghcr.io/patrickdappollonio/docker-http-server:v2`
- boot up the fedora core installer and install with your ignition file: `sudo coreos-installer install /dev/sda --ignition-url https://192.168.0.100:8080/ucore.ign` and then reboot

## Final Setup
- ensure tailscaled is running: `sudo systemctl start tailescaled` (ansible will ensure its enabled) and then sign in `tailscale up` 
- `ansible-playbook -i server.tailscale.hostname, homeserver.yml` (you will need `--ask-become-pass` if there is a sudo password)
- The server should be ready to go 🎉

## Quick updates
Once a host has already been fully set up, routine changes (e.g. editing `homeserver/docker/compose.yml` or another file under `homeserver/docker/`) don't need the full first-time-setup run. Use `--tags deploy` to just copy the compose files/secrets and run `docker compose up`:
- `ansible-playbook -i server.tailscale.hostname, homeserver.yml --tags deploy`

## Native restic backups (configuration only until explicitly deployed)

`homeserver-backup.yml` is a separate playbook, not part of ordinary deployment.
It layers `restic` and `rsync` with rpm-ostree, never applies them live, and never
reboots automatically. If the booted deployment lacks them, it stops with an
instruction to reboot in your own maintenance window and rerun. It does not run
the existing homeserver playbook's deployment or reboot handlers.

### Consistency and availability

`homeserver/backup.yml` lists explicit service groups and sources. Each group gets
a live rsync pre-copy, then only its currently running containers stop. A final
`rsync --checksum` copies a consistent state with ownership, permissions, ACLs,
xattrs and hardlinks preserved. Containers restart in reverse stop order before
the next group. Only after every group succeeds does restic upload staged data.
Network upload time is never intentional container downtime. Related services
share a group: Immich/server + PostgreSQL, Karakeep + Meilisearch, and download
clients + their VPN. No whole-stack `compose down` or `docker pause` is used.

This follows PostgreSQL's documented two-pass rsync backup approach. For databases,
native dumps or storage-level snapshots are also conventional; raw copies of
actively changing database files are not sufficient. Existing storage is configured
as ext4 with no managed snapshot layer, so this does not add one.

Important limits:
- Checksumming still reads every file while that group is stopped. Large Immich
  libraries or Syncthing trees may have significant downtime even with pre-copy.
  Measure first; use coordinated filesystem snapshots or application-native
  backups if that pause is unacceptable. Stopping around the B2 upload is the
  simpler alternative, but would keep that app offline for the upload duration.
- Groups represent different capture times, not one atomic whole-host snapshot.
- PostgreSQL uses a complete cold physical copy, not a portable SQL dump. Restore
  with the same PostgreSQL major version and compatible extensions/image. No
  external tablespace or external WAL paths are covered by the current scope.
- Recovery records container IDs durably before stopping; `finally`, systemd
  `ExecStopPost`, and an enabled boot recovery unit retry restarts. Originally
  stopped containers stay stopped. Failed restarts retain the recovery record.
  Recovery checks running state, not application readiness or long-term health.
- Do not deploy, recreate containers, or run other backups against these sources
  concurrently. The lock serializes this runner only. All writers to a source
  must be in its group; update scope and its coverage test when adding services.
- Staging needs a full extra copy plus free space for rsync replacement files.
  Default location is `/var/lib/homelab-backup/staging`, with a 10 GiB pre-copy
  free-space guard, not a capacity reservation. Choose a sufficiently large local
  disk and add its mount to `required_mounts` if moved. Missing mounts, missing
  sources, forced kills, copy errors, and restic failures fail the job.
- Staging contains plaintext secrets and data under a root-only directory. It is
  a mutable working copy, not an independent backup or historical restore point.

### Scope, schedule, and cost

Includes declared app state under `/mnt/homelab_iscsi`, Immich uploads and database,
Syncthing config/data, `homeserver_9router-data` resolved through Docker volume
inspection, deployed Compose files/secrets, and backup configuration/credentials.
Excludes **all `/mnt/media` content** (including audiobooks), Prometheus history,
LanguageTool downloaded models, Docker images/overlay layers, and the named log,
cache and temporary volumes. Loki has no persistent data mount in this Compose
file. Unlisted/orphaned directories and other projects are not automatically
included. This is an application-data backup, not a bare-metal OS image.

Timers use the host timezone, a random delay up to 30 minutes and missed-run
catch-up. They remain **disabled by default**:
- Daily 03:30: stage and back up. Successful snapshot ID is recorded locally.
- Sunday 10:00: repository check plus a rotating 1/12 data read; twelve consecutive
  scheduled runs cycle all subsets, though changing repository contents can shift
  subset membership. This does not replace restore testing or a full check.
- First of month 12:00: check, retain 14 daily / 8 weekly / 12 monthly / 3 yearly,
  prune, then check again. Retention requires a verified successful staged snapshot
  less than 48 hours old matching the current config/repository. No daily prune.

Prune limits repacking to 1 GiB per run and permits 20% unused space to reduce
rewrites. This trades higher stored bytes for less repacking; the repack cap can
leave more than 20% unused. Review billed storage and loosen the cap if needed.
Retention filters by `homeserver` and `homelab-staged-v1`; old `docker-volumes`
snapshots are intentionally untouched. Prune operates on the whole repository
but preserves data referenced by any remaining snapshot. Use a dedicated repo.

Backblaze B2 has no minimum-storage-duration/early-deletion fee. Retained data,
hidden object versions, and applicable egress matter more than churn itself.
This config preserves the existing native `b2:` backend and its credential names.
Use a private bucket and a bucket-scoped application key with list/read/write/delete
permissions, not the account master key. Store the repository password and recovery
credentials separately in a password manager; do not rely on their backed-up copy.
Do not change the password when reusing the existing repository.

Never add a lifecycle rule that expires live restic objects by age: old packs can
still hold current backup data. Native B2 deletions remove versions directly;
if switching to S3 later, configure hidden/noncurrent-version cleanup deliberately
because S3 deletion hides objects. Bucket Object Lock is not enabled here; normal
prune needs delete access, so host compromise can delete backups. Independent
immutable/offline copies need a separate design, not an untested retention toggle.

### Future rollout (operator actions, not run by editing this repository)

1. Copy `homeserver/backup-secrets.yml.example` to
   `homeserver/backup-secrets.yml`, restrict it to mode 0600, fill in the existing
   repository/password and a scoped B2 key, then encrypt it with `ansible-vault
   encrypt homeserver/backup-secrets.yml`. The real file is ignored by Git.
2. Review scope, free space, host timezone and retention in `homeserver/backup.yml`.
   Keep `backup_enable_timers: false`. On your chosen rollout date run
   `ansible-playbook homeserver-backup.yml`. If packages need a reboot, schedule it
   yourself and rerun afterward. Neither install nor configuration starts a backup.
3. Wait for any legacy backup/check/prune to finish. Stop and remove only
   `restic-backup`, `restic-prune`, and `restic-check`; do not kill an active prune.
   Their Compose definitions are now behind the opt-in `legacy-backup` profile,
   so normal deployment will not create them again. Profiles do not stop already
   running containers. The native runner refuses to proceed while any of those
   legacy containers still exist, even if stopped.
4. On the host, run `sudo homelab-backup restic snapshots` to verify existing
   repository access. Only for an intentionally new repository run
   `sudo homelab-backup restic init --repository-version 2`. No automatic init,
   migration, unlock, or fallback repository is performed.
5. Run `sudo systemctl start homelab-backup.service`, inspect
   `sudo journalctl -u homelab-backup.service`, then run
   `sudo systemctl start homelab-check.service`. Check container health and measure
   each group's downtime before enabling scheduling.
6. Perform an isolated restore drill as described below. Then set
   `backup_enable_timers: true` and rerun the backup playbook. Activation checks
   the successful snapshot marker, remote snapshot, sources and legacy retirement.
   With persistent timers, enabling them may trigger a missed scheduled run.

Do not use `--profile legacy-backup` after migration. Retire old snapshot history
only after the new backup has passed a restore drill; no old history is deleted
automatically by this migration.

### Restore and monitoring

Run `sudo homelab-backup restic snapshots --tag homelab-staged-v1`, select an exact
successful snapshot ID, and inspect it with `sudo homelab-backup restic ls SNAPSHOT_ID`.
Restore into a new, private scratch directory, never over running application data:

```sh
sudo install -d -m 0700 /var/tmp/homelab-restore-test
sudo homelab-backup restic restore SNAPSHOT_ID --target /var/tmp/homelab-restore-test --verify
```

The tree contains the staging path followed by group name and original source
path; named volumes use `GROUP/volumes/VOLUME_NAME`. Use `restic ls` rather than
assuming a layout. For disaster recovery, reinstall restic and recover credentials
from your password manager first; direct restic works without this runner.

Test a SQLite-backed app and Immich/PostgreSQL in isolated containers with matching
images, restored ownership, and no production mounts/networks. Confirm login,
database reads and representative media retrieval. For actual recovery, stop only
the affected group, preserve its current data for rollback, restore its data and
permissions, then start dependencies before applications. Treat Immich DB and
uploads as one group. Repeat drills quarterly and after major app/DB upgrades.

Review `systemctl list-timers 'homelab-*'`, `systemctl --failed`, job journals and
`/var/lib/homelab-backup/last-success.json`. Wire an external monitor to failed jobs
and a last-success age over 48 hours before relying on this unattended. No alert
destination was supplied, so no email/webhook or false promise of notifications
is configured. A hung runner times out after 12 hours; boot/stop recovery retries
container starts. Check actual service health after any interrupted run.

Local-only validation (no Docker daemon, host connection, or B2 access):

```sh
python3 -m unittest discover -s homeserver/tests -v
ANSIBLE_ASK_VAULT_PASS=false ansible-playbook -i localhost, --connection local homeserver-backup.yml --syntax-check
```

Tests require local Python, PyYAML and rsync; the installed runner uses only Python
standard-library modules plus host Docker, rsync and restic. The backup playbook
also needs the `community.general` Ansible collection. Use a long random repository
password for a new repository; existing repository passwords must remain unchanged.

References: PostgreSQL documentation, "File System Level Backup" (two-pass rsync);
rpm-ostree administrator handbook (offline package layering); restic documentation,
"Preparing a new repository" and "Removing backup snapshots"; Backblaze B2 pricing.
