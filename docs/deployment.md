# LAN deployment and recovery runbook

This runbook is for the production boundary in Slice 8. PostgreSQL stores
metadata; `MEDIA_ROOT` stores managed clinical media and is never served by
nginx.

## Layout and identities

Use these paths (change them together if the clinic layout differs):

- release: `/opt/before-after`, root-owned and not writable by the app;
- media: `/var/lib/before-after/media`, owned by `before-after:before-after-media`, mode `2750`;
- mounted backup filesystem: `/mnt/clinic-backup`;
- backup root: `/mnt/clinic-backup/before-after`, owned by
  `before-after-backup`, mode `0700`;
- app environment: `/etc/before-after/before-after.env`, `root:before-after`
  mode `0640`;
- `/etc/before-after` itself is `root:before-after` mode `0750`; `www-data`
  belongs only to `before-after-web` and cannot traverse this directory;
- persistent coordination lock: `/var/lib/before-after/media/.backup.lock`,
  `before-after:before-after-media` mode `0660`;
- backup environment: `/etc/before-after/before-after-backup.env`,
  `root:before-after-backup` mode `0640`.

`before-after` and `before-after-backup` are separate system users and groups.
The dedicated `before-after-media` group contains only those two approved
identities (whether primary or supplementary); bootstrap and verification
reject extra members and POSIX ACLs. The app owns the media tree and can write
it; the backup user has group `r-x` on
directories and group `r--` on files, so it can read every managed file but
cannot create, modify, or delete media. The app user must not own or write the
backup root.

## Fresh-host install

Run these commands from the release checkout. The preparation helper creates both users and groups before any release or
media ownership is changed. It validates the canonical root-owned,
non-writable parent chain and creates MEDIA_ROOT, its managed children, and
`.backup.lock` through `mkdirat`/`openat` with `O_NOFOLLOW`; `/`, `/etc`,
symlink overrides, and untrusted parents are refused.

```bash
sudo install -d -o root -g root -m 0755 /opt/before-after
sudo cp -a --no-preserve=ownership . /opt/before-after/
sudo /opt/before-after/deploy/bootstrap.sh --prepare-only
# bootstrap adds app and nginx's www-data to the separate before-after-web socket group.
sudo chown -R root:root /opt/before-after
sudo python3 -m venv /opt/before-after/.venv
sudo apt-get update
sudo apt-get install --yes postgresql-client-16
sudo /opt/before-after/.venv/bin/pip install --requirement /opt/before-after/requirements.txt

sudo install -o root -g before-after -m 0640 \
  /opt/before-after/deploy/before-after.env.example \
  /etc/before-after/before-after.env
sudo install -o before-after -g before-after -m 0600 /dev/null \
  /etc/before-after/before-after.pgpass
sudoedit /etc/before-after/before-after.env
# Add the protected libpq entry to before-after.pgpass:
# 127.0.0.1:5432:before_after:before_after:<database-password>
sudoedit /etc/before-after/before-after.pgpass
sudo /opt/before-after/deploy/bootstrap.sh

sudo install -o root -g root -m 0644 \
  /opt/before-after/deploy/systemd/before-after.service \
  /etc/systemd/system/before-after.service
sudo systemctl daemon-reload
sudo systemctl enable --now before-after.service
```

`bootstrap.sh` changes to the release directory, verifies that the app env is
regular, root-owned, mode `0640`, and group `before-after`, then passes only a
credential-free URL and protected PGPASSFILE to Alembic and the interactive
`create-admin` command as `before-after`. It never puts the database password
in a child environment, argument, or `PGPASSWORD`; do not `source "$1"` in a
child process.
Generate `SECRET_KEY` without logging or committing it:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

After upgrading an existing host, normalize the pre-group media tree once as
root. The helper explicitly sets the root and four managed directories to
`2750`, nested directories to `0750`, clinical files to `0640`, and
pending/delete/restore markers, upload temps, plus `.reconcile.lock` to `0600`;
do not replace it with `chmod -R`:

```bash
sudo /opt/before-after/deploy/normalize-media-permissions.sh
sudo MEDIA_ROOT=/var/lib/before-after/media /opt/before-after/deploy/verify-permissions.sh
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
sudo install -o before-after-backup -g before-after-backup -m 0600 /dev/null \
  /etc/before-after/before-after-backup.pgpass
sudoedit /etc/before-after/before-after-backup.env
# Add the protected libpq entry to before-after-backup.pgpass:
# 127.0.0.1:5432:before_after:before_after_backup:<backup-password>
sudoedit /etc/before-after/before-after-backup.pgpass
# Create the dedicated read-only PostgreSQL role as an administrator. The
# helper reads the password from a mode-0600 file, not from argv.
export BACKUP_DB_PASSWORD_FILE=/var/lib/postgresql/before-after-backup.password
sudo install -o postgres -g postgres -m 0600 /dev/null "$BACKUP_DB_PASSWORD_FILE"
sudoedit "$BACKUP_DB_PASSWORD_FILE"
sudo -u postgres env BACKUP_DB_PASSWORD_FILE="$BACKUP_DB_PASSWORD_FILE" \
  /opt/before-after/deploy/bootstrap-postgres-backup-role.sh
sudo rm -f "$BACKUP_DB_PASSWORD_FILE"

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

The backup service runs as `before-after-backup` and connects with the
`before_after_backup` PostgreSQL LOGIN role through its protected PGPASSFILE.
That role is scoped to the production database and receives only CONNECT,
public-schema USAGE, and SELECT on current tables/sequences plus matching
future defaults; it has no write, ownership, role-management, or
database-creation privileges. Verify
with `pg_dump` and explicitly test that DELETE and DDL fail before enabling
production backups. Only its `ExecStartPre`/`ExecStopPost` commands stop and
start the app; the Python backup process never runs as root. Gunicorn holds a shared flock on the
persistent media lock for its whole lifetime. `ops/backup.sh` takes the same
lock exclusively before checking `systemctl is-active`, and keeps it through
`pg_dump` and media publication. An app restart during backup therefore waits;
there is no `Conflicts=` relationship that can kill an active backup. The
script fails closed unless `systemctl is-active` explicitly reports `inactive`
(including systemd's normal inactive exit status). A stop failure prevents the
backup command from running.

The backup code also requires `BACKUP_ROOT` to resolve to a non-root mount,
rejects symlink/unsafe ACLs, requires mode `0700` and ownership by the current
backup uid, and compares `findmnt` sources through `lsblk` top-level devices.
NFS and removable media are accepted when their sources differ from the media
source. Native `pg_dump`, `pg_restore`, and `psql` are preflighted against the
actual source server; all majors must match. The source major and all client
majors are recorded in the manifest, and restore rejects a target with a
different major. Install `postgresql-client-16` for a PostgreSQL 16 clinic.
There is no production same-device CLI bypass. A stopped-app media tree with a
`.pending-*`, `.capture-delete-*`, `.restore-*`, or upload-temp marker is not
copied as if it were clinical media: the backup fails before staging with
`recovery required`. Stop the service, run the app's `reconcile-media` CLI as
`before-after`, then retry; this keeps the database/media generation paired
rather than omitting active recovery state. For example, with the protected
app environment loaded by root:

```bash
sudo systemctl stop before-after.service
sudo runuser --user before-after -- env -i \
  PATH=/opt/before-after/.venv/bin:/usr/local/bin:/usr/bin:/bin \
  HOME=/opt/before-after \
  /bin/bash -c 'set -a; source "$1"; set +a; cd "$2"; exec "$3" --app app:create_app reconcile-media' \
  bash /etc/before-after/before-after.env /opt/before-after /opt/before-after/.venv/bin/flask
sudo systemctl start before-after.service
```

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
manifest is published. Retention uses the same restorable-v2 verifier as
restore and keeps only the requested number of verified v2 generations.
Unsupported v1 or corrupt generations never consume retention slots and are
preserved. Selection only ignores `.staging-*`; it never deletes staging.
Explicit stale-stage cleanup runs under the backup lock and removes only old
stages with a valid owner marker. A failed cleanup is reported as poisoned
instead of being silently left behind. Unknown content directly under
`MEDIA_ROOT` fails backup before publication rather than being omitted.

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
production and comparing the PostgreSQL cluster system identifier plus
database OID; TCP address/port are supplemental diagnostics, and may be NULL
for Unix sockets. DNS, `localhost`, `hostaddr`, and socket aliases cannot
bypass the guard. The guard fails closed when `pg_control_system()` or the
database OID is unavailable. Media paths use canonical-path/inode/containment
checks and reject symlink components; restore ownership markers hash the
canonical target path.

Choose a new generated target database name on the same PostgreSQL cluster
(for example, `before_after_restore_<32 lowercase hex characters>`) and create
the private restore parent. Never
run `CREATE DATABASE` or restore into an existing target yourself. The
`--provision-target` flow uses a mandatory admin connection, refuses an existing
name, writes a per-run registry marker before `CREATE DATABASE`, records the
exact ownership marker in `pg_database`, then creates the empty `0700` media
target. A crash before the comment is recoverable only when that registry
proves the generated name, server, target, and empty database; otherwise the
command fails closed for manual cleanup. If media provisioning fails, the
newly created DB is rolled back:

```bash
sudo install -d -o root -g root -m 0700 /var/lib/before-after/restore-drills
sudo install -o root -g root -m 0600 \
  /opt/before-after/deploy/restore-check.env.example \
  /etc/before-after/restore-check.env
sudoedit /etc/before-after/restore-check.env
sudo install -o root -g root -m 0600 /dev/null \
  /var/lib/before-after/restore-drills/smoke.password
sudoedit /var/lib/before-after/restore-drills/smoke.password
sudo install -o root -g root -m 0600 /dev/null \
  /etc/before-after/restore-check.pgpass
sudoedit /etc/before-after/restore-check.pgpass
```

Set `RESTORE_CHECK_DATABASE_URL` to the new database name,
`RESTORE_CHECK_MEDIA_ROOT` to
`/var/lib/before-after/restore-drills/media`, and
`RESTORE_SMOKE_USERNAME` to a temporary username. The protected password file
is used for that account. The restored backup must already contain an active
login and an active Comparison Set. `--create-smoke-account` may create the
named temporary Editor only in the owned isolated DB when it is absent; the
account is removed explicitly after the smoke, and the owned DB is always
cleaned on failure or by the cleanup trap. It never creates an account in
production.

Run the tool with URLs from the protected env file, not command arguments. The
smoke password is read from a mode-`0600` file; it is not a CLI option:

```bash
set -euo pipefail
cleanup() {
  status=$?
  set +e
  sudo /bin/bash -c '
    set -a
    . /etc/before-after/restore-check.env
    set +a
    exec /opt/before-after/.venv/bin/python -m ops.restore_check \
      --isolated --cleanup-target
  '
  cleanup_status=$?
  sudo rm -f /var/lib/before-after/restore-drills/smoke.password
  secret_status=$?
  if (( cleanup_status != 0 || secret_status != 0 )); then
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT
sudo /bin/bash -c '
  set -a
  . /etc/before-after/restore-check.env
  set +a
  exec /opt/before-after/.venv/bin/python -m ops.restore_check \
    --backup-root /mnt/clinic-backup/before-after \
    --smoke-password-file /var/lib/before-after/restore-drills/smoke.password \
    --provision-target --create-smoke-account --isolated
'
```

`restore_check` validates the private restore parent, target containment,
backup/production path separation, and every symlink constraint before creating
the target database. It then copies the verified dump and media generation into
a private staging tree, checks checksums and `PGDMP`/`pg_restore --list` there,
then makes those copies read-only before restoring. If a tar archive is ever
used, members are checked for traversal, links, devices, and non-file types
before extraction. Restore, migration, database/media checksum, or smoke
failure removes restored media and staging and writes a private
`.<target>.restore-failed` disposable marker. It never leaves clinical assets
behind. The cleanup command drops only the database carrying this run's
ownership marker; it never cleans an existing database. If any cleanup step
fails, the command fails loudly and marks the target `POISONED` so it must be
destroyed before another drill.

The smoke is an actual authenticated login followed by Patient read,
Comparison Set read/preview, and PNG export. It requires at least one restored
active Comparison Set and exports that set. The Flask session is an in-memory
isolated test-client session. The `EXIT` cleanup drops the disposable target
database and removes its media and secret file; do not create temporary
accounts in production.

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

The normal unit suite does not need Docker. The all-slice gate uses a
PostgreSQL 16 server, matching native `postgresql-client-16`
`pg_dump`/`pg_restore`/`psql`, and the mandatory disposable DAC proof:

```bash
docker run --detach --rm --name before-after-postgres-test \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=before_after_test \
  -p 55433:5432 postgres:16
trap 'docker rm --force before-after-postgres-test >/dev/null 2>&1 || true' EXIT
until docker exec before-after-postgres-test pg_isready -U postgres -d before_after_test >/dev/null; do sleep 1; done
export TEST_DATABASE_URL=postgresql+psycopg://postgres:test@127.0.0.1:55433/before_after_test
POSTGRES_CLIENT_MAJOR=16 PYTHON_BIN=/opt/before-after/.venv/bin/python ./scripts/test-postgres.sh
```

`test-postgres.sh` fails closed when a native client major or Docker DAC proof
is unavailable; no mandatory PostgreSQL test is converted into a skip. The
fixed gate rejects all extra pytest selector/filter arguments and runs pytest
with plugin autoload, selector, `BUILD_SHA`, `FIXED_GATE_*`, `EVIDENCE_*`,
`PYTEST_PLUGINS`, `PYTHONPATH`, and ambient `PG*` variables removed. It writes
hashed JUnit output, a DAC proof marker, and commit/tree-bound completion
metadata to a private external directory. Use either a temporary directory or
a documented external artifact directory:

```bash
artifact_dir=$(mktemp -d)
BUILD_SHA=$(git rev-parse HEAD) FIXED_GATE_ARTIFACT_DIR="$artifact_dir" \
  POSTGRES_CLIENT_MAJOR=16 PYTHON_BIN=/opt/before-after/.venv/bin/python \
  ./scripts/test-postgres.sh
```

Generate a redacted local evidence artifact for an issue without collecting
clinical data. Keep both directories outside the repository and private:

```bash
evidence_dir=$(mktemp -d /tmp/before-after-evidence.XXXXXX)
chmod 700 "$evidence_dir"
FIXED_GATE_ARTIFACT_DIR="$artifact_dir" \
  python3 -m ops.evidence --output "$evidence_dir/slice8-local-evidence.json"
```

Use [`docs/issue-evidence-template.md`](issue-evidence-template.md) for the
ticket. The artifact explicitly records clinic hardware and TLS/LAN UAT as
`not_run`; local syntax/compile checks do not claim runtime success. Runtime
success is recorded only when the completed fixed-gate metadata matches the
current build SHA, its JUnit/XML and DAC proof artifacts pass independent
validation, and a detached signature verifies with the configured
`EVIDENCE_PUBLIC_KEY`.

CI or a maintainer signs the canonical metadata bytes (including the commit,
tree, JUnit hash, and DAC proof hash) with a private key held in CI secret
storage or an offline secret store:

```bash
: "${EVIDENCE_PRIVATE_KEY:?set EVIDENCE_PRIVATE_KEY to the signing key path}"
python3 -c 'import json,sys; from ops.evidence import canonical_fixed_gate_metadata; sys.stdout.buffer.write(canonical_fixed_gate_metadata(json.load(open(sys.argv[1]))))' \
  "$artifact_dir/slice8-fixed-gate.json" > "$artifact_dir/.slice8-fixed-gate.canonical"
openssl dgst -sha256 -sign "$EVIDENCE_PRIVATE_KEY" \
  -out "$artifact_dir/slice8-fixed-gate.json.sig" "$artifact_dir/.slice8-fixed-gate.canonical"
rm -f "$artifact_dir/.slice8-fixed-gate.canonical"
```

Use the exact external metadata and detached signature as certification inputs
(configure either `FIXED_GATE_ARTIFACT_DIR` or `FIXED_GATE_ARTIFACT`, not both):

```bash
env -u FIXED_GATE_ARTIFACT_DIR \
  FIXED_GATE_ARTIFACT="$artifact_dir/slice8-fixed-gate.json" \
  EVIDENCE_PUBLIC_KEY=/etc/before-after/evidence-public.pem \
  EVIDENCE_SIGNATURE="$artifact_dir/slice8-fixed-gate.json.sig" \
  python3 -m ops.evidence --certification \
    --output "$evidence_dir/slice8-local-evidence.json"
```

The private key is never stored in this repository or in an evidence artifact;
without the configured trusted public key and detached signature, runtime
evidence remains unverified/not-run and certification exits nonzero.

The acceptance tests use an injected storage policy only for their same-device
temporary test directory; production policy is never bypassed. Also run:

```bash
bash -n deploy/bootstrap.sh ops/backup.sh deploy/normalize-media-permissions.sh \
  deploy/verify-permissions.sh scripts/test-dac.sh scripts/test-postgres.sh
python3 -m compileall -q app migrations ops tests
sudo systemd-analyze verify /etc/systemd/system/before-after*.service \
  /etc/systemd/system/before-after-backup.timer
```

Real-clinic UAT still requires hardware and services unavailable on a local
checkout: the approved backup disk/mount, clinic CA/TLS trust and LAN DNS,
nginx/systemd on the clinic host, and a real clinic workstation/camera workflow.
No clinical data is used by the automated tests.
