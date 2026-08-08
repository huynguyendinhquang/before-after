# Clinical Image Comparison MVP — Implementation Plan

This plan implements [`mvp-spec.md`](mvp-spec.md) incrementally on top of the existing Flask and Pillow prototype. Each slice must leave a runnable, demonstrable application; no big-bang rewrite.

## Constraints

- Keep Flask, Pillow, server-rendered Jinja, CSS, and vanilla JavaScript.
- Use one LAN modular monolith and one PostgreSQL database.
- Store immutable originals in a managed filesystem outside the static web root.
- Keep rendering synchronous until measurements prove a worker is necessary.
- Do not add microservices, Redis, a frontend framework, repository interfaces, AI alignment, HIS integration, mobile capture, or free-layout editing.
- Use millimetres for persisted Canvas dimensions. Preset 16:9 and 16:10 Canvases use a 297 mm width; custom Canvas accepts width and height in mm.

## Target modules

Keep feature modules as files until their size demonstrates a need for packages.

```text
app/
├── __init__.py            # create_app and configuration
├── __main__.py            # development entry point
├── db.py                  # Flask-SQLAlchemy extension
├── models.py              # ORM models and database constraints
├── auth.py                # login, roles, user administration
├── audit.py               # append audit event in caller transaction
├── patients.py            # Patient and Consent Confirmation flows
├── captures.py            # Capture Library and Shot Type flows
├── comparisons.py         # Comparison Set, Frame, lock, and export flows
├── storage.py             # managed filesystem operations
├── board.py               # pure layout, crop, render, and encode logic
├── templates/             # Jinja pages
└── static/                # CSS and vanilla JavaScript editor

migrations/                # Alembic migrations
tests/                     # focused integration and renderer tests
ops/                       # backup and restore-check scripts
deploy/                    # production service configuration
```

Do not create generic repositories or services. The feature module that owns an invariant also owns its transaction: authorize, validate, mutate, append audit, commit once.

## Deep module seams

### Managed filesystem — `app/storage.py`

One concrete module hides upload validation, safe storage keys, SHA-256, atomic writes, EXIF inspection, previews, quarantine, and cleanup. It is configured with `MEDIA_ROOT`; tests use a temporary directory.

Original image paths are never accepted from a request and `MEDIA_ROOT` is never served as a static directory.

### Renderer — `app/board.py`

The renderer accepts persistence-independent render data and returns an image or encoded bytes. It knows nothing about Flask, users, PostgreSQL, audit, or storage policy.

Minimum interface:

```python
layout_frames(spec) -> list[FrameGeometry]
render_canvas(spec, dpi) -> PIL.Image.Image
encode(image, format) -> bytes
```

Crop state is normalized and shared by browser and renderer:

- `zoom`: `1.0..5.0`; `1.0` is the minimum cover scale;
- `pan_x`, `pan_y`: `-1.0..1.0`;
- apply `ImageOps.exif_transpose()` before calculating geometry.

### Feature modules

`patients.py`, `captures.py`, and `comparisons.py` expose only their user workflows. Routes do not update ORM objects piecemeal. Tests exercise the same workflow functions/routes used by callers.

## Dependencies

Add only:

| Dependency | Purpose |
|---|---|
| `Flask-SQLAlchemy` | PostgreSQL session lifecycle |
| `psycopg[binary]` | PostgreSQL driver |
| `Alembic` | Schema migrations |
| `Flask-Login` | Named-user sessions |
| `Flask-WTF` | CSRF protection |
| `gunicorn` | Production WSGI server |
| `pytest` | Development test runner |

Use Werkzeug's password hashing already supplied through Flask. Keep Pillow for PDF; do not add ReportLab. Keep production dependencies in `requirements.txt` and test-only dependencies in `requirements-dev.txt`.

## Slice 0 — Verify trust boundaries and preserve the prototype

**User value:** none directly; this removes unknowns that would otherwise invalidate storage and crop work.

**Work**

- Add a smoke test for the current `app.board` PNG and PDF output.
- Confirm production `MEDIA_ROOT` supports atomic `os.replace` and is included in backup storage.
- Test real clinic images to set configurable byte and pixel limits.
- Confirm supported formats; start with JPEG, PNG, TIFF, and WebP, rejecting animation and unknown formats.
- Verify EXIF orientations 1, 6, and 8 through Pillow.
- Verify Vietnamese font availability on the target server.
- Keep the current prototype reachable during migration; do not extend its inline UI.

**Files:** `tests/test_legacy_board.py`, test assets, configuration documentation.

**Gate:** current sample renders PNG/PDF; EXIF and crop assumptions have executable tests; upload limits and production media path are recorded in configuration.

## Slice 1 — Login, Patient, consent, and audit foundation

**User value:** an Editor can log in, create/find a Patient, confirm consent, and reopen the record; a Viewer can only read.

**Migration `0001_identity_patients_audit`**

- `users`: normalized unique username, display name, password hash, role, active flag, timestamps.
- `patients`: unique patient ID, name, birth year, consent actor/time, archive fields, actor/timestamps.
- `audit_events`: actor, action, entity type/ID, JSON details, UTC timestamp.

**Work**

- Convert `app/__init__.py` to `create_app(config=None)` and validate `DATABASE_URL`, `MEDIA_ROOT`, and `SECRET_KEY`.
- Add `db.py`, `models.py`, `auth.py`, `audit.py`, and `patients.py`.
- Add login and Patient Jinja pages.
- Add `flask --app app:create_app create-admin`.
- Move the old one-page renderer under a temporary prototype route until export cutover.
- Enable secure session settings, CSRF, `debug=False` outside development, and server-side role guards.

**Smallest check:** Editor creates a Patient with consent and an audit event; Viewer mutation receives 403; Patient survives app restart.

**Gate:** migration applies to a clean PostgreSQL database and the complete Patient flow passes against PostgreSQL, not SQLite.

## Slice 2 — Capture Library and immutable originals

**User value:** an Editor uploads one Capture, confirms its Capture Date and Shot Type, and sees it after restart.

**Migration `0002_shot_types_captures`**

- `shot_types`: name, canonical/proposal/merged state, creator, optional canonical target.
- `captures`: Patient, Capture Date, Shot Type, storage key, original filename, format, dimensions, byte count, SHA-256, archive fields, actor/timestamps.
- Unique `(patient_id, sha256)` to prevent accidental duplicate storage for the same Patient.

**Work**

- Add `storage.py` with validate, inspect, atomic-store, resolve, preview, quarantine, and cleanup operations.
- Add `captures.py`, Capture form, Capture Library view, and typeahead.
- Support file-picker and drag-and-drop input through the same upload command.
- Treat EXIF `DateTimeOriginal` as a suggestion only; require explicit confirmation.
- Store original bytes unchanged; create a bounded preview derivative with metadata removed.
- Reject upload before Consent Confirmation and clean partial files on every failure.
- Support upload from both Patient Library and Add Frame callers through one Capture command.

**Smallest check:** upload a synthetic EXIF-oriented image, override its suggested date, verify original bytes/SHA-256 remain unchanged, and prove a failed upload leaves no row or file.

**Gate:** Capture persists, preview displays through an authorized route, and duplicate upload does not create a second original.

## Slice 3 — Persistent Comparison Sets and Frames

**User value:** an Editor creates a Set, adds existing or newly uploaded Captures, and reopens the same composition from another desktop.

**Migration `0003_comparison_sets_frames`**

- `comparison_sets`: Patient, name/title, Canvas dimensions, preset key, Frame ratio, columns, output-field flags, date-label default, version, edit-lock holder/expiry, archive fields, actor/timestamps.
- `frames`: Set, Capture, position, visible flag, label, date visibility override, zoom, pan X/Y.
- Constraints for unique active Set name per Patient, unique Frame position per Set, crop ranges, and same-Patient references enforced by commands.

**Work**

- Add `comparisons.py` and basic editor page.
- Add create, open, duplicate-later placeholder, and Add Frame workflows.
- Selecting an existing Capture and uploading a new Capture use the same Capture module from Slice 2.
- Default order is Capture Date ascending; persist manual reorder.
- Store explicit Canvas width/height instead of relying only on a preset key.

**Smallest check:** one Set receives an existing Capture and one inline upload; reload shows the same two Frame references and order, with only one stored copy per Capture.

**Gate:** another authorized desktop opens the same persisted state; Viewer sees it read-only.

## Slice 4 — Structured editor, crop, visibility, labels, and lock

**User value:** an Editor configures Canvas/grid and manually matches image framing without changing originals.

**Work**

- Add persistence-independent Canvas/Frame render data to `board.py`; leave legacy functions temporarily intact.
- Implement 16:9, 16:10, A4 landscape/portrait, and custom-mm Canvas choices.
- Implement shared Frame aspect ratio, column count, equal Frames, and centered final row.
- Implement cover crop with normalized pan/zoom.
- Add reorder, visible/hidden, Set-level date default, Frame override, Set title, and optional Patient fields.
- Add a five-minute edit lease with heartbeat and server time; other users become read-only.
- Require both the active lease and expected Set version for updates; stale saves return 409.
- Use server-produced geometry as the authority for preview/export. Browser interactions write only normalized state.

**Smallest checks**

- Synthetic four-corner image verifies pan and zoom extremes.
- Hidden Frames do not affect rendered layout.
- Two Editors cannot save concurrently; expired lock can be acquired; stale version cannot overwrite.
- Original SHA-256 is unchanged after all editing.

**Gate:** save/reload preserves Canvas, order, crop, visibility, and labels; server preview matches the persisted Set version.

## Slice 5 — Audited PNG/PDF export

**User value:** Editor/Admin exports exactly the visible Canvas; Viewer cannot export.

**Migration `0004_exports`**

- `exports`: Set, format, storage key, byte count, SHA-256, rendered Set version, actor/time.

**Work**

- Render preview and export through the same `board.py` interface.
- Read one immutable render specification for a specific Set version.
- Write derivative atomically, then record export and audit in one workflow; clean orphan derivatives if the transaction fails.
- Return PNG or PDF through an authorized route with `Cache-Control: no-store`.
- Remove the old `/render` path, fixed template UI, and legacy renderer only after the new export tests pass.

**Smallest check:** export a cropped Set containing a hidden Frame; assert PNG dimensions/pixels, PDF signature, derivative checksum, audit actor, and Viewer 403.

**Gate:** editor preview, PNG, and PDF represent the same Set version and never mutate Capture originals.

## Slice 6 — Lifecycle and administration

**User value:** the clinic can duplicate Sets, archive safely, manage users, and normalize Shot Types.

**Work**

- Duplicate Set in one transaction, copying Frame configuration while retaining Capture references.
- Archive/unarchive Captures and Sets.
- Block hard-delete while Frames reference a Capture; quarantine then delete unreferenced media safely.
- Add Admin user management; prevent disabling or demoting the final active Admin.
- Let Editors create Shot Type Proposals; let Admin promote or merge them and update affected Captures in one audited transaction.
- Add a bounded audit viewer that avoids unnecessary PII and original filenames.

**Smallest checks:** duplicate Set references the same Capture IDs; referenced Capture deletion fails; proposal merge updates all affected Captures; Editor receives 403 on Admin actions.

**Gate:** no lifecycle operation creates dangling Frame references or missing media.

## Slice 7 — Reviewed batch upload

**User value:** an Editor can import multiple images without silently trusting ambiguous dates or Shot Types.

**Work**

- Add batch selection as a secondary Capture Library flow.
- Inspect every image and require explicit Capture Date and Shot Type review before enabling commit.
- Revalidate all images server-side.
- Commit all-or-nothing; clean every newly written asset if any image fails.
- Keep the single-image flow unchanged.

**Smallest check:** one incomplete item causes zero Captures/files; a fully reviewed batch creates exactly one Capture and audit event per image.

**Gate:** batch never infers file modification date or bypasses consent, duplicate, size, format, or pixel checks.

## Slice 8 — LAN production, backup, and restore

**User value:** the clinic runs the system safely and can recover Patient records, originals, Sets, and exports after failure.

**Work**

- Run Gunicorn behind a LAN reverse proxy with HTTPS.
- Add production service configuration with least-privilege filesystem access.
- Add daily backup outside Flask: PostgreSQL custom-format dump plus media copy and generation manifest/checksums.
- Add isolated restore-check script: restore database/media, run migration check, verify sample checksums, and render one Set.
- Keep backups on a second storage device and document periodic restore drills.

**Smallest check:** restore the latest backup into isolated paths and perform login, read, and export smoke tests without touching production.

**Gate:** a tested restore reproduces database rows and matching media from the same backup generation.

## Migration strategy

1. Keep the current renderer and prototype route working through Slices 1–3.
2. Add the new renderer beside the legacy `BoardTemplate`, `Slot`, and `CaseData` functions in Slice 4.
3. Switch only Comparison Set preview/export to the new renderer.
4. Delete inline prototype UI, `viengut_case.json`, and legacy renderer after Slice 5 passes.
5. Keep the CLI only as an output comparator during cutover; remove it when it would bypass auth/audit. Retain Flask operational commands.
6. Do not build a prototype-data importer: the current app has no persistent records. Add one only if a real clinic dataset is identified.

## Security and data-integrity gates

These are part of the slices, not deferred hardening:

- HTTPS on LAN; secure, HttpOnly, SameSite cookies; production debug disabled.
- CSRF on every mutation and export.
- Server-side role and object authorization on every route.
- Configurable request-byte and decoded-pixel limits.
- No direct static access to originals or previews.
- Immutable originals, safe generated storage keys, and path traversal protection.
- Audit and mutation in the same database transaction.
- Database server time plus optimistic version for edit locks.
- No Patient name, original filename, or image metadata in routine logs/audit details.
- Paired database/media backup with a verified restore path.

## Delivery order

| Order | Slice | Status |
|---:|---|---|
| 0 | Trust-boundary spikes and prototype smoke test | MVP blocker |
| 1 | Login, Patient, consent, audit | MVP blocker |
| 2 | Capture Library and immutable originals | MVP blocker |
| 3 | Persistent Comparison Sets and Frames | MVP blocker |
| 4 | Structured editor, crop, labels, visibility, lock | MVP blocker |
| 5 | PNG/PDF export | MVP blocker |
| 6 | Lifecycle and administration | MVP blocker |
| 7 | Reviewed batch upload | MVP blocker |
| 8 | Production deployment, backup, restore | MVP blocker |

Deferred until measured need: browser E2E suite, async workers, Redis, object storage, cloud/multi-site, AI alignment, guided capture, HIS integration, mobile-first UI, and free layout.

## First implementation ticket

Start with Slice 0 and the app-factory portion of Slice 1 only:

1. add current-renderer smoke tests;
2. add the minimal runtime/test dependencies;
3. introduce `create_app()` without changing visible prototype behavior;
4. connect PostgreSQL and create migration `0001_identity_patients_audit`;
5. implement login plus Patient/Consent flow and its single integration test.

Do not begin Capture storage until this gate passes.
