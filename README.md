# before-after

Small open-source replacement for CorelDRAW **PowerCLIP** case boards.

Typical use (Viengut-style medical sheet):

- title: `Name - Year - ID - City`
- fixed frames (slots) for clinical photos + X-rays
- each image is clipped into its frame (`cover` crop or `contain` letterbox)
- optional date captions under slots
- export **PNG** / **PDF**

## Quick start

```bash
cd before-after
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Web UI (recommended)

The Slice 1 app requires PostgreSQL and three runtime settings:

```bash
export DATABASE_URL=postgresql+psycopg://user:password@127.0.0.1/before_after
export MEDIA_ROOT=/var/lib/before-after/media
export SECRET_KEY='replace-with-a-random-secret'
alembic upgrade head
flask --app app:create_app create-admin
flask --app app:create_app run
# open http://127.0.0.1:5000/login
```

The legacy board remains available at `/prototype` during the transition.
The standalone renderer CLI below remains available for Slice 0 comparisons.

### CLI

```bash
python -m app.cli \
  --title "Nguyễn Thế Sơn - 1990 - VG2606000151 - Hải Phòng" \
  --input-dir ./examples/sample_case \
  --label xray_1=24/06/2026 \
  --label xray_2=31/07/2026 \
  -o output/case.png
```

Or assign slots explicitly:

```bash
python -m app.cli \
  --title "Patient - 1990 - ID - City" \
  --slot portrait=./p.jpg \
  --slot clinical_1=./c1.jpg \
  --slot xray_1=./x1.jpg \
  --label xray_1=24/06/2026 \
  -o output/case.pdf
```

## Template = layout

Edit `app/templates/viengut_case.json`:

| field | meaning |
|-------|---------|
| `width_mm` / `height_mm` | page size (default A4 landscape) |
| `slots[].id` | slot name (`portrait`, `clinical_1`, …) |
| `slots[].x/y/w/h` | frame position/size in **mm** |
| `slots[].fit` | `cover` (fill+crop) or `contain` (letterbox, good for X-ray) |

Add more templates as extra JSON files in `app/templates/`.

## Layout matching your Corel board

Default template mirrors the common foot-case sheet:

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
| Rectangle + PowerCLIP | slot frame + `cover`/`contain` |
| Manual arrange | JSON template (repeatable) |
| Export bitmap/PDF | Pillow PNG/PDF |

### Build path (suggested evolution)

1. **Now** — template JSON + CLI + local web UI  
2. **Next** — pan/zoom offset per slot (true PowerCLIP reframe)  
3. **Later** — multi-page cases, before/after pair templates, HIS filename parse  

## Requirements

- Python 3.10+
- Pillow, Flask (see `requirements.txt`)
