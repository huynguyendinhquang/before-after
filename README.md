# before-after

Small open-source replacement for CorelDRAW **PowerCLIP** case boards.

Typical use (Viengut-style medical sheet):

- title: `Name - Year - ID - City`
- structured Frames for clinical photos + X-rays
- each image is clipped into its Frame with a persisted cover crop
- optional Capture Date and Frame labels
- export **PNG** / **PDF**

## Quick start

```bash
cd before-after
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### One-command local demo

To evaluate the workflow without dealing with login setup, run:

```bash
./scripts/run-demo.sh
```

The script starts a dedicated PostgreSQL 16 container, migrates and seeds only
synthetic images, auto-logs in as a local Demo Admin, and binds the app to
`127.0.0.1:8765`. Demo Mode refuses to run outside `APP_ENV=development`.

### Web UI (recommended)

The Slice 1 app requires PostgreSQL and three runtime settings:

```bash
export DATABASE_URL=postgresql+psycopg://user:password@127.0.0.1/before_after
export MEDIA_ROOT=/var/lib/before-after/media
export SECRET_KEY='replace-with-a-random-secret'
export APP_ENV=development  # local HTTP development exception for Secure cookies
alembic upgrade head
flask --app app:create_app create-admin
flask --app app:create_app run
# open http://127.0.0.1:5000/login
```

`normalize_database_url` preserves the password in this normal in-process
Flask/Alembic path, so the documented raw `DATABASE_URL` works without a
`PGPASSFILE`. Native backup/restore child processes use a separate
credential-free URL and a protected `PGPASSFILE`; they never receive the raw
URL or password in argv/environment.

Production keeps `SESSION_COOKIE_SECURE` enabled and must run behind HTTPS
(the HTTPS deployment is part of Slice 8). Comparison Set preview and PNG/PDF
export are versioned server-side; export is restricted to Admins/Editors,
CSRF-protected, audited, and served with `Cache-Control: no-store`. Viewer
accounts can read Sets and previews but cannot export.

Production Gunicorn, nginx, systemd, environment, backup, and isolated
restore-check artifacts are in `deploy/`, `ops/`, and
[`docs/deployment.md`](docs/deployment.md). The backup timer deliberately stops
Gunicorn during the paired PostgreSQL/media copy and publishes only complete,
checksummed generations to a second device.

### PostgreSQL acceptance gate

The ordinary test run may skip database-backed Slice 1 tests when no database
URL is configured. The mandatory gate requires a disposable clean PostgreSQL
database, runs the Alembic migration from the test fixture, and fails loudly
when `TEST_DATABASE_URL` is missing:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://user:password@127.0.0.1/before_after_test
./scripts/test-postgres.sh
```

## Comparison Set preview and export

Open a persisted Comparison Set in the web UI. The server preview and the
Editor/Admin export form use the same versioned Canvas render specification;
choose PNG or PDF and submit the current Set version. Export derivatives are
stored outside the static web root and linked to an audited `Export` record.

## Structured Canvas layout

Canvas presets include 16:9, 16:10, A4 landscape, A4 portrait, and custom mm.
The Set stores a shared Frame ratio, column count, order, visibility, labels,
and normalized crop state. Hidden Frames are retained in the Set but excluded
from preview/export.

## Layout matching your Corel board

A three-column structured grid mirrors the common foot-case sheet:

```
+------------+--------+--------+--------+
|            | clin 1 | clin 2 | clin 3 |
|  portrait  +--------+--------+--------+
|            |   xray 1   |   xray 2    |
+------------+------------+-------------+
```

## Design notes

| CorelDRAW | this app |
|-----------|----------|
| Rectangle + PowerCLIP | structured Frame + normalized cover crop |
| Manual arrange | persisted Canvas/grid/order state |
| Export bitmap/PDF | audited Pillow PNG/PDF derivative |

### Build path (suggested evolution)

1. **Now** — persisted Comparison Sets + local web UI + audited export
2. **Next** — lifecycle and administration workflows
3. **Later** — multi-page cases, HIS integration, guided capture

## Requirements

- Python 3.10+
- Pillow, Flask (see `requirements.txt`)
