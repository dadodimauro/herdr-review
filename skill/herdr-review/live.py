#!/usr/bin/env python3
"""Make review.json show the code as it is now in the checkout (the Herdr mod's live diff).

live.py --folder <review folder>

The mod runs it when a file in the checkout changes (an edit in nvim, Claude's change). It compares the working tree
with the review's base, as build.py does, and keeps Claude's notes for every hunk that did not change (build.py's
carry_over). It never writes notes.json: that file stays Claude's.

- A changed hunk has no note. When its file has no topic, it goes to the topic "Changed after the review".
- Syntax colors: reused for unchanged files, computed for changed ones. LSP tokens stay only on unchanged files.
- review.json gets "live": {"at", "needNotes": [hunk ids]}. db/meta.json changes, so the mod loads it.
Prints one JSON object: {"ok": true, "changed": n, "files": n} or {"ok": false, "error": "..."}.
"""

import argparse
import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LIVE_GROUP = {
    "id": "glive",
    "title": "Changed after the review",
    "summary": "Code that changed after Claude wrote the notes. Ask Claude to rebuild the review for notes.",
}


def load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def live(folder: Path) -> dict[str, Any]:
    notes = json.loads((folder / "notes.json").read_text(encoding="utf-8"))
    data = json.loads((folder / "review.json").read_text(encoding="utf-8"))
    build = load("code_review_diff_build", HERE / "lib" / "code-review-diff" / "build.py")
    extract = load("herdr_review_extract", HERE / "extract.py")

    kept = build.kept_build(notes, folder / "notes.json")
    os.chdir(kept["checkout"])
    build.check_checkout(kept)
    options = build.review_options({"base": None, "committed": None, "untracked": None}, kept, may_change=False)
    # Always the working tree: an edit that is not committed yet must show
    review = build.collect_review(options["base"], False, options["untracked"])

    # The notes of the review, carried to the hunks of now (a copy: notes.json is not changed)
    now = {"files": [build.skeleton_file(file) for file in review["files"]]}
    build.carry_over(now, copy.deepcopy(notes))
    groups = [dict(g) for g in notes["groups"]]
    # Changed after the review: a hunk whose lines (hash) the reviewed notes do not have
    reviewed = {(f["path"], h["hash"]) for f in notes["files"] for h in f["hunks"]}
    need = []
    for file in now["files"]:
        for hunk in file["hunks"]:
            if (file["path"], hunk["hash"]) not in reviewed:
                need.append(hunk["id"])
            if not (build.text_of(hunk["group"]) or build.text_of(file["group"])):
                hunk["group"] = LIVE_GROUP["id"]
    if any(h["group"] == LIVE_GROUP["id"] for f in now["files"] for h in f["hunks"]):
        groups.append(LIVE_GROUP)

    files = build.page_files(now, review)
    blobs = review["blobs"]
    # Syntax: the old spans for blobs that did not change, new spans for the others
    old_syntax = data.get("syntax") or {}
    fresh = [f for f in files if (f["oldBlob"] and f["oldBlob"] not in old_syntax) or (f["newBlob"] and f["newBlob"] not in old_syntax)]
    syntax = {**old_syntax, **build.collect_syntax(fresh, blobs)}
    data.update(
        {
            "headSha": review["head"],
            "dirty": review["dirty"],
            "stats": {
                "files": len(files),
                "adds": sum(f["adds"] for f in files),
                "dels": sum(f["dels"] for f in files),
            },
            "groups": [
                {
                    "id": g["id"],
                    "title": build.text_of(g["title"]),
                    "summary": build.diff_build.sanitize_note(build.text_of(g["summary"])),
                    "files": [i for i, f in enumerate(files) if any(h["group"] == g["id"] for h in f["hunks"])],
                }
                for g in groups
            ],
            "files": files,
        }
    )
    data["syntax"] = extract.diff_lines(data, syntax)
    data["tokens"] = extract.diff_lines(data, data.get("tokens") or {})
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["live"] = {"at": at, "needNotes": need}
    load_extract().write_atomic(folder / "review.json", json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    meta = {"buildId": data["buildId"], "builtAt": data["builtAt"], "liveAt": at}
    load_extract().write_atomic(folder / "db" / "meta.json", json.dumps(meta) + "\n")
    return {"ok": True, "changed": len(need), "files": len(files)}


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
        # build.py prints notes for a person (carried hunks, untracked files); only the JSON goes to stdout
        with contextlib.redirect_stdout(out):
            folder = args.folder.resolve()
            with load_extract().folder_lock(folder):
                result = live(folder)
    except SystemExit as error:
        result = {"ok": False, "error": str(error.code)}
    except subprocess.CalledProcessError as error:
        result = {"ok": False, "error": (error.stderr or str(error)).strip()}
    print(out.getvalue(), file=sys.stderr, end="")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
