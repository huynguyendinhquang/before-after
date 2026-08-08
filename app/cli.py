#!/usr/bin/env python3
"""CLI: build a case board from a folder or explicit slot images."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.board import MAX_TITLE_CHARS, BoardTemplate, CaseData, export, render
from app.image_policy import ImagePolicyError, SUPPORTED_EXTENSIONS

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = ROOT / "app" / "templates" / "viengut_case.json"


def _string_mapping(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ImagePolicyError(f"case {field} must map strings to strings")
    return value


def _validate_title(title: object) -> str:
    if not isinstance(title, str):
        raise ImagePolicyError("case title must be a string")
    if len(title) > MAX_TITLE_CHARS:
        raise ImagePolicyError(f"case title exceeds {MAX_TITLE_CHARS} characters")
    return title


def _load_json_case(
    path: Path, default_title: str
) -> tuple[str, dict[str, Path], dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        detail = exc.strerror or exc.__class__.__name__
        raise ImagePolicyError(f"could not read case JSON: {detail}") from exc
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ImagePolicyError("invalid case JSON") from exc

    if not isinstance(data, dict):
        raise ImagePolicyError("case JSON must be an object")
    title = _validate_title(data.get("title", default_title))
    images = _string_mapping(data.get("images", {}), "images")
    labels = _string_mapping(data.get("labels", {}), "labels")
    return title, {key: Path(value) for key, value in images.items()}, labels


def _collect_from_dir(folder: Path, slot_ids: list[str]) -> dict[str, Path]:
    """Map slot ids to images found in folder (by name prefix or sorted order)."""
    try:
        files = sorted(p for p in folder.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS)
    except (OSError, ValueError) as exc:
        if isinstance(exc, OSError):
            detail = exc.strerror or exc.__class__.__name__
        else:
            detail = "invalid path"
        raise ImagePolicyError(f"could not read input directory: {detail}") from exc
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
        try:
            tmpl = BoardTemplate.load(args.template)
        except OSError as exc:
            detail = exc.strerror or exc.__class__.__name__
            raise ImagePolicyError(f"could not read template: {detail}") from exc
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ImagePolicyError("invalid template") from exc
        slot_ids = [s.id for s in tmpl.slots]

        title = args.title
        images: dict[str, Path] = {}
        labels: dict[str, str] = {}

        if args.json_case:
            case_title, case_images, case_labels = _load_json_case(args.json_case, title)
            title = case_title
            images.update(case_images)
            labels.update(case_labels)

        title = _validate_title(title)

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
    except (OSError, ValueError, OverflowError) as exc:
        detail = exc.strerror if isinstance(exc, OSError) else str(exc)
        print(f"error: {detail or exc.__class__.__name__}", file=sys.stderr)
        return 2

    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
