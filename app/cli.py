#!/usr/bin/env python3
"""CLI: build a case board from a folder or explicit slot images."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.board import BoardTemplate, CaseData, export, render
from app.image_policy import ImagePolicyError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = ROOT / "app" / "templates" / "viengut_case.json"


def _collect_from_dir(folder: Path, slot_ids: list[str]) -> dict[str, Path]:
    """Map slot ids to images found in folder (by name prefix or sorted order)."""
    exts = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in exts)
    by_stem = {p.stem.lower(): p for p in files}
    images: dict[str, Path] = {}
    unused = list(files)
    for sid in slot_ids:
        key = sid.lower()
        if key in by_stem:
            images[sid] = by_stem[key]
            if by_stem[key] in unused:
                unused.remove(by_stem[key])
            continue
        # prefix match: clinical_1.jpg, xray_1.png, ...
        hit = next((p for p in unused if p.stem.lower().startswith(key)), None)
        if hit:
            images[sid] = hit
            unused.remove(hit)
    # fill remaining slots in template order from leftover files
    for sid in slot_ids:
        if sid in images or not unused:
            continue
        images[sid] = unused.pop(0)
    return images


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build before/after medical case board")
    p.add_argument("-t", "--template", type=Path, default=DEFAULT_TEMPLATE)
    p.add_argument("--title", required=True, help='e.g. "Nguyen Van A - 1990 - ID - City"')
    p.add_argument("-i", "--input-dir", type=Path, help="Folder of images auto-mapped to slots")
    p.add_argument(
        "--slot",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="Assign image to slot, repeatable",
    )
    p.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="ID=TEXT",
        help="Caption under slot, e.g. xray_1=24/06/2026",
    )
    p.add_argument("-o", "--output", type=Path, required=True)
    p.add_argument("--dpi", type=float, default=300)
    p.add_argument("--json-case", type=Path, help="Optional case JSON (title/images/labels)")
    args = p.parse_args(argv)

    try:
        tmpl = BoardTemplate.load(args.template)
        slot_ids = [s.id for s in tmpl.slots]

        title = args.title
        images: dict[str, Path] = {}
        labels: dict[str, str] = {}

        if args.json_case:
            data = json.loads(args.json_case.read_text(encoding="utf-8"))
            title = data.get("title", title)
            images.update({k: Path(v) for k, v in data.get("images", {}).items()})
            labels.update(data.get("labels", {}))

        if args.input_dir:
            images.update(_collect_from_dir(args.input_dir, slot_ids))

        for item in args.slot:
            if "=" not in item:
                p.error(f"--slot needs ID=PATH, got {item!r}")
            sid, path = item.split("=", 1)
            images[sid] = Path(path)

        for item in args.label:
            if "=" not in item:
                p.error(f"--label needs ID=TEXT, got {item!r}")
            sid, text = item.split("=", 1)
            labels[sid] = text

        case = CaseData(title=title, images=images, labels=labels)
        board = render(tmpl, case, dpi=args.dpi)
        out = export(board, args.output)
    except ImagePolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
