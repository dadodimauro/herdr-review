#!/usr/bin/env python3
"""Load the PR's GitHub state again into review.json, without a new review (the Herdr mod's Check GitHub button).

github_refresh.py --folder <review folder>

It runs in the checkout that notes.json names. It prints one
JSON object: {"ok": true, "changes": [...], "rebuild": bool} or {"ok": false, "error": "..."}.
- changes: what is new on the PR since the last build or refresh (build.py's own words).
- rebuild: the code under review is not the code of review.json (new commits, or a changed working tree). The
  threads still load, on the lines that they can find. A full review needs Claude (/herdr-review).
It writes review.json ("github"), github.json (the new snapshot) and db/meta.json (so that the mod loads again).
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_build(scripts: Path) -> Any:
    spec = importlib.util.spec_from_file_location("code_review_diff_build", scripts / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refresh(folder: Path) -> dict[str, Any]:
    notes = json.loads((folder / "notes.json").read_text(encoding="utf-8"))
    data = json.loads((folder / "review.json").read_text(encoding="utf-8"))
    if not notes.get("pr"):
        return {"ok": False, "error": "This review has no PR."}
    build = load_build(Path(__file__).resolve().parent / "lib" / "code-review-diff")
    kept = build.kept_build(notes, folder / "notes.json")
    os.chdir(kept["checkout"])
    build.check_checkout(kept)
    options = build.review_options({"base": None, "committed": None, "untracked": None}, kept, may_change=False)
    review = build.collect_review(options["base"], options["committed"], options["untracked"])

    hunks = lambda files: {(h["id"], h["hash"]) for f in files for h in f["hunks"]}  # noqa: E731
    same_code = hunks(review["files"]) == hunks(notes["files"]) and {f["path"] for f in review["files"]} == {
        f["path"] for f in notes["files"]
    }
    # page_files needs a note entry for each hunk: with changed code, the threads use the hunks of review.json
    files = build.page_files(notes, review) if same_code else data["files"]
    blobs = review["blobs"] if same_code else None
    if blobs is None:
        # The blobs of the reviewed code are still in git: read them by id from the old page
        page = (folder / "review.html").read_text(encoding="utf-8")
        start = page.index('id="review-data">') + len('id="review-data">')
        blobs = json.loads(page[start : page.index("</script>", start)])["blobs"]

    repo = data["repo"]
    pull = build.diff_build.gh_api(f"repos/{repo}/pulls/{notes['pr']}")
    github, snapshot = build.collect_github(repo, pull, files, blobs)
    snapshot_path = folder / "github.json"
    changes = build.github_changes(json.loads(snapshot_path.read_text(encoding="utf-8")), snapshot) if snapshot_path.exists() else []

    data["github"] = github
    load_extract().write_atomic(folder / "review.json", json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {"buildId": data["buildId"], "builtAt": data["builtAt"], "githubAt": now}
    load_extract().write_atomic(folder / "db" / "meta.json", json.dumps(meta) + "\n")
    rebuild = not same_code or review["head"] != data["headSha"] or pull["head"]["sha"] != review["head"]
    return {"ok": True, "changes": changes, "rebuild": rebuild}


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
        # build.py prints notes for a person (untracked files); only the JSON goes to stdout
        with contextlib.redirect_stdout(out):
            folder = args.folder.resolve()
            with load_extract().folder_lock(folder):
                result = refresh(folder)
    except SystemExit as error:
        result = {"ok": False, "error": str(error.code)}
    except subprocess.CalledProcessError as error:
        result = {"ok": False, "error": (error.stderr or str(error)).strip()}
    print(out.getvalue(), file=sys.stderr, end="")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
