#!/usr/bin/env python3
"""Hover text, definitions and uses for the Python lines of the review, from ty (the Herdr mod's LSP data).

lsp_tokens.py --folder <review folder>

It runs lib/create-diff/lsp.py (`ty server`, uv tool install ty, with the project's virtualenv) for the code that
review.json shows now (also after a live update). It writes "hovers", "definitions", "references"
and "tokens" in review.json and changes db/meta.json, so that the mod loads them.
Prints one JSON object: {"ok": true, "tokens": n, "seconds": s} or {"ok": false, "error": "..."}.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect(folder: Path) -> dict[str, Any]:
    if shutil.which("ty") is None:
        return {"ok": False, "error": "ty is not installed (uv tool install ty)"}
    started = time.monotonic()
    notes = json.loads((folder / "notes.json").read_text(encoding="utf-8"))
    data = json.loads((folder / "review.json").read_text(encoding="utf-8"))
    build = load("code_review_diff_build", HERE / "lib" / "code-review-diff" / "build.py")
    extract = load("herdr_review_extract", HERE / "extract.py")

    kept = build.kept_build(notes, folder / "notes.json")
    os.chdir(kept["checkout"])
    options = build.review_options({"base": None, "committed": None, "untracked": None}, kept, may_change=False)
    # The blobs of review.json: the working tree (a live update) or HEAD (the build)
    blobs: dict[str, list[str]] = {}
    for committed in (False, True):
        blobs.update(build.collect_review(options["base"], committed, options["untracked"])["blobs"])
    files = [
        {**f, "oldBlob": f["oldBlob"] if f["oldBlob"] in blobs else None, "newBlob": f["newBlob"] if f["newBlob"] in blobs else None}
        for f in data["files"]
    ]
    hovers, definitions, references, tokens = build.diff_build.collect_hovers([{"files": files}], blobs)
    data.update({"hovers": hovers, "definitions": definitions, "references": references})
    data["tokens"] = extract.diff_lines(data, {blob: {str(k): v for k, v in lines.items()} for blob, lines in tokens.items()})
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["lspAt"] = at
    load_extract().write_atomic(folder / "review.json", json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    meta_path = folder / "db" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.update({"buildId": data["buildId"], "lspAt": at})
    load_extract().write_atomic(meta_path, json.dumps(meta) + "\n")
    count = sum(len(spans) for lines in data["tokens"].values() for spans in lines.values())
    return {"ok": True, "tokens": count, "seconds": round(time.monotonic() - started, 1)}


def load_extract() -> Any:
    """extract.py, beside this file: folder_lock, write_atomic and diff_lines."""
    spec = importlib.util.spec_from_file_location("herdr_review_extract", Path(__file__).resolve().parent / "extract.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            folder = args.folder.resolve()
            with load_extract().folder_lock(folder):
                result = collect(folder)
    except SystemExit as error:
        result = {"ok": False, "error": str(error.code)}
    except (subprocess.CalledProcessError, OSError) as error:
        result = {"ok": False, "error": str(getattr(error, "stderr", None) or error).strip()}
    print(out.getvalue(), file=sys.stderr, end="")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
