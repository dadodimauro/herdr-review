#!/usr/bin/env python3
"""Make the files that the Herdr `review` mod reads, from the output of `lib/code-review-diff/build.py page`.

extract.py --folder <review folder> [--session <terminal id>]

It writes, in the review folder:
- review.json: the page data without the full file contents (blobs) and with syntax only for the diff lines.
  The mod can read at most 1 MiB, and the full page data is often more.
- db/comments, db/batches, db/ghthreads: one JSON file per document (the layout that github.py --docs reads).
  Each finding in findings/ becomes db/comments/<id>.json when that file does not exist yet.
- herdr.json: where this skill is and which Claude session gets the batches.
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

DATA_BLOCK = re.compile(r'<script[^>]*id="review-data"[^>]*>(.*?)</script>', re.DOTALL)
COLLECTIONS = ("comments", "batches", "ghthreads")
MOD_LIMIT = 1024 * 1024


@contextlib.contextmanager
def folder_lock(folder: Path) -> Iterator[None]:
    """One writer of review.json at a time: two mod tabs on one review run the helpers at the same time."""
    with open(folder / ".herdr.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def write_atomic(path: Path, text: str) -> None:
    """The reader (the mod) sees the old file or the new one, never half of it."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def page_data(page: Path) -> dict[str, Any]:
    match = DATA_BLOCK.search(page.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"{page} has no review-data block. Run build.py page first.")
    return json.loads(match.group(1))


def diff_lines(data: dict[str, Any], table: dict[str, dict[str, list[Any]]]) -> dict[str, dict[str, list[Any]]]:
    """The entries of a per-blob, per-line table (syntax or LSP tokens) for the lines that the hunks show.
    A context line is on both sides (the split view shows it twice): it keeps the entries of both blobs."""
    kept: dict[str, dict[str, list[Any]]] = {}

    def keep(blob: str | None, number: int) -> None:
        spans = blob and table.get(blob, {}).get(str(number))
        if spans:
            kept.setdefault(blob, {})[str(number)] = spans

    for file in data["files"]:
        for hunk in file["hunks"]:
            old_no, new_no = hunk["oldStart"], hunk["newStart"]
            for kind, _text in hunk["lines"]:
                if kind != "add":
                    keep(file["oldBlob"], old_no)
                    old_no += 1
                if kind != "del":
                    keep(file["newBlob"], new_no)
                    new_no += 1
    return kept


def slim(data: dict[str, Any]) -> dict[str, Any]:
    out = {key: value for key, value in data.items() if key not in ("blobs", "tokens")}
    out["syntax"] = diff_lines(data, data.get("syntax") or {})
    # LSP tokens: [col, len, hover, definition, references] per line; the three lists stay whole (they are small)
    out["tokens"] = diff_lines(data, data.get("tokens") or {})
    return out


def seed_findings(folder: Path) -> int:
    added = 0
    for path in sorted((folder / "findings").glob("*.json")):
        target = folder / "db" / "comments" / path.name
        if not target.exists():
            target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            added += 1
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", type=Path, required=True, help="the review folder")
    parser.add_argument("--session", help="the Claude terminal that gets the batches ($HC_TERMINAL_ID)")
    args = parser.parse_args()

    folder = args.folder.resolve()
    with folder_lock(folder):
        write_files(folder, args)


def write_files(folder: Path, args: argparse.Namespace) -> None:
    data = slim(page_data(folder / "review.html"))
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if len(text.encode("utf-8")) > MOD_LIMIT:
        # The mod reads the file with exec("cat") when it is too large for readTextFile: a warning only
        print(f"warning: review.json is {len(text) // 1024} KB, more than 1 MiB", file=sys.stderr)
    write_atomic(folder / "review.json", text)

    for collection in COLLECTIONS:
        (folder / "db" / collection).mkdir(parents=True, exist_ok=True)
    added = seed_findings(folder)
    # The mod watches db/ only (a PR review folder also has the whole worktree): this file tells it to load review.json again
    meta = {"buildId": data["buildId"], "builtAt": data["builtAt"]}
    write_atomic(folder / "db" / "meta.json", json.dumps(meta) + "\n")

    herdr_path = folder / "herdr.json"
    herdr = json.loads(herdr_path.read_text(encoding="utf-8")) if herdr_path.exists() else {}
    skill = Path(__file__).resolve().parent
    herdr.update({"skill": str(skill), "scripts": str(skill / "lib" / "code-review-diff")})
    herdr.pop("project", None)
    if args.session:
        herdr["session"] = args.session
    herdr_path.write_text(json.dumps(herdr, indent=2) + "\n", encoding="utf-8")

    hunks = sum(len(file["hunks"]) for file in data["files"])
    print(
        f"Wrote {folder / 'review.json'} ({len(text) // 1024} KB): {len(data['groups'])} topics, "
        f"{len(data['files'])} files, {hunks} hunks. {added} new findings in db/comments."
    )


if __name__ == "__main__":
    main()
