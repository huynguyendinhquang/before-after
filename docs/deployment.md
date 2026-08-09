# LAN deployment and recovery runbook

This runbook is for the production boundary in Slice 8. PostgreSQL stores
metadata; `MEDIA_ROOT` stores managed clinical media and is never served by
nginx.

## Layout and identities

Use these paths (change them together if the clinic layout differs):

- release: `/opt/before-after`, root-owned and not writable by the app;
- media: `/var/lib/before-after/media`, owned by `before-after`, mode `0700`;
- mounted backup filesystem: `/mnt/clinic-backup`;
- backup root: `/mnt/clinic-backup/before-after`, owned by
  `before-after-backup`, mode `0700`;
- app environment: `/etc/before-after/before-after.env`, `root:before-after`
  mode `0640`;
- backup environment: `/etc/before-after/before-after-backup.env`,
  `root:before-after-backup` mode `0640`.

`before-after` and `before-after-backup` are separate system users and groups.
The app user must not own or write the backup root.

## Fresh-host install

Run these commands from the release checkout. The preparation helper creates
both users and groups before any release or media ownership is changed.

```bash
sudo install -d -o root -g root -m 0755 /opt/before-after
sudo cp -a --no-preserve=ownership . /opt/before-after/
sudo /opt/before-after/deploy/bootstrap.sh --prepare-only
# bootstrap also adds nginx's www-data to before-after when present.
sudo chown -R root:root /opt/before-after
sudo python3 -m venv /opt/before-after/.venv
sudo /opt/before-after/.venv/bin/pip install --requirement /opt/before-after/requirements.txt

sudo install -o root -g before-after -m 0640 \
  /opt/before-after/deploy/before-after.env.example \
  /etc/before-after/before-after.env
sudoedit /etc/before-after/before-after.env
sudo /opt/before-after/deploy/bootstrap.sh

sudo install -o root -g root -m 0644 \
  /opt/before-after/deploy/systemd/before-after.service \
  /etc/systemd/system/before-after.service
sudo systemctl daemon-reload
sudo systemctl enable --now before-after.service
```

`bootstrap.sh` changes to the release directory, verifies that the app env is
regular, root-owned, mode `0640`, and group `before-after`, then loads only
that trusted file and runs Alembic and the interactive `create-admin` command
as `before-after`. It does not put the database password in an argument.
Generate `SECRET_KEY` without logging or committing it:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

## Mount and install the backup service

Mount the approved backup disk before creating `BACKUP_ROOT`. Never create the
backup root on the OS disk as a fallback.

```bash
sudo install -d -o root -g root -m 0755 /mnt/clinic-backup
sudo mount /dev/REPLACE_WITH_APPROVED_BACKUP_DEVICE /mnt/clinic-backup
sudo install -d -o before-after-backup -g before-after-backup -m 0700 \
  /mnt/clinic-backup/before-after
sudo install -o root -g before-after-backup -m 0640 \
  /opt/before-after/deploy/before-after-backup.env.example \
  /etc/before-after/before-after-backup.env
sudoedit /etc/before-after/before-after-backup.env

sudo install -o root -g root -m 0644 \
  /opt/before-after/deploy/systemd/before-after-backup.service \
  /etc/systemd/system/before-after-backup.service
sudo install -o root -g root -m 0644 \
  /opt/before-after/deploy/systemd/before-after-backup.timer \
  /etc/systemd/system/before-after-backup.timer
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/before-after.service \
  /etc/systemd/system/before-after-backup.service \
  /etc/systemd/system/before-after-backup.timer
sudo systemctl enable --now before-after-backup.timer
sudo systemctl start before-after-backup.service
```

The backup service runs as `before-after-backup`. Only its `+`
`ExecStartPre`/`ExecStopPost` commands stop and start the app; the Python
backup process never runs as root. `ops/backup.sh` fails closed unless
`systemctl is-active` explicitly reports `inactive` (including systemd's
normal inactive exit status). A stop failure prevents the backup command from
running.

The backup code also requires `BACKUP_ROOT` to resolve to a non-root mount,
rejects symlink/unsafe ACLs, requires mode `0700` and ownership by the current
backup uid, and compares `findmnt` sources through `lsblk` top-level devices.
NFS and removable media are accepted when their sources differ from the media
source. There is no production same-device CLI bypass.

Check the actual mount and ownership before enabling the timer:

```bash
findmnt --target /var/lib/before-after/media
findmnt --target /mnt/clinic-backup/before-after
lsblk --fs
sudo stat -c '%U:%G %a %n' /mnt/clinic-backup/before-after
```

Each generation is private and contains a custom PostgreSQL dump, all four
managed media directories, and a checksum manifest. The dump is fsynced,
checked for the `PGDMP` header, and validated by `pg_restore --list` before the
manifest is published. Retention never removes the last checksum-verified
generation when a new generation has not verified. Stale `.staging-*` entries
are safely removed and are never restore candidates.

## HTTPS reverse proxy

Install the nginx file after replacing the hostname and certificate paths:

```bash
sudo install -o root -g root -m 0644 \
  /opt/before-after/deploy/nginx/before-after.conf \
  /etc/nginx/conf.d/before-after.conf
sudo nginx -t
sudo systemctl reload nginx
curl --fail --silent --show-error --resolve before-after.clinic.lan:443:127.0.0.1 \
  https://before-after.clinic.lan/login >/dev/null
```

The file has a default-server rejector and an exact clinic host, redirects the
whitelisted HTTP host to HTTPS, sets HSTS on HTTPS, overwrites (rather than
trusts) forwarded host/for headers, and clears `X-Forwarded-Prefix`. The app
uses `ProxyFix` only for the configured nginx hop and never trusts a forwarded
prefix.

## Isolated restore-check

Never target production. Production database and media identifiers are
mandatory. Database identity is checked by connecting to both target and
production and comparing the actual server address (or cluster identity for a
Unix socket), port, and database name; DNS, `localhost`, `hostaddr`, and socket
aliases cannot bypass the guard. Media paths use realpath/inode/containment and
reject symlink components.

Create the private restore parent before running the check. Do not use an arbitrary temporary directory:

```bash
sudo install -d -o root -g root -m 0700 /var/lib/before-after/restore-drills
sudo install -d -o root -g root -m 0700 /var/lib/before-after/restore-drills/media
sudo install -o root -g root -m 0600 \
  /opt/before-after/deploy/restore-check.env.example \
  /etc/before-after/restore-check.env
sudoedit /etc/before-after/restore-check.env
sudo install -o root -g root -m 0600 /dev/null \
  /var/lib/before-after/restore-drills/smoke.password
sudoedit /var/lib/before-after/restore-drills/smoke.password
```

Run the tool with URLs from the protected env file, not command arguments.
The smoke password is read from a mode-`0600` file; it is not a CLI option.
Remove the secret and disposable database after the drill.

```bash
trap 'sudo rm -f /var/lib/before-after/restore-drills/smoke.password' EXIT
sudo /bin/bash -c '
  set -a
  . /etc/before-after/restore-check.env
  set +a
  exec /opt/before-after/.venv/bin/python -m ops.restore_check \\
    --isolated \\
    --backup-root /mnt/clinic-backup/before-after \\
    --smoke-password-file /var/lib/before-after/restore-drills/smoke.password
'
```

`restore_check` first copies the verified dump and media generation into a
private staging tree, checks checksums and `PGDMP`/`pg_restore --list` there,
then makes those copies read-only before restoring. If a tar archive is ever
used, members are checked for traversal, links, devices, and non-file types
before extraction. Restore, migration, database/media checksum, or smoke
failure removes restored media and staging and writes a private
`.<target>.restore-failed` disposable marker. It never leaves clinical assets
behind.

The smoke is an actual authenticated login followed by Patient read,
Comparison Set read/preview, and PNG export. The Flask session is an in-memory
isolated test-client session. Use a disposable target database and drop it
afterward; do not create temporary accounts in production.

## Restore drills and failure response

Run a restore drill quarterly and after migrations, storage replacement,
PostgreSQL upgrades, or backup-unit changes. Record only generation, build SHA,
UTC date, operator role, duration, and pass/fail. Destroy the disposable
Database and media according to clinic policy. If a drill fails, keep the
backup generation, investigate, and do not declare the backup healthy.

If the primary host fails, provision a clean host, mount the backup device,
restore into new database/media paths, complete the migration and authenticated
smoke, and only then point nginx at it. Do not reuse production paths as a
restore target.

## Verification commands

The normal unit suite does not need Docker. The all-slice gate uses disposable
PostgreSQL and native `pg_dump`/`pg_restore`:

```bash
docker run --rm --name before-after-postgres-test \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=before_after_test \
  -p 55432:5432 postgres:16
export TEST_DATABASE_URL=postgresql+psycopg://postgres:test@127.0.0.1:55432/before_after_test
PYTHON_BIN=/opt/before-after/.venv/bin/python ./scripts/test-postgres.sh
```

The acceptance tests use an injected storage policy only for their same-device
temporary test directory; production policy is never bypassed. Also run:

```bash
bash -n deploy/bootstrap.sh ops/backup.sh scripts/test-postgres.sh
python3 -m compileall -q app migrations ops tests
sudo systemd-analyze verify /etc/systemd/system/before-after*.service \
  /etc/systemd/system/before-after-backup.timer
```

Real-clinic UAT still requires hardware and services unavailable on a local
checkout: the approved backup disk/mount, clinic CA/TLS trust and LAN DNS,
nginx/systemd on the clinic host, and a real clinic workstation/camera workflow.
No clinical data is used by the automated tests.
