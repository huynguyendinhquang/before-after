#!/usr/bin/env python3
"""Local web UI: drop images into slots, export PNG/PDF."""

from __future__ import annotations

import io
import json
import re
import tempfile
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file

from app.board import MAX_TITLE_CHARS, BoardTemplate, CaseData, export, render
from app.image_policy import ImagePolicyError, configured_request_limit, open_image, read_bounded

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "app" / "templates"
DEFAULT_TEMPLATE = TEMPLATE_DIR / "viengut_case.json"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = configured_request_limit()

_TEMPLATE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_LABELS_JSON_BYTES = 64 * 1024
MAX_LABEL_ENTRIES = 100
MAX_LABEL_KEY_CHARS = 128
MAX_LABEL_VALUE_CHARS = 500

PAGE = r"""
<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Before/After Board</title>
  <style>
    :root { font-family: system-ui, sans-serif; color: #111; }
    body { margin: 0; background: #eef1f4; }
    header { background: #0f3d5c; color: #fff; padding: 12px 20px; }
    header h1 { margin: 0; font-size: 1.15rem; font-weight: 600; }
    main { display: grid; grid-template-columns: 340px 1fr; gap: 16px; padding: 16px; }
    .panel { background: #fff; border-radius: 10px; padding: 14px; box-shadow: 0 1px 3px #0001; }
    label { display: block; font-size: .8rem; color: #444; margin: 10px 0 4px; }
    input[type=text] { width: 100%; box-sizing: border-box; padding: 8px; border: 1px solid #ccc; border-radius: 6px; }
    .slots { display: grid; gap: 10px; margin-top: 8px; }
    .slot { border: 1px dashed #9aa; border-radius: 8px; padding: 8px; background: #fafbfc; }
    .slot h3 { margin: 0 0 6px; font-size: .85rem; }
    .slot input[type=file] { width: 100%; font-size: .8rem; }
    .actions { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
    button, .btn {
      background: #0f3d5c; color: #fff; border: 0; border-radius: 6px;
      padding: 9px 14px; cursor: pointer; font-size: .9rem; text-decoration: none;
    }
    button.secondary { background: #5a6b7a; }
    #preview { width: 100%; background: #ddd; border-radius: 8px; min-height: 360px; object-fit: contain; }
    .hint { font-size: .78rem; color: #666; margin-top: 8px; line-height: 1.4; }
    @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header><h1>Before / After Case Board</h1></header>
  <main>
    <section class="panel">
      <label>Title</label>
      <input id="title" type="text" placeholder="Nguyễn Văn A - 1990 - VG... - Hải Phòng"
             value="{{ default_title }}" />
      <label>Template</label>
      <input id="template" type="text" value="viengut_case" readonly />
      <div class="slots" id="slots"></div>
      <div class="actions">
        <button type="button" id="btnPreview">Preview</button>
        <button type="button" id="btnPng" class="secondary">Export PNG</button>
        <button type="button" id="btnPdf" class="secondary">Export PDF</button>
      </div>
      <p class="hint">
        PowerCLIP-style: each slot is a fixed frame; image is center-cropped (clinical)
        or letterboxed (x-ray). Fill captions for dates under slots.
      </p>
    </section>
    <section class="panel">
      <img id="preview" alt="preview will appear here" />
    </section>
  </main>
  <script>
    const SLOTS = {{ slots_json|safe }};
    const root = document.getElementById('slots');
    for (const s of SLOTS) {
      const div = document.createElement('div');
      div.className = 'slot';
      div.innerHTML = `
        <h3>${s.id} <small>(${s.w}×${s.h} mm, ${s.fit})</small></h3>
        <input type="file" accept="image/*" data-slot="${s.id}" />
        <label>Caption / date</label>
        <input type="text" data-label="${s.id}" placeholder="dd/mm/yyyy" />
      `;
      root.appendChild(div);
    }

    async function build(fmt) {
      const fd = new FormData();
      fd.append('title', document.getElementById('title').value);
      fd.append('template', document.getElementById('template').value);
      fd.append('format', fmt);
      const labels = {};
      for (const inp of document.querySelectorAll('input[data-label]')) {
        if (inp.value.trim()) labels[inp.dataset.label] = inp.value.trim();
      }
      fd.append('labels', JSON.stringify(labels));
      for (const inp of document.querySelectorAll('input[data-slot]')) {
        if (inp.files[0]) fd.append('slot_' + inp.dataset.slot, inp.files[0]);
      }
      const res = await fetch('/render', { method: 'POST', body: fd });
      if (!res.ok) { alert(await res.text()); return null; }
      return await res.blob();
    }

    document.getElementById('btnPreview').onclick = async () => {
      const blob = await build('png');
      if (!blob) return;
      document.getElementById('preview').src = URL.createObjectURL(blob);
    };
    async function download(fmt, name) {
      const blob = await build(fmt);
      if (!blob) return;
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      document.getElementById('preview').src = a.href;
    }
    document.getElementById('btnPng').onclick = () => download('png', 'case-board.png');
    document.getElementById('btnPdf').onclick = () => download('pdf', 'case-board.pdf');
  </script>
</body>
</html>
"""


def _valid_template_id(name: object) -> bool:
    return isinstance(name, str) and _TEMPLATE_ID.fullmatch(name) is not None


def _load_template(name: str = "viengut_case") -> BoardTemplate:
    if not _valid_template_id(name):
        raise ValueError("invalid template id")
    path = TEMPLATE_DIR / f"{name}.json"
    if not path.exists():
        path = DEFAULT_TEMPLATE
    return BoardTemplate.load(path)


@app.get("/")
def index():
    tmpl = _load_template()
    slots = [{"id": s.id, "w": s.w, "h": s.h, "fit": s.fit} for s in tmpl.slots]
    return render_template_string(
        PAGE,
        slots_json=json.dumps(slots),
        default_title="Patient - YYYY - ID - City",
    )


@app.get("/api/template/<name>")
def api_template(name: str):
    if not _valid_template_id(name):
        return jsonify(error="invalid template"), 400
    tmpl = _load_template(name)
    return jsonify(
        {
            "name": tmpl.name,
            "width_mm": tmpl.width_mm,
            "height_mm": tmpl.height_mm,
            "slots": [s.__dict__ for s in tmpl.slots],
        }
    )


@app.post("/render")
def do_render():
    title = request.form.get("title") or "Case"
    if len(title) > MAX_TITLE_CHARS:
        return jsonify(error="title is too long"), 400
    raw_template = request.form.get("template")
    tmpl_name = "viengut_case" if raw_template is None else raw_template
    if not _valid_template_id(tmpl_name):
        return jsonify(error="invalid template"), 400

    fmt = (request.form.get("format") or "png").lower()
    raw_labels = request.form.get("labels")
    raw_labels = "{}" if raw_labels is None else raw_labels
    try:
        labels_size = len(raw_labels.encode("utf-8"))
    except UnicodeError:
        return jsonify(error="invalid labels"), 400
    if labels_size > MAX_LABELS_JSON_BYTES:
        return jsonify(error="invalid labels"), 400
    try:
        labels = json.loads(raw_labels)
    except (json.JSONDecodeError, RecursionError):
        return jsonify(error="invalid labels"), 400
    if not isinstance(labels, dict) or len(labels) > MAX_LABEL_ENTRIES:
        return jsonify(error="invalid labels"), 400
    if any(
        not isinstance(key, str)
        or len(key) > MAX_LABEL_KEY_CHARS
        or not isinstance(value, str)
        or len(value) > MAX_LABEL_VALUE_CHARS
        for key, value in labels.items()
    ):
        return jsonify(error="invalid labels"), 400

    tmpl = _load_template(tmpl_name)
    images: dict[str, Path] = {}

    try:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            for slot in tmpl.slots:
                f = request.files.get(f"slot_{slot.id}")
                if not f or not f.filename:
                    continue
                # Stage once so validation and rendering use the same bytes;
                # FileStorage.save() would see EOF for a consumed stream.
                try:
                    payload = read_bounded(f.stream)
                    with open_image(payload):
                        pass
                except (ImagePolicyError, OSError, ValueError):
                    return jsonify(error="invalid image upload"), 400
                dest = tdir / f"{slot.id}.upload"
                dest.write_bytes(payload)
                images[slot.id] = dest

            case = CaseData(title=title, images=images, labels=labels)
            board = render(tmpl, case, dpi=200 if fmt == "png" else 300)

            buf = io.BytesIO()
            if fmt == "pdf":
                board.convert("RGB").save(buf, "PDF", resolution=300.0)
                buf.seek(0)
                return send_file(buf, mimetype="application/pdf", download_name="case-board.pdf")
            board.save(buf, "PNG")
            buf.seek(0)
            return send_file(buf, mimetype="image/png", download_name="case-board.png")
    except ImagePolicyError:
        return jsonify(error="invalid image upload"), 400


def main():
    app.run(host="127.0.0.1", port=8765, debug=True)


if __name__ == "__main__":
    main()
