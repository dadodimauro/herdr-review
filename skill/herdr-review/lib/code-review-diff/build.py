#!/usr/bin/env python3
"""Build the review page: the branch diff in topics, with Claude's notes, findings and the GitHub threads.

build.py skeleton [--base origin/main] [--[no-]committed] [--untracked PATH ...] [--previous old-notes.json] --out notes.json
build.py page --notes notes.json --out review.html [--no-hovers]
build.py check --snapshot github.json

skeleton keeps the checkout and the options in notes.json ("build"); page and skeleton --previous use them.
"""

import argparse
import functools
import hashlib
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pygments.lexer import Lexer
from pygments.lexers import get_lexer_for_filename
from pygments.token import Comment, Keyword, Name, Number, Operator, String
from pygments.util import ClassNotFound

DIFF_DIR = Path(__file__).resolve().parent.parent / "create-diff"
sys.path.insert(0, str(DIFF_DIR))  # create-diff/build.py does "import lsp" at the top
_spec = importlib.util.spec_from_file_location("create_diff_build", DIFF_DIR / "build.py")
assert _spec is not None and _spec.loader is not None
diff_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diff_build)
import lsp  # noqa: E402

TEMPLATE = Path(__file__).with_name("template.html")
CONTEXT_LINES = 2
MAX_BLOB_BYTES = 1_000_000
MAX_SYNTAX_LINES = 20_000
PREVIEW_LINES = 8
SNIPPET_ABOVE = 3
THREAD_CONTEXT = 3
TITLE_LIMIT = 120
SIGNS = {"add": "+", "del": "-", "ctx": " "}
KIND_ORDER = ("code", "config", "docs", "test")
TEST_NAME = re.compile(r"^test_.*\.py$|_test\.py$|\.(test|spec)\.tsx?$")
DOCS_SUFFIXES = (".md", ".rst", ".txt")
CONFIG_SUFFIXES = (".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".tf", ".lock")
GROUP_ID = re.compile(r"^g[0-9a-z-]*$")
# Mermaid loads an image URL and makes a link for each viewer, also in strict mode
DIAGRAM_URL = re.compile(r"//|\bhttps?:", re.IGNORECASE)
LINE_KEY = re.compile(r"^([LR])([1-9]\d*)$")
QUOTED_HALF = re.compile(r'"(?:[^"\\]|\\.)*"')
C_QUOTED = re.compile(r"\\([0-7]{3}|.)|([^\\]+)")
C_ESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r"}
# The same paths and prefixes whatever the user's git config says (core.quotePath, diff.noprefix, diff.mnemonicPrefix)
DIFF_OPTIONS = ("--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", f"-U{CONTEXT_LINES}")
SEVERITIES = ("critical", "major", "minor", "nit")
CATEGORIES = ("bug", "security", "performance", "design", "tests", "style", "docs")
# First match wins: Keyword.Constant must come before Keyword
SYNTAX_KINDS = (
    (Comment, "c"),
    (String, "s"),
    (Number, "n"),
    (Keyword.Constant, "n"),
    (Keyword, "k"),
    (Operator.Word, "k"),
    (Name.Builtin, "b"),
    (Name.Function, "f"),
    (Name.Class, "t"),
    (Name.Namespace, "t"),
    (Name.Tag, "t"),
    (Name.Decorator, "d"),
    (Name.Attribute, "d"),
)
THREADS_QUERY = (
    "query($o:String!,$r:String!,$n:Int!,$endCursor:String){repository(owner:$o,name:$r){pullRequest(number:$n)"
    "{reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor}"
    "nodes{id isResolved comments(first:1){nodes{databaseId}}}}}}}"
)
CODE_CHANGED = "The code changed since the skeleton. Run skeleton again with --previous {notes}."
BUILD_DEFAULTS = {"base": "origin/main", "committed": False}
CHECK_RESULTS = {
    "SUCCESS": "success",
    "SKIPPED": "skipped",
    "NEUTRAL": "skipped",
    "STALE": "skipped",
    "FAILURE": "failure",
    "ERROR": "failure",
    "TIMED_OUT": "failure",
    "CANCELLED": "failure",
    "ACTION_REQUIRED": "failure",
    "STARTUP_FAILURE": "failure",
}
CHECK_ORDER = ("failure", "pending", "success", "skipped")
# As in the reviewer list on GitHub: a later comment does not replace an approval or a change request
OPINIONS = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
STATE_NEWS = {
    "open": "The PR is open now.",
    "draft": "The PR is a draft now.",
    "merged": "The PR is merged now.",
    "closed": "The PR is closed now.",
}
DECISION_WORDS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes requested",
    "REVIEW_REQUIRED": "review required",
}
REVIEW_WORDS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "requested changes",
    "COMMENTED": "commented",
    "DISMISSED": "has a dismissed review",
}
CI_NEWS = {
    "failure": "CI fails now",
    "pending": "CI has not completed",
    "success": "CI passes now",
    "none": "No checks now",
}


def git(*args: str, ok: tuple[int, ...] = (0,)) -> str:
    result = subprocess.run(["git", *args], capture_output=True)
    if result.returncode not in ok:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8", errors="replace")


def text_of(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def unquote_path(text: str) -> str:
    """A path as git prints it: git puts a path with special characters in quotes, with C escapes."""
    if not (len(text) > 1 and text[0] == text[-1] == '"'):
        return text
    raw = bytearray()
    for escape, plain in C_QUOTED.findall(text[1:-1]):
        if plain:
            raw += plain.encode()
        elif len(escape) == 3:
            raw.append(int(escape, 8))
        else:
            raw += C_ESCAPES.get(escape, escape).encode()
    return raw.decode("utf-8", errors="replace")


def header_path(line: str) -> str:
    """The b-side path of "diff --git a/P b/P". Paths with spaces work because both halves are equal."""
    rest = line.removeprefix("diff --git ")
    if first := QUOTED_HALF.match(rest):
        # "a/P" "b/P". A renamed file can have one quoted half only, and its "rename to" line gives the path.
        return unquote_path(rest[first.end() + 1 :]).removeprefix("b/")
    path = rest[2 : 2 + (len(rest) - 5) // 2]
    return path if rest == f"a/{path} b/{path}" else rest.split(" b/", 1)[-1]


def parse_files(diff: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    old_left = new_left = 0
    for line in (raw.removesuffix("\r") for raw in diff.split("\n")):
        # Inside a hunk the header counts tell where it ends, so a deleted "-- x" line is not a "--- x" header
        if old_left > 0 or new_left > 0:
            if line.startswith("\\"):
                continue
            kind = {"+": "add", "-": "del"}.get(line[:1], "ctx")
            old_left -= kind != "add"
            new_left -= kind != "del"
            files[-1]["hunks"][-1]["lines"].append([kind, line[1:]])
        elif line.startswith("diff --git "):
            path = header_path(line)
            files.append({"path": path, "oldPath": None, "status": "modified", "binary": False, "hunks": []})
        elif match := diff_build.HUNK_HEADER.match(line):
            files[-1]["hunks"].append({"oldStart": int(match[1]), "newStart": int(match[3]), "lines": []})
            old_left, new_left = int(match[2] or 1), int(match[4] or 1)
        elif line.startswith("new file mode"):
            files[-1]["status"] = "added"
        elif line.startswith("deleted file mode"):
            files[-1]["status"] = "deleted"
        elif line.startswith("rename from "):
            files[-1] |= {"status": "renamed", "oldPath": unquote_path(line.removeprefix("rename from "))}
        elif line.startswith("rename to "):
            files[-1]["path"] = unquote_path(line.removeprefix("rename to "))
        elif line.startswith("Binary files ") and line.endswith(" differ"):
            files[-1]["binary"] = True
    for file in files:
        if file["binary"] and file["status"] == "modified":
            file["status"] = "binary"
    return files


def read_blob(rev: str | None, path: str) -> bytes | None:
    """The file at `rev`, or in the working tree when `rev` is None. None when it does not exist there."""
    if rev is None:
        # git diffs a symlink as the text of its target: never read the file it points to
        if Path(path).is_symlink():
            return os.fsencode(os.readlink(path))
        return Path(path).read_bytes() if Path(path).is_file() else None
    result = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True)
    return result.stdout if result.returncode == 0 else None


def store_blob(blobs: dict[str, list[str]], content: bytes | None) -> str | None:
    if content is None or len(content) > MAX_BLOB_BYTES:
        return None
    blob_id = hashlib.sha1(content).hexdigest()[:16]
    if blob_id not in blobs:
        lines = [line.removesuffix("\r") for line in content.decode("utf-8", errors="replace").split("\n")]
        blobs[blob_id] = lines[:-1] if lines and lines[-1] == "" else lines
    return blob_id


def file_kind(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if path.startswith("tests/") or "/__tests__/" in f"/{path}" or TEST_NAME.search(name):
        return "test"
    if name.endswith(DOCS_SUFFIXES):
        return "docs"
    if name.endswith(CONFIG_SUFFIXES) or name.startswith("Dockerfile") or ".lock." in name:
        return "config"
    return "code"


def numbered(hunk: dict[str, Any]) -> Iterator[tuple[str, str, int | None, int | None]]:
    """Each hunk line with its old and new number (None on the side where the line does not exist)."""
    old_no, new_no = hunk["oldStart"], hunk["newStart"]
    for kind, text in hunk["lines"]:
        yield kind, text, None if kind == "add" else old_no, None if kind == "del" else new_no
        old_no += kind != "add"
        new_no += kind != "del"


def hunk_numbers(hunk: dict[str, Any]) -> dict[str, set[int]]:
    sides: dict[str, set[int]] = {"L": set(), "R": set()}
    for _, _, old_no, new_no in numbered(hunk):
        if old_no is not None:
            sides["L"].add(old_no)
        if new_no is not None:
            sides["R"].add(new_no)
    return sides


def number_range(numbers: set[int]) -> str:
    return f"{min(numbers)}-{max(numbers)}" if numbers else "none"


def merge_repeats(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per path. git lists a path twice when its type changes (a file becomes a symlink): a deletion and
    an addition. A file removed from the index but still on disk comes as a tracked deletion plus an untracked file."""
    by_path: dict[str, dict[str, Any]] = {}
    for file in files:
        if (first := by_path.setdefault(file["path"], file)) is not file:
            first["hunks"] += file["hunks"]
            first["binary"] = first["binary"] or file["binary"]
            first["status"] = "binary" if first["binary"] else "modified"
    return list(by_path.values())


def kept_build(notes: dict[str, Any], path: Path) -> dict[str, Any]:
    if not isinstance(build := notes.get("build"), dict):
        raise SystemExit(
            f'{path} has no "build": an older build.py wrote it. Add the options of its first build by hand: '
            '{"checkout": <path>, "branch": <branch, or HEAD when detached>, "base": <base>, "committed": <bool>, '
            '"untracked": []}'
        )
    return build


def review_options(given: dict[str, Any], kept: dict[str, Any] | None, may_change: bool) -> dict[str, Any]:
    """The build options of a review. notes.json keeps them, so that each build of one review shows the same code.
    skeleton --previous can change one (for example a PR with a new base) and says so. page cannot: its notes are
    for the hunks of the old value."""
    options: dict[str, Any] = {}
    for key, default in BUILD_DEFAULTS.items():
        value, old = given[key], (kept or {}).get(key)
        if value is not None and old is not None and value != old:
            if not may_change:
                raise SystemExit(
                    f"This review was built with {key} = {old!r}, not {value!r}. "
                    "To change it, run skeleton --previous with the new value."
                )
            print(f"Changed {key}: {old!r} -> {value!r}")
        options[key] = value if value is not None else old if old is not None else default
    # The user says yes to each untracked file: one that comes later can be a secret or a data export
    if given["untracked"] and not may_change:
        raise SystemExit("page cannot add untracked files. Run skeleton --previous with --untracked <path>.")
    options["untracked"] = sorted(set((kept or {}).get("untracked") or []) | set(given["untracked"] or []))
    return options


def check_checkout(kept: dict[str, Any] | None) -> tuple[str, str]:
    """This checkout and its branch ("HEAD" when detached). A review of a branch runs in any checkout of that branch.
    A review of a PR runs in its own detached worktree."""
    toplevel = git("rev-parse", "--show-toplevel").strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if not kept:
        return toplevel, branch
    old_branch, old_checkout = kept.get("branch"), kept.get("checkout")
    if old_branch and old_branch != "HEAD":
        if branch != old_branch:
            raise SystemExit(
                f"This review is of branch {old_branch}, but {toplevel} is on {branch}. "
                f"Run build.py in a checkout of {old_branch} (git worktree list shows them)."
            )
    elif old_checkout and Path(old_checkout).resolve() != Path(toplevel).resolve():
        if not Path(old_checkout).exists():
            raise SystemExit(
                f"The checkout of this review ({old_checkout}) does not exist now. "
                "Make it again (SKILL.md, Rebuild) and run build.py there."
            )
        raise SystemExit(f"This review was built in {old_checkout}. Run build.py there, not in {toplevel}.")
    return toplevel, branch


def fetch_base(base: str) -> None:
    """A PR starts from a new main: with an old local origin/main, other merged PRs come into the review."""
    if base == "origin/HEAD":
        base = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", ok=(0, 1, 128)).strip() or base
    if (match := re.fullmatch(r"origin/(.+)", base)) and match[1] != "HEAD":
        # An explicit refspec: a plain "git fetch origin <x>" can leave origin/<x> as it was
        refspec = f"+refs/heads/{match[1]}:refs/remotes/origin/{match[1]}"
        result = subprocess.run(["git", "fetch", "--quiet", "origin", refspec], capture_output=True, text=True)
        if result.returncode:
            print(
                f"warning: could not fetch {base}: {result.stderr.strip()} The review uses the old local copy.",
                file=sys.stderr,
            )


def collect_review(base: str, committed: bool, untracked: list[str]) -> dict[str, Any]:
    if not (merge_base := git("merge-base", base, "HEAD", ok=(0, 1)).strip()):
        raise SystemExit(f"{base} and HEAD have no common commit.")
    diff = ["-c", "core.quotePath=false", "diff", *DIFF_OPTIONS]
    files = parse_files(git(*diff, "-M", merge_base, *(["HEAD"] if committed else [])))
    found = [] if committed else git("ls-files", "--others", "--exclude-standard", "-z").split("\0")[:-1]
    # An untracked file can be a local secret or a data export: it goes into the page only when the user says so
    included, left_out = [p for p in found if p in untracked], [p for p in found if p not in untracked]
    for path in included:
        files += parse_files(git(*diff, "--no-index", "--", "/dev/null", path, ok=(0, 1)))
    files = merge_repeats(files)

    blobs: dict[str, list[str]] = {}
    for file in files:
        binary = file.pop("binary")
        old = None if binary or file["status"] == "added" else read_blob(merge_base, file["oldPath"] or file["path"])
        new = None if binary or file["status"] == "deleted" else read_blob("HEAD" if committed else None, file["path"])
        file["oldBlob"], file["newBlob"] = store_blob(blobs, old), store_blob(blobs, new)
        file["kind"] = file_kind(file["path"])
        file["adds"] = sum(kind == "add" for hunk in file["hunks"] for kind, _ in hunk["lines"])
        file["dels"] = sum(kind == "del" for hunk in file["hunks"] for kind, _ in hunk["lines"])
        for n, hunk in enumerate(file["hunks"], start=1):
            hunk["id"] = f"{file['path']}#{n}"
            hunk["hash"] = hashlib.sha1(json.dumps(hunk["lines"]).encode()).hexdigest()[:12]
    files.sort(key=lambda f: (KIND_ORDER.index(f["kind"]), f["path"]))
    if left_out:
        print(
            f"Left out {len(left_out)} untracked files. Show the user this list. Add a file (--untracked <path>) only after their yes:"
        )
        print("\n".join(f"  {p}" for p in left_out))
    return {
        "mergeBase": merge_base,
        "head": git("rev-parse", "HEAD").strip(),
        "dirty": not committed and bool(included or git("diff", "--name-only", "HEAD").strip()),
        "files": files,
        "blobs": blobs,
    }


def preview(hunk: dict[str, Any]) -> list[str]:
    changed = [
        f"R{new_no} + {text}" if kind == "add" else f"L{old_no} - {text}"
        for kind, text, old_no, new_no in numbered(hunk)
        if kind != "ctx"
    ]
    return changed[:PREVIEW_LINES]


def skeleton_file(file: dict[str, Any]) -> dict[str, Any]:
    # oldStart / newStart let a later "skeleton --previous" shift the line notes
    hunks = [
        {
            "id": hunk["id"],
            "hash": hunk["hash"],
            "oldStart": hunk["oldStart"],
            "newStart": hunk["newStart"],
            "group": "",
            "note": "",
            "lines": {},
            "changed": preview(hunk),
        }
        for hunk in file["hunks"]
    ]
    return {
        "path": file["path"],
        "status": file["status"],
        "adds": file["adds"],
        "dels": file["dels"],
        "group": "",
        "note": "",
        "hunks": hunks,
    }


def shift_line_notes(lines: dict[str, str], old_delta: int, new_delta: int) -> dict[str, str]:
    shifted = {}
    for key, text in lines.items():
        if match := LINE_KEY.match(key):
            key = f"{match[1]}{int(match[2]) + (new_delta if match[1] == 'R' else old_delta)}"
        shifted[key] = text
    return shifted


def carry_over(notes: dict[str, Any], old: dict[str, Any]) -> None:
    for key in ("title", "heading", "intro", "ticket", "pr", "effort", "diagram", "groups"):
        if key in old:
            notes[key] = old[key]
    old_files = {file["path"]: file for file in old.get("files", [])}
    # Two hunks of one file can have the same lines, so the same hash: pair them in order, and only when the count
    # did not change. Else no one can tell which note belongs to which hunk.
    old_hunks: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for file in old.get("files", []):
        for hunk in file.get("hunks", []):
            old_hunks.setdefault((file["path"], hunk["hash"]), []).append(hunk)
    new_count = Counter((file["path"], hunk["hash"]) for file in notes["files"] for hunk in file["hunks"])

    carried, total, need = 0, 0, []
    for file in notes["files"]:
        if old_file := old_files.get(file["path"]):
            file["group"], file["note"] = old_file.get("group", ""), old_file.get("note", "")
        for hunk in file["hunks"]:
            total += 1
            same_hash = (file["path"], hunk["hash"])
            if (same := old_hunks.get(same_hash, [])) and len(same) == new_count[same_hash]:
                new_count[same_hash] -= 1
                old_hunk = same.pop(0)
                carried += 1
                hunk["group"], hunk["note"] = old_hunk.get("group", ""), old_hunk.get("note", "")
                hunk["lines"] = shift_line_notes(
                    old_hunk.get("lines", {}),
                    hunk["oldStart"] - old_hunk["oldStart"],
                    hunk["newStart"] - old_hunk["newStart"],
                )
            if not (text_of(hunk["note"]) or text_of(file["note"])):
                need.append(hunk["id"])
    print(f"carried {carried} of {total} hunks; need notes: {', '.join(need) or 'none'}")


def diff_text(review: dict[str, Any], base: str, committed: bool) -> str:
    numbers = [n for f in review["files"] for h in f["hunks"] for _, _, *pair in numbered(h) for n in pair if n]
    width = len(str(max(numbers, default=1))) + 2
    target = f"HEAD {review['head'][:9]}" if committed else "the working tree"
    out = [
        f"Review diff: {target} against {base} (merge base {review['mergeBase'][:9]})"
        + (", with uncommitted or untracked changes" if review["dirty"] else ""),
        "Line columns: sign, L<old line>, R<new line>, text. Line-note keys and findings use these numbers.",
        "",
    ]
    for file in review["files"]:
        status = f"renamed from {file['oldPath']}" if file["oldPath"] else file["status"]
        out.append(f"=== {file['path']}  ({status}, {file['kind']}, +{file['adds']} -{file['dels']}) ===")
        if not file["hunks"]:
            out.append("(no text diff: this file needs a file-level note)")
        for hunk in file["hunks"]:
            sides = hunk_numbers(hunk)
            out.append(
                f"--- {hunk['id']}  hash {hunk['hash']}  "
                f"old {number_range(sides['L'])}  new {number_range(sides['R'])} ---"
            )
            for kind, text, old_no, new_no in numbered(hunk):
                left = f"L{old_no}" if old_no is not None else ""
                right = f"R{new_no}" if new_no is not None else ""
                out.append(f"{SIGNS[kind]} {left:<{width}}{right:<{width}} {text}")
        out.append("")
    return "\n".join(out)


def write_skeleton(given: dict[str, Any], out: Path, previous: Path | None) -> None:
    if previous is None and out.exists():
        raise SystemExit(
            f"{out} exists: this review has notes. Run Rebuild (skeleton --previous) to keep them and the page, "
            "or delete the file to start a new review."
        )
    old = json.loads(previous.read_text(encoding="utf-8")) if previous else None
    kept = kept_build(old, previous) if old is not None and previous else None
    options = review_options(given, kept, may_change=True)
    checkout, branch = check_checkout(kept)
    base, committed = options["base"], options["committed"]
    fetch_base(base)
    review = collect_review(base, committed, options["untracked"])
    notes: dict[str, Any] = {
        "title": "",
        "heading": "",
        "intro": "",
        "ticket": "",
        "pr": None,
        "effort": None,
        "diagram": "",
        "groups": [{"id": "g1", "title": "", "summary": ""}],
        "files": [skeleton_file(file) for file in review["files"]],
        "findings": [],
    }
    if old:
        carry_over(notes, old)
    # Later builds of this review (a Rebuild in another session too) take the checkout and the options from here
    notes["build"] = {"checkout": checkout, "branch": branch, **options}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(notes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    diff_path = out.with_suffix(".diff.txt")
    diff_path.write_text(diff_text(review, base, committed), encoding="utf-8")
    hunks = sum(len(file["hunks"]) for file in review["files"])
    print(f"Wrote {out} and {diff_path}: {len(review['files'])} files, {hunks} hunks, dirty={review['dirty']}")


def finding_side(finding: dict[str, Any], file: dict[str, Any]) -> tuple[str, str | None]:
    """The L/R key letter and the blob id of the side the finding is on."""
    return ("R", file["newBlob"]) if finding["side"] == "RIGHT" else ("L", file["oldBlob"])


def finding_id(finding: dict[str, Any], line_text: str) -> str:
    key = f"{finding['path']}|{finding['side']}|{line_text}|{text_of(finding['title'])}"
    return "f-" + hashlib.sha1(key.encode()).hexdigest()[:12]


def check_finding(
    finding: dict[str, Any], files_by_path: dict[str, dict[str, Any]], blobs: dict[str, list[str]]
) -> tuple[list[str], list[str]]:
    errors = []
    if finding.get("severity") not in SEVERITIES:
        errors.append(f"severity must be one of {', '.join(SEVERITIES)}.")
    if finding.get("category") not in CATEGORIES:
        errors.append(f"category must be one of {', '.join(CATEGORIES)}.")
    if not 1 <= len(text_of(finding.get("title"))) <= TITLE_LIMIT:
        errors.append(f"title must have 1-{TITLE_LIMIT} characters.")
    if not text_of(finding.get("body")):
        errors.append("body is empty.")
    if finding.get("side") not in ("RIGHT", "LEFT"):
        return [*errors, "side must be RIGHT or LEFT."], []
    if (suggestion := finding.get("suggestion")) and (finding["side"] != "RIGHT" or not isinstance(suggestion, str)):
        errors.append("a suggestion must be text, on side RIGHT only.")
    if (file := files_by_path.get(str(finding.get("path")))) is None:
        return [*errors, f"path {finding.get('path')!r} is not in the diff."], []
    key, blob_id = finding_side(finding, file)
    if blob_id is None:
        side_name = "new" if key == "R" else "old"
        return [*errors, f"{file['path']} has no {side_name} side ({file['status']} or too big)."], []
    line, length = finding.get("line"), len(blobs[blob_id])
    start = line if finding.get("startLine") is None else finding["startLine"]
    if not (isinstance(line, int) and isinstance(start, int) and 1 <= start <= line <= length):
        return [*errors, f"lines must be 1 <= startLine <= line <= {length} (the length of that side)."], []
    if all(any(n in hunk_numbers(hunk)[key] for hunk in file["hunks"]) for n in (start, line)):
        return errors, []
    return errors, [f"{key}{line} is outside every hunk of {file['path']}: GitHub takes it only as a file comment."]


def check_header(notes: dict[str, Any]) -> list[str]:
    errors = []
    for key in ("title", "heading"):
        if not text_of(notes.get(key)):
            errors.append(f'"{key}" is empty.')
    if notes.get("pr") is not None and not isinstance(notes["pr"], int):
        errors.append('"pr" must be a PR number or null.')
    effort = notes.get("effort")
    if effort is not None and not (
        isinstance(effort, dict)
        and effort.get("level") in range(1, 6)
        and isinstance(effort.get("minutes"), int)
        and effort["minutes"] > 0
    ):
        errors.append('"effort" must be null or {"level": 1-5, "minutes": <minutes>}.')
    if DIAGRAM_URL.search(text_of(notes.get("diagram"))):
        errors.append('"diagram" must not contain a URL: the page draws it for each viewer.')
    groups = notes.get("groups") or []
    group_ids = [group.get("id") for group in groups]
    for gid in sorted({gid for gid in group_ids if group_ids.count(gid) > 1}, key=str):
        errors.append(f"Topic id {gid} is used {group_ids.count(gid)} times.")
    for group in groups:
        if not isinstance(group.get("id"), str) or not GROUP_ID.match(group["id"]):
            errors.append(f"Topic id {group.get('id')!r} must match ^g[0-9a-z-]*$.")
        for key in ("title", "summary"):
            if not text_of(group.get(key)):
                errors.append(f"Topic {group.get('id')}: the {key} is empty.")
    return errors


def check_hunk_notes(notes: dict[str, Any], review: dict[str, Any]) -> list[str]:
    errors = []
    group_ids = [group.get("id") for group in notes.get("groups") or []]
    used: set[str] = set()
    notes_files = {file["path"]: file for file in notes["files"]}
    for file in review["files"]:
        file_notes = notes_files[file["path"]]
        file_group, file_note = text_of(file_notes.get("group")), text_of(file_notes.get("note"))
        if file_group and file_group not in group_ids:
            errors.append(f"File {file['path']}: topic {file_group} does not exist.")
        if not file["hunks"] and not file_note:
            errors.append(f"File {file['path']} has no text diff ({file['status']}), so it needs a file note.")
        hunk_notes = {hunk["id"]: hunk for hunk in file_notes["hunks"]}
        for hunk in file["hunks"]:
            entry = hunk_notes[hunk["id"]]
            if not (group := text_of(entry.get("group")) or file_group):
                errors.append(f'Hunk {hunk["id"]}: no topic. Set "group" on the hunk or on its file.')
            elif group not in group_ids:
                errors.append(f"Hunk {hunk['id']}: topic {group} does not exist.")
            used.add(group)
            if not (text_of(entry.get("note")) or file_note):
                errors.append(f'Hunk {hunk["id"]}: no note. Set "note" on the hunk or on its file.')
            sides = hunk_numbers(hunk)
            for key, value in (entry.get("lines") or {}).items():
                if not (match := LINE_KEY.match(key)):
                    errors.append(f'Hunk {hunk["id"]}: line-note key "{key}" must be L<old line> or R<new line>.')
                elif int(match[2]) not in sides[match[1]]:
                    lines = f"L {number_range(sides['L'])}, R {number_range(sides['R'])}"
                    errors.append(f"Hunk {hunk['id']}: line note {key} is not a line of this hunk ({lines}).")
                elif not text_of(value):
                    errors.append(f"Hunk {hunk['id']}: line note {key} is empty.")
    for gid in group_ids:
        if gid not in used:
            errors.append(f"Topic {gid}: no hunk is in this topic.")
    return errors


def validate(notes: dict[str, Any], review: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors = check_header(notes) + check_hunk_notes(notes, review)
    warnings: list[str] = []
    files_by_path = {file["path"]: file for file in review["files"]}
    seen: dict[str, int] = {}
    for n, finding in enumerate(notes.get("findings") or [], start=1):
        where = f"Finding {n} ({text_of(finding.get('title'))[:50] or 'no title'})"
        finding_errors, finding_warnings = check_finding(finding, files_by_path, review["blobs"])
        errors += [f"{where}: {error}" for error in finding_errors]
        warnings += [f"{where}: {warning}" for warning in finding_warnings]
        if not finding_errors:
            _, blob_id = finding_side(finding, files_by_path[finding["path"]])
            doc_id = finding_id(finding, review["blobs"][blob_id][finding["line"] - 1])
            if doc_id in seen:
                errors.append(f"{where}: same path, side, line text and title as finding {seen[doc_id]}.")
            seen[doc_id] = n
    return errors, warnings


def lexer_for(path: str) -> Lexer | None:
    try:
        return get_lexer_for_filename(path, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


@functools.cache
def syntax_kind(token_type: Any) -> str:
    return next((kind for parent, kind in SYNTAX_KINDS if token_type in parent), "")


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def highlight(lines: list[str], lexer: Lexer) -> dict[str, list[list[Any]]]:
    """Per line [col, len, kind] spans, in UTF-16 units like JS string indexes."""
    if len(lines) > MAX_SYNTAX_LINES:
        return {}
    text = "\n".join(lines) + "\n"
    spans: dict[int, list[list[Any]]] = {}
    pos = row = col = 0
    # Token indexes are not usable: Markdown code blocks restart them at 0. The values follow the text in order.
    for _, token_type, value in lexer.get_tokens_unprocessed(text):
        if not text.startswith(value, pos):
            return {}
        pos += len(value)
        kind = syntax_kind(token_type)
        for offset, part in enumerate(value.split("\n")):
            if offset:
                row, col = row + 1, 0
            width = utf16_len(part)
            if kind and width:
                line_spans = spans.setdefault(row + 1, [])
                if line_spans and line_spans[-1][2] == kind and line_spans[-1][0] + line_spans[-1][1] == col:
                    line_spans[-1][1] += width
                else:
                    line_spans.append([col, width, kind])
            col += width
    return {str(line): line_spans for line, line_spans in spans.items()}


def collect_syntax(files: list[dict[str, Any]], blobs: dict[str, list[str]]) -> dict[str, dict[str, list[list[Any]]]]:
    syntax: dict[str, dict[str, list[list[Any]]]] = {}
    for file in files:
        for blob_id, path in ((file["oldBlob"], file["oldPath"] or file["path"]), (file["newBlob"], file["path"])):
            if blob_id and blob_id not in syntax and (lexer := lexer_for(path)):
                syntax[blob_id] = highlight(blobs[blob_id], lexer)
    return {blob_id: spans for blob_id, spans in syntax.items() if spans}


def find_line(blob: list[str], text: str, wanted: int, above: list[str] | None = None) -> int | None:
    """The line closest to `wanted` that has this text and, just before it, the `above` lines."""
    above = above or []
    matches = [
        n
        for n, line in enumerate(blob, start=1)
        if line == text and n > len(above) and blob[n - 1 - len(above) : n - 1] == above
    ]
    return min(matches, key=lambda n: (abs(n - wanted), n)) if matches else None


def github_thread(
    root: dict[str, Any],
    replies: list[dict[str, Any]],
    node: dict[str, Any] | None,
    files_by_path: dict[str, dict[str, Any]],
    blobs: dict[str, list[str]],
) -> dict[str, Any]:
    side = root.get("side") or "RIGHT"
    rows = [row.removesuffix("\r") for row in (root.get("diff_hunk") or " ").rstrip("\n").split("\n")]
    line_text = rows[-1][1:]
    wanted = root.get("line") or root.get("original_line")
    above: list[str] = []
    if root.get("line") is None:
        # GitHub lost the line, and the last line alone can be a generic "return": the lines above it must match too
        skip = ("@@", "\\", "-" if side == "RIGHT" else "+")
        above = [row[1:] for row in rows[:-1] if not row.startswith(skip)][-THREAD_CONTEXT:]
    file = files_by_path.get(root["path"])
    blob_id = file and (file["newBlob"] if side == "RIGHT" else file["oldBlob"])
    # A comment on the whole file has line 1 and no diff hunk: it has no line, the page shows it on the file
    on_file = root.get("subject_type") == "file"
    line = find_line(blobs[blob_id], line_text, wanted, above) if blob_id and wanted and not on_file else None
    start_line = root.get("start_line") if root.get("line") else root.get("original_start_line")
    if line and wanted and start_line and (root.get("start_side") or side) == side:
        start_line += line - wanted
        start_line = start_line if 1 <= start_line < line else None
    else:
        start_line = None
    return {
        "id": root["id"],
        "nodeId": node["id"] if node else None,
        "path": root["path"],
        "side": side,
        "line": line,
        "startLine": start_line,
        "lineText": line_text,
        "url": root["html_url"],
        "resolved": bool(node and node["isResolved"]),
        "outdated": root.get("line") is None,
        "file": on_file,
        "comments": [
            {
                "id": comment["id"],
                "author": comment["user"]["login"],
                "bot": diff_build.is_bot(comment["user"]),
                "at": comment["created_at"],
                "body": comment["body"],
                "url": comment["html_url"],
            }
            for comment in [root, *sorted(replies, key=lambda c: c["created_at"])]
        ],
    }


def pr_lists(repo: str, pr: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The line comments, the reviews and the conversation comments of the PR, all pages."""
    comments, reviews, issue_comments = (
        [item for page in diff_build.gh_api("--paginate", "--slurp", f"repos/{repo}/{path}") for item in page]
        for path in (f"pulls/{pr}/comments", f"pulls/{pr}/reviews", f"issues/{pr}/comments")
    )
    return comments, reviews, issue_comments


def check_result(check: dict[str, Any]) -> str:
    """A check run has a status, and a conclusion when it is complete. A commit status has only a state."""
    if check.get("__typename") == "CheckRun" and check.get("status") != "COMPLETED":
        return "pending"
    return CHECK_RESULTS.get(check.get("conclusion") or check.get("state") or "", "pending")


def pr_status(repo: str, pull: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """The PR as the top of its GitHub page shows it: state, author, reviewers, review decision and checks."""
    fields = "author,reviewDecision,statusCheckRollup"
    view = json.loads(
        subprocess.run(
            ["gh", "pr", "view", str(pull["number"]), "-R", repo, "--json", fields],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    reviewers: dict[str, dict[str, Any]] = {}
    for review in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        user = review["user"]
        # A PENDING review (only its author sees it) has no submitted_at
        if not review.get("submitted_at") or user["login"] == pull["user"]["login"]:
            continue
        if review["state"] in OPINIONS or reviewers.get(user["login"], {}).get("state") not in OPINIONS:
            reviewers[user["login"]] = {
                "login": user["login"],
                "bot": diff_build.is_bot(user),
                "state": review["state"],
                "at": review["submitted_at"],
                "sha": review.get("commit_id") or "",
            }
    for user in pull.get("requested_reviewers") or []:
        reviewers[user["login"]] = {
            "login": user["login"],
            "bot": False,
            **reviewers.get(user["login"], {}),
            "requested": True,
        }
    for team in pull.get("requested_teams") or []:
        login = f"{repo.split('/')[0]}/{team['slug']}"
        reviewers[login] = {"login": login, "team": True, "requested": True}
    # Every run, as on GitHub: a run that a newer run of the same workflow cancelled also makes the PR red there
    checks = sorted(
        (
            {
                "name": check.get("name") or check.get("context") or "check",
                "workflow": check.get("workflowName") or "",
                "result": check_result(check),
                "url": check.get("detailsUrl") or check.get("targetUrl") or "",
            }
            for check in view.get("statusCheckRollup") or []
        ),
        key=lambda c: (CHECK_ORDER.index(c["result"]), c["workflow"].lower(), c["name"].lower()),
    )
    merged = bool(pull.get("merged") or pull.get("merged_at"))
    return {
        "number": pull["number"],
        "title": pull["title"],
        "url": pull["html_url"],
        "state": "merged"
        if merged
        else "closed"
        if pull["state"] == "closed"
        else "draft"
        if pull.get("draft")
        else "open",
        "author": pull["user"]["login"],
        "authorName": (view.get("author") or {}).get("name") or "",
        "headRef": pull["head"]["ref"],
        "baseRef": pull["base"]["ref"],
        "headSha": pull["head"]["sha"],
        "commits": pull["commits"],
        "closedAt": pull.get("closed_at"),
        "mergedBy": (pull.get("merged_by") or {}).get("login"),
        "reviewDecision": view.get("reviewDecision") or None,
        "reviewers": sorted(reviewers.values(), key=lambda r: (not r.get("requested"), r["login"].lower())),
        "checks": checks,
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def github_snapshot(
    repo: str,
    status: dict[str, Any],
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    issue_comments: list[dict[str, Any]],
) -> dict[str, Any]:
    """What the page knows about the PR: the refresh check compares GitHub with it."""
    # The latest review of each reviewer, any state: a new one is news even when the reviewer list looks the same
    latest: dict[str, dict[str, Any]] = {}
    for review in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        if review.get("submitted_at") and review["user"]["login"] != status["author"]:
            latest[review["user"]["login"]] = {"review": review["id"], "state": review["state"]}
    reviewers = {r["login"]: {"requested": bool(r.get("requested")), "review": None} for r in status["reviewers"]}
    for login, review in latest.items():
        reviewers[login] = {"requested": False, **reviewers.get(login, {}), **review}
    return {
        "v": 1,
        "repo": repo,
        "pr": status["number"],
        "fetchedAt": status["fetchedAt"],
        "status": {key: status[key] for key in ("state", "title", "headSha", "commits", "baseRef", "reviewDecision")},
        "reviewers": reviewers,
        "checks": check_map(status["checks"]),
        "commentIds": sorted(c["id"] for c in comments),
        # The page leaves out the conversation comments of bots, so a new one is no news for it
        "conversationIds": sorted(
            [c["id"] for c in issue_comments if not diff_build.is_bot(c["user"])]
            + [r["id"] for r in reviews if r.get("submitted_at") and (r["body"] or "").strip()]
        ),
    }


def check_map(checks: list[dict[str, Any]]) -> dict[str, str]:
    """One result for each check name. The checks are sorted failure first, so a name keeps its worst run."""
    results: dict[str, str] = {}
    for check in checks:
        results.setdefault(" / ".join(filter(None, (check["workflow"], check["name"]))), check["result"])
    return results


def ci_state(checks: dict[str, str]) -> str:
    """As GitHub rolls up the checks: one failure fails, then one pending check, and skipped checks pass."""
    results = set(checks.values())
    return next((result for result in ("failure", "pending") if result in results), "success" if results else "none")


def name_list(names: list[str]) -> str:
    return f"{', '.join(names[:5])}{' …' if len(names) > 5 else ''}"


def github_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """One plain line for each thing that is new on the PR since the old snapshot."""
    changes = []
    was, now = old["status"], new["status"]
    if now["headSha"] != was["headSha"]:
        added = now["commits"] - was["commits"]
        head = now["headSha"][:7]
        changes.append(
            f"{added} new commit{'s' if added > 1 else ''} (head {head})."
            if added > 0
            else f"The branch changed (head {head}): a force push or a rebase."
        )
    if now["baseRef"] != was["baseRef"]:
        changes.append(f"The PR now merges into {now['baseRef']} (before: {was['baseRef']}).")
    if now["state"] != was["state"]:
        ready = was["state"] == "draft" and now["state"] == "open"
        changes.append("The PR is ready for review now." if ready else STATE_NEWS[now["state"]])
    if now["title"] != was["title"]:
        changes.append(f"New title: {now['title']}")
    if now["reviewDecision"] != was["reviewDecision"]:
        changes.append(f"Review status: {DECISION_WORDS.get(now['reviewDecision'], 'no review required')}.")
    for login, reviewer in new["reviewers"].items():
        before = old["reviewers"].get(login, {"requested": False, "review": None})
        reviewed = reviewer["review"] is not None and reviewer["review"] != before["review"]
        if reviewed:
            changes.append(f"{login} {REVIEW_WORDS.get(reviewer['state'], reviewer['state'].lower())}.")
        if reviewer["requested"] and not before["requested"]:
            changes.append(f"{login} was asked to review.")
        elif before["requested"] and not reviewer["requested"] and not reviewed:
            changes.append(f"{login} is not asked to review now.")
    for login, before in old["reviewers"].items():
        if before["requested"] and login not in new["reviewers"]:
            changes.append(f"{login} is not asked to review now.")
    failing = sorted(name for name, result in new["checks"].items() if result == "failure")
    newly_failing = [name for name in failing if old["checks"].get(name) != "failure"]
    fixed = sorted(name for name, result in old["checks"].items() if result == "failure" and name not in failing)
    if ci_state(new["checks"]) != ci_state(old["checks"]) or newly_failing:
        changes.append(f"{CI_NEWS[ci_state(new['checks'])]}{f': {name_list(failing)}' if failing else ''}.")
    elif fixed:
        changes.append(f"Checks that pass now: {name_list(fixed)}.")
    for key, where in (("commentIds", "on lines"), ("conversationIds", "in the conversation")):
        if count := len(set(new[key]) - set(old[key])):
            changes.append(f"{count} new comment{'s' if count > 1 else ''} {where}.")
    return changes


def check_github(snapshot_path: Path) -> None:
    """Print, as JSON, what is new on the PR since the page was built."""
    if not snapshot_path.is_file():
        raise SystemExit(f"{snapshot_path} does not exist. Build the page again (build.py page) to write it.")
    old = json.loads(snapshot_path.read_text(encoding="utf-8"))
    repo, pr = old["repo"], old["pr"]
    try:
        pull = diff_build.gh_api(f"repos/{repo}/pulls/{pr}")
        comments, reviews, issue_comments = pr_lists(repo, pr)
        new = github_snapshot(repo, pr_status(repo, pull, reviews), comments, reviews, issue_comments)
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Could not read PR {pr} from GitHub: {(error.stderr or '').strip()}") from error
    print(
        json.dumps({"changes": github_changes(old, new), "checkedAt": new["fetchedAt"]}, indent=2, ensure_ascii=False)
    )


def collect_github(
    repo: str, pull: dict[str, Any], files: list[dict[str, Any]], blobs: dict[str, list[str]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    owner, name = repo.split("/")
    gh_api = diff_build.gh_api
    pr = pull["number"]
    comments, reviews, issue_comments = pr_lists(repo, pr)
    query = ["-f", f"query={THREADS_QUERY}", "-f", f"o={owner}", "-f", f"r={name}", "-F", f"n={pr}"]
    nodes = {
        node["comments"]["nodes"][0]["databaseId"]: node
        for page in gh_api("graphql", "--paginate", "--slurp", *query)
        for node in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        if node["comments"]["nodes"]
    }

    replies: dict[int, list[dict[str, Any]]] = {}
    for comment in comments:
        if comment.get("in_reply_to_id"):
            replies.setdefault(comment["in_reply_to_id"], []).append(comment)
    files_by_path = {file["path"]: file for file in files}
    threads = [
        github_thread(root, replies.get(root["id"], []), nodes.get(root["id"]), files_by_path, blobs)
        for root in comments
        if not root.get("in_reply_to_id")
    ]
    conversation = [
        {
            "author": r["user"]["login"],
            "bot": diff_build.is_bot(r["user"]),
            "at": r["submitted_at"],
            "body": r["body"],
            "url": r["html_url"],
        }
        # A PENDING review (only its author sees it) has no submitted_at
        for r in reviews
        if r.get("submitted_at") and (r["body"] or "").strip()
    ] + [
        {"author": c["user"]["login"], "bot": False, "at": c["created_at"], "body": c["body"], "url": c["html_url"]}
        for c in issue_comments
        if not diff_build.is_bot(c["user"])
    ]
    status = pr_status(repo, pull, reviews)
    github = {"threads": threads, "conversation": sorted(conversation, key=lambda c: c["at"]), "pr": status}
    return github, github_snapshot(repo, status, comments, reviews, issue_comments)


def branch_name(head: str, pull: dict[str, Any] | None) -> str:
    """The page keys its localStorage by branch. A detached worktree has no branch: use the PR head, else the commit."""
    if (branch := git("rev-parse", "--abbrev-ref", "HEAD").strip()) != "HEAD":
        return branch
    return pull["head"]["ref"] if pull else f"detached-{head[:9]}"


def pull_problem(pull: dict[str, Any], head: str) -> str | None:
    """Why the PR may not be the reviewed code: it has another branch, and its head has no link to HEAD."""
    pr_head = pull["head"]["sha"]
    if pr_head == head or git("rev-parse", "--abbrev-ref", "HEAD").strip() == pull["head"]["ref"]:
        return None
    # A squash merge of the PR: GitHub ends its subject with "(#<number>)"
    if git("log", "-1", "--format=%s", "HEAD").rstrip().endswith(f"(#{pull['number']})"):
        return None
    # Local commits not pushed yet, or new commits on the PR. A commit that is not in this clone proves nothing.
    for older, newer in ((pr_head, head), (head, pr_head)):
        if subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], capture_output=True).returncode != 1:
            return None
    return f"PR {pull['number']} is branch {pull['head']['ref']} at {pr_head[:9]}: no link to HEAD {head[:9]}."


def pull_ahead(pull: dict[str, Any], head: str) -> str | None:
    """The open PR has code that the reviewed code does not have: the page would show older code than GitHub."""
    pr_head = pull["head"]["sha"]
    if pr_head == head or pull["state"] != "open":
        return None
    # Exit 1: a known commit that is not in HEAD. Exit 128: a commit that this clone does not have yet.
    if subprocess.run(["git", "merge-base", "--is-ancestor", pr_head, head], capture_output=True).returncode == 0:
        return None
    return (
        f"The PR head on GitHub ({pr_head[:9]}) is not in the reviewed code ({head[:9]}): "
        "get the PR commits (fetch, or pull) and build again, or the page shows older code than GitHub."
    )


def page_files(notes: dict[str, Any], review: dict[str, Any]) -> list[dict[str, Any]]:
    notes_files = {file["path"]: file for file in notes["files"]}
    files = []
    for file in review["files"]:
        file_notes = notes_files[file["path"]]
        file_group = text_of(file_notes.get("group"))
        hunk_notes = {hunk["id"]: hunk for hunk in file_notes["hunks"]}
        hunks = []
        for hunk in file["hunks"]:
            entry = hunk_notes[hunk["id"]]
            line_notes = entry.get("lines") or {}
            hunks.append(
                {
                    "id": hunk["id"],
                    "hash": hunk["hash"],
                    "group": text_of(entry.get("group")) or file_group,
                    "note": diff_build.sanitize_note(text_of(entry.get("note"))),
                    "oldStart": hunk["oldStart"],
                    "newStart": hunk["newStart"],
                    "lines": hunk["lines"],
                    "lineNotes": {key: diff_build.sanitize_note(text_of(value)) for key, value in line_notes.items()},
                }
            )
        lexer = lexer_for(file["path"])
        files.append(
            {
                "path": file["path"],
                "oldPath": file["oldPath"],
                "status": file["status"],
                "lang": lexer.aliases[0] if lexer and lexer.aliases else "",
                "kind": file["kind"],
                "adds": file["adds"],
                "dels": file["dels"],
                "group": file_group,
                "note": diff_build.sanitize_note(text_of(file_notes.get("note"))),
                "oldBlob": file["oldBlob"],
                "newBlob": file["newBlob"],
                "hunks": hunks,
            }
        )
    return files


def finding_doc(
    finding: dict[str, Any], file: dict[str, Any], blobs: dict[str, list[str]], meta: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """The db document of a finding (spec 7.2) and its id."""
    _, blob_id = finding_side(finding, file)
    assert blob_id is not None  # validate() refuses a finding on a side that has no file
    blob = blobs[blob_id]
    line = finding["line"]
    start = finding.get("startLine") or line
    anchor = {"type": "line", "path": file["path"], "side": finding["side"], "line": line}
    if start < line:
        anchor["startLine"] = start
    snippet_start = max(1, start - SNIPPET_ABOVE)
    doc: dict[str, Any] = {
        "v": 1,
        "kind": "finding",
        "author": "claude",
        "body": text_of(finding["body"]),
        "createdAt": meta["builtAt"],
        "anchor": anchor,
        "lineText": blob[line - 1],
        "snippet": {"start": snippet_start, "lines": blob[snippet_start - 1 : line]},
        "buildId": meta["buildId"],
        "headSha": meta["headSha"],
        "resolved": False,
        "status": "draft",
        "severity": finding["severity"],
        "category": finding["category"],
        "title": text_of(finding["title"]),
    }
    if suggestion := finding.get("suggestion"):
        doc["suggestion"] = suggestion.rstrip("\n")
    return finding_id(finding, blob[line - 1]), doc


def write_finding_docs(folder: Path, notes: dict[str, Any], files: list[dict[str, Any]], data: dict[str, Any]) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.json"):
        old.unlink()
    files_by_path = {file["path"]: file for file in files}
    findings = notes.get("findings") or []
    for finding in findings:
        doc_id, doc = finding_doc(finding, files_by_path[finding["path"]], data["blobs"], data)
        (folder / f"{doc_id}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(findings)


def write_page(given: dict[str, Any], notes_path: Path, out: Path, use_lsp: bool) -> None:
    notes = json.loads(notes_path.read_text(encoding="utf-8"))
    kept = kept_build(notes, notes_path)
    options = review_options(given, kept, may_change=False)
    check_checkout(kept)
    base = options["base"]
    fetch_base(base)
    review = collect_review(base, options["committed"], options["untracked"])
    notes_hunks = {(hunk["id"], hunk["hash"]) for file in notes["files"] for hunk in file["hunks"]}
    review_hunks = {(hunk["id"], hunk["hash"]) for file in review["files"] for hunk in file["hunks"]}
    if notes_hunks != review_hunks or {f["path"] for f in notes["files"]} != {f["path"] for f in review["files"]}:
        raise SystemExit(CODE_CHANGED.format(notes=notes_path))
    errors, warnings = validate(notes, review)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if errors:
        print(f"{notes_path} has {len(errors)} problems:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)

    files = page_files(notes, review)
    blobs = review["blobs"]
    groups = [
        {
            "id": group["id"],
            "title": text_of(group["title"]),
            "summary": diff_build.sanitize_note(text_of(group["summary"])),
            "files": [i for i, file in enumerate(files) if any(hunk["group"] == group["id"] for hunk in file["hunks"])],
        }
        for group in notes["groups"]
    ]
    hovers, definitions, references, tokens = (
        diff_build.collect_hovers([{"files": files}], blobs) if use_lsp and lsp.is_available() else ([], [], [], {})
    )
    repo = re.sub(r"^.*github\.com[:/]|\.git$", "", git("remote", "get-url", "origin").strip())
    try:
        pull = diff_build.gh_api(f"repos/{repo}/pulls/{notes['pr']}") if notes.get("pr") else None
        # A warning, not a stop: a PR that was rebased after this checkout has no link to HEAD either
        if pull and (problem := pull_problem(pull, review["head"])):
            print(f'warning: {problem} Check "pr" in {notes_path}.', file=sys.stderr)
        elif pull and (problem := pull_ahead(pull, review["head"])):
            print(f"warning: {problem}", file=sys.stderr)
        github, snapshot = collect_github(repo, pull, files, blobs) if pull else (None, None)
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Could not read PR {notes['pr']} from GitHub: {(error.stderr or '').strip()}") from error

    hunk_keys = [f"{file['path']}|{hunk['id']}|{hunk['hash']}" for file in files for hunk in file["hunks"]]
    effort = notes.get("effort")
    data = {
        "v": 1,
        "repo": repo,
        "branch": branch_name(review["head"], pull),
        "base": base,
        "baseSha": review["mergeBase"],
        "headSha": review["head"],
        "dirty": review["dirty"],
        "buildId": hashlib.sha1("\n".join([review["head"], *hunk_keys]).encode()).hexdigest()[:16],
        "builtAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": text_of(notes["title"]),
        "heading": text_of(notes["heading"]),
        "intro": diff_build.sanitize_note(text_of(notes.get("intro"))),
        "ticket": text_of(notes.get("ticket")),
        "pr": notes.get("pr"),
        "effort": {"level": effort["level"], "minutes": effort["minutes"]} if effort else None,
        "diagram": text_of(notes.get("diagram")),
        "stats": {
            "files": len(files),
            "adds": sum(file["adds"] for file in files),
            "dels": sum(file["dels"] for file in files),
        },
        "groups": groups,
        "files": files,
        "blobs": blobs,
        "syntax": collect_syntax(files, blobs),
        "hovers": hovers,
        "definitions": definitions,
        "references": references,
        "tokens": tokens,
        "github": github,
    }
    # Escape every "<": a "</script" or a "<!--" followed by "<script" in the code would end the JSON block early
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    page = TEMPLATE.read_text(encoding="utf-8").replace("{{TITLE}}", html.escape(data["title"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page.replace("{{DATA}}", payload), encoding="utf-8")
    finding_count = write_finding_docs(out.parent / "findings", notes, files, data)
    if snapshot:
        (out.parent / "github.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")

    hunk_count = sum(len(file["hunks"]) for file in files)
    print(
        f"Wrote {out} ({out.stat().st_size // 1024} KB): {len(groups)} topics, {len(files)} files, "
        f"{hunk_count} hunks, {finding_count} findings (docs in {out.parent / 'findings'})"
    )
    if github:
        placed = sum(thread["line"] is not None for thread in github["threads"])
        status = github["pr"]
        print(
            f"GitHub PR {notes['pr']} ({status['state']}): {len(github['threads'])} threads ({placed} on a line, "
            f"{len(github['threads']) - placed} not on the current code), {len(github['conversation'])} conversation "
            f"comments, {len(status['reviewers'])} reviewers, {len(status['checks'])} checks "
            f"({ci_state(check_map(status['checks']))}). Snapshot: {out.parent / 'github.json'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the topic-by-topic review page.")
    parser.add_argument("mode", choices=["skeleton", "page", "check"])
    # No defaults here: notes.json keeps the options of the review, and BUILD_DEFAULTS fills the rest
    parser.add_argument("--base", help="the branch or commit to compare with (default origin/main)")
    parser.add_argument("--committed", action=argparse.BooleanOptionalAction, help="compare HEAD, not the working tree")
    parser.add_argument("--untracked", nargs="+", metavar="PATH", help="untracked files to show (after the user's yes)")
    parser.add_argument("--previous", type=Path, help="skeleton: copy the notes of an older notes.json")
    parser.add_argument("--notes", type=Path, help="page: the filled notes.json")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--snapshot", type=Path, help="check: the github.json that page wrote beside the page")
    parser.add_argument("--no-hovers", action="store_true")
    args = parser.parse_args()
    if args.mode == "check":
        if args.snapshot is None:
            parser.error("check needs --snapshot")
        check_github(args.snapshot)
        return
    if args.out is None:
        parser.error(f"{args.mode} needs --out")
    if args.mode == "page" and args.notes is None:
        parser.error("page needs --notes")
    out = args.out.resolve()
    notes = args.notes.resolve() if args.notes else None
    previous = args.previous.resolve() if args.previous else None
    # git ls-files and the working-tree reads use paths from the repository root
    os.chdir(git("rev-parse", "--show-toplevel").strip())
    given = {"base": args.base, "committed": args.committed, "untracked": args.untracked}
    if args.mode == "skeleton":
        write_skeleton(given, out, previous)
    else:
        assert notes is not None
        write_page(given, notes, out, not args.no_hovers)


if __name__ == "__main__":
    main()
