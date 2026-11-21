#!/usr/bin/env python3
"""Build the commit-by-commit diff review page.

build.py skeleton --base origin/main --out notes.json
build.py page --base origin/main --notes notes.json --out review.html
"""

import argparse
import hashlib
import html
import json
import re
import subprocess
import token
import tokenize
from io import StringIO
from keyword import iskeyword, issoftkeyword
from pathlib import Path
from typing import Any

import lsp

TEMPLATE = Path(__file__).with_name("template.html")
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
CONTEXT_LINES = 2
ALLOWED_NOTE_TAGS = ("code", "b")
HOVER_MARGIN = 15


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check).stdout


def sanitize_note(text: str) -> str:
    escaped = html.escape(text, quote=False)
    for tag in ALLOWED_NOTE_TAGS:
        escaped = escaped.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return escaped


def list_commits(base: str) -> list[str]:
    return git("rev-list", "--reverse", "--no-merges", f"{base}..HEAD").split()


def parse_diff(diff: str) -> dict[str, list[dict[str, Any]]]:
    files: dict[str, list[dict[str, Any]]] = {}
    current = ""
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            current = line.split(" b/", 1)[1]
            files[current] = []
        elif match := HUNK_HEADER.match(line):
            files[current].append({"oldStart": int(match[1]), "newStart": int(match[3]), "lines": []})
        elif current and files[current] and not line.startswith("\\"):
            kind = {"+": "add", "-": "del"}.get(line[:1], "ctx")
            files[current][-1]["lines"].append([kind, line[1:]])
    return files


def parse_hunk_ranges(diff: str) -> list[list[int]]:
    ranges = []
    for line in diff.splitlines():
        if match := HUNK_HEADER.match(line):
            ranges.append([int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1)])
    return ranges


def show_commit(sha: str) -> dict[str, list[dict[str, Any]]]:
    return parse_diff(git("show", "--format=", f"-U{CONTEXT_LINES}", sha))


def sort_files(paths: list[str]) -> list[str]:
    return sorted(paths, key=lambda path: (path.startswith("tests/"), path))


def write_skeleton(base: str, out: Path) -> None:
    commits = []
    for sha in list_commits(base):
        parsed = show_commit(sha)
        files = []
        for path in sort_files(list(parsed)):
            hunks = [
                {
                    "changed": [("+" if kind == "add" else "-") + text for kind, text in h["lines"] if kind != "ctx"][
                        :6
                    ],
                    "note": "",
                }
                for h in parsed[path]
            ]
            files.append({"path": path, "hunks": hunks})
        subject = git("log", "-1", "--format=%s", sha).strip()
        commits.append({"sha": sha, "subject": subject, "summary": "", "files": files})
    skeleton = {"title": "", "heading": "", "intro": "", "ticket": "", "pr": None, "commits": commits}
    out.write_text(json.dumps(skeleton, indent=2))


def collect_commentable_lines(base: str, path: str) -> dict[str, list[Any]]:
    right: list[list[int]] = []
    left: list[int] = []
    for hunk in parse_diff(git("diff", f"{base}...HEAD", "--", path)).get(path, []):
        old_no, new_no = hunk["oldStart"], hunk["newStart"]
        first_new = new_no
        for kind, _ in hunk["lines"]:
            if kind == "del":
                left.append(old_no)
                old_no += 1
            elif kind == "add":
                new_no += 1
            else:
                old_no += 1
                new_no += 1
        right.append([first_new, new_no - 1])
    return {"right": right, "left": left}


def store_blob(blobs: dict[str, list[str]], rev: str, path: str) -> str | None:
    object_id = git("rev-parse", "--verify", "-q", f"{rev}:{path}", check=False).strip()
    if not object_id:
        return None
    if object_id not in blobs:
        lines = git("show", f"{rev}:{path}").split("\n")
        blobs[object_id] = lines[:-1] if lines and lines[-1] == "" else lines
    return object_id


def count_changes(sha: str) -> tuple[int, int]:
    adds = dels = 0
    for row in git("show", "--format=", "--numstat", sha).splitlines():
        added, deleted, _ = row.split("\t")
        adds += int(added) if added != "-" else 0
        dels += int(deleted) if deleted != "-" else 0
    return adds, dels


def highlight_python(lines: list[str]) -> dict[int, list[list[Any]]]:
    spans: dict[int, list[list[Any]]] = {}

    def add(start: tuple[int, int], end: tuple[int, int], kind: str) -> None:
        for row in range(start[0], end[0] + 1):
            first = start[1] if row == start[0] else 0
            last = end[1] if row == end[0] else len(lines[row - 1])
            if last > first:
                spans.setdefault(row, []).append([first, last - first, kind])

    string_types = {token.STRING, getattr(token, "FSTRING_START", -1), getattr(token, "FSTRING_MIDDLE", -1)}
    string_types |= {getattr(token, "FSTRING_END", -1)}
    previous = None
    try:
        for tok in tokenize.generate_tokens(StringIO("\n".join(lines) + "\n").readline):
            if tok.type == token.NAME and (iskeyword(tok.string) or (issoftkeyword(tok.string) and tok.start[1] == 0)):
                add(tok.start, tok.end, "k")
            elif (
                tok.type == token.NAME and previous is not None and previous.string == "@" and previous.type == token.OP
            ):
                add(previous.start, tok.end, "d")
            elif tok.type in string_types:
                add(tok.start, tok.end, "s")
            elif tok.type == token.NUMBER:
                add(tok.start, tok.end, "n")
            elif tok.type == token.COMMENT:
                add(tok.start, tok.end, "c")
            previous = tok
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return spans


def collect_hovers(
    commits: list[dict[str, Any]], blobs: dict[str, list[str]]
) -> tuple[list[str], list[dict[str, Any]], list[Any], dict[str, Any]]:
    wanted: dict[str, set[int]] = {}
    blob_paths: dict[str, str] = {}
    for commit in commits:
        for file in commit["files"]:
            if not file["path"].endswith(".py"):
                continue
            for hunk in file["hunks"]:
                old_no, new_no = hunk["oldStart"], hunk["newStart"]
                for kind, _ in hunk["lines"]:
                    if kind != "add" and file["oldBlob"]:
                        wanted.setdefault(file["oldBlob"], set()).update(
                            range(old_no - HOVER_MARGIN, old_no + HOVER_MARGIN + 1)
                        )
                        blob_paths[file["oldBlob"]] = file["path"]
                    if kind != "del" and file["newBlob"]:
                        wanted.setdefault(file["newBlob"], set()).update(
                            range(new_no - HOVER_MARGIN, new_no + HOVER_MARGIN + 1)
                        )
                        blob_paths[file["newBlob"]] = file["path"]
                    old_no += kind != "add"
                    new_no += kind != "del"
    collector = lsp.Collector(Path(git("rev-parse", "--show-toplevel").strip()))
    try:
        tokens = {
            blob_id: collector.collect(blob_paths[blob_id], blob_id, "\n".join(blobs[blob_id]) + "\n", lines)
            for blob_id, lines in wanted.items()
        }
    finally:
        collector.close()
    return collector.hovers, collector.definitions, collector.references, tokens


def gh_api(*args: str) -> Any:
    return json.loads(subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=True).stdout)


def is_bot(user: dict[str, Any]) -> bool:
    return bool(user.get("type") == "Bot" or user["login"].endswith("[bot]"))


def find_commit_index(
    sha: str, commits: list[dict[str, Any]], repo: str, subjects: dict[str, str | None]
) -> int | None:
    """Index of the branch commit matching `sha`, following a rebase by commit subject."""
    for i, commit in enumerate(commits):
        if commit["sha"] == sha:
            return i
    if sha not in subjects:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{sha}", "--jq", ".commit.message"], capture_output=True, text=True
        )
        subjects[sha] = result.stdout.split("\n", 1)[0].strip() if result.returncode == 0 else None
    matches = [i for i, commit in enumerate(commits) if subjects[sha] and commit["subject"] == subjects[sha]]
    return matches[-1] if matches else None


def find_thread_anchor(
    path: str, hunk_line: str, original_line: int | None, commits: list[dict[str, Any]], start: int
) -> dict[str, Any] | None:
    """The commit line a review comment refers to, found by its text so it survives rebases and later commits."""
    wanted = {"+": "add", "-": "del"}.get(hunk_line[:1], "ctx")
    text = hunk_line[1:]
    order = [*range(start, -1, -1), *range(start + 1, len(commits))]
    for kinds in ((wanted,), ("add", "ctx", "del")):
        for ci in order:
            file = next((f for f in commits[ci]["files"] if f["path"] == path), None)
            if file is None:
                continue
            candidates = []
            for hunk in file["hunks"]:
                old_no, new_no = hunk["oldStart"], hunk["newStart"]
                for kind, line_text in hunk["lines"]:
                    if kind in kinds and line_text == text:
                        side, line = ("LEFT", old_no) if kind == "del" else ("RIGHT", new_no)
                        candidates.append((abs(line - (original_line or line)), side, line))
                    old_no += kind != "add"
                    new_no += kind != "del"
            if candidates:
                _, side, line = min(candidates)
                return {"commitSha": commits[ci]["sha"], "path": path, "side": side, "line": line}
    return None


def collect_pr_discussion(repo: str, pr: int, commits: list[dict[str, Any]]) -> dict[str, Any]:
    owner, name = repo.split("/")
    pull = gh_api(f"repos/{repo}/pulls/{pr}")
    comments = [c for page in gh_api("--paginate", "--slurp", f"repos/{repo}/pulls/{pr}/comments") for c in page]
    reviews = [r for page in gh_api("--paginate", "--slurp", f"repos/{repo}/pulls/{pr}/reviews") for r in page]
    issue_comments = [c for page in gh_api("--paginate", "--slurp", f"repos/{repo}/issues/{pr}/comments") for c in page]
    query = (
        "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n)"
        "{reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{databaseId}}}}}}}"
    )
    threads_data = gh_api("graphql", "-f", f"query={query}", "-f", f"o={owner}", "-f", f"r={name}", "-F", f"n={pr}")
    resolved = {
        node["comments"]["nodes"][0]["databaseId"]: node["isResolved"]
        for node in threads_data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        if node["comments"]["nodes"]
    }

    subjects: dict[str, str | None] = {}
    replies: dict[int, list[dict[str, Any]]] = {}
    for comment in comments:
        if comment.get("in_reply_to_id"):
            replies.setdefault(comment["in_reply_to_id"], []).append(comment)
    threads: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    for root in (c for c in comments if not c.get("in_reply_to_id")):
        hunk_line = root["diff_hunk"].splitlines()[-1] if root.get("diff_hunk") else " "
        start = find_commit_index(root["original_commit_id"], commits, repo, subjects)
        anchor = find_thread_anchor(
            root["path"], hunk_line, root.get("original_line"), commits, len(commits) - 1 if start is None else start
        )
        thread = {
            "path": root["path"],
            "lineText": hunk_line[1:],
            "url": root["html_url"],
            "resolved": resolved.get(root["id"], False),
            "outdated": root.get("line") is None,
            "comments": [
                {"author": c["user"]["login"], "bot": is_bot(c["user"]), "at": c["created_at"], "body": c["body"]}
                for c in [root, *sorted(replies.get(root["id"], []), key=lambda c: c["created_at"])]
            ],
        }
        (threads if anchor else unplaced).append(thread | (anchor or {}))

    conversation = [
        {
            "author": r["user"]["login"],
            "bot": is_bot(r["user"]),
            "at": r["submitted_at"],
            "body": r["body"],
            "url": r["html_url"],
        }
        for r in reviews
        if r["submitted_at"] and (r["body"] or "").strip()
    ] + [
        {"author": c["user"]["login"], "bot": False, "at": c["created_at"], "body": c["body"], "url": c["html_url"]}
        for c in issue_comments
        if not is_bot(c["user"])
    ]

    # One mark per reviewer and commit; the PR author's reviews are only thread replies
    rounds: dict[tuple[str, str | None, bool], dict[str, Any]] = {}
    # A pending review is the user's own unsubmitted draft: no date, marked on its own
    for review in sorted(reviews, key=lambda r: r["submitted_at"] or "~"):
        if is_bot(review["user"]) or review["user"]["login"] == pull["user"]["login"] or not review.get("commit_id"):
            continue
        index = find_commit_index(review["commit_id"], commits, repo, subjects)
        commit_sha = commits[index]["sha"] if index is not None else None
        rounds[(review["user"]["login"], commit_sha, review["state"] == "PENDING")] = {
            "login": review["user"]["login"],
            "at": review["submitted_at"],
            "state": review["state"].lower().replace("_", " "),
            "sha": review["commit_id"],
            "commitSha": commit_sha,
        }
    latest_at: dict[str, str] = {}
    for mark in rounds.values():
        if mark["at"]:
            latest_at[mark["login"]] = max(latest_at.get(mark["login"], ""), mark["at"])
    review_marks = [
        mark | {"latest": bool(mark["at"]) and mark["at"] == latest_at[mark["login"]]} for mark in rounds.values()
    ]

    return {
        "threads": threads,
        "unplacedThreads": unplaced,
        "conversation": sorted(conversation, key=lambda c: c["at"]),
        "reviewMarks": review_marks,
    }


def write_page(base: str, notes_path: Path, out: Path, use_lsp: bool) -> None:
    notes = json.loads(notes_path.read_text())
    notes_by_sha = {commit["sha"]: commit for commit in notes["commits"]}
    shas = list_commits(base)
    merge_base = git("merge-base", base, "HEAD").strip()
    if list(notes_by_sha) != shas:
        raise SystemExit("Notes do not match the commits on the branch: run skeleton again.")

    blobs: dict[str, list[str]] = {}
    commentable: dict[str, dict[str, list[Any]]] = {}
    commits: list[dict[str, Any]] = []
    for index, sha in enumerate(shas, start=1):
        commit_notes = notes_by_sha[sha]
        notes_by_path = {f["path"]: f["hunks"] for f in commit_notes["files"]}
        parsed = show_commit(sha)
        files = []
        for path in sort_files(list(parsed)):
            hunks = parsed[path]
            hunk_notes = notes_by_path.get(path, [])
            if len(hunk_notes) != len(hunks) or any(not n["note"].strip() for n in hunk_notes):
                raise SystemExit(f"Missing notes for {sha[:9]} {path}")
            for hunk, hunk_note in zip(hunks, hunk_notes):
                hunk["note"] = sanitize_note(hunk_note["note"])
            commentable.setdefault(path, collect_commentable_lines(base, path))
            files.append(
                {
                    "path": path,
                    "kind": "test" if path.startswith("tests/") else "code",
                    "hunks": hunks,
                    "newBlob": store_blob(blobs, sha, path),
                    "oldBlob": store_blob(blobs, f"{sha}^", path),
                    "toHead": parse_hunk_ranges(git("diff", "-U0", sha, "HEAD", "--", path)),
                    "parentToBase": parse_hunk_ranges(git("diff", "-U0", f"{sha}^", merge_base, "--", path)),
                    "key": hashlib.sha1(json.dumps(hunks).encode()).hexdigest()[:16],
                }
            )
        adds, dels = count_changes(sha)
        commits.append(
            {
                "index": index,
                "sha": sha,
                "short": sha[:9],
                "subject": git("log", "-1", "--format=%s", sha).strip(),
                "body": git("log", "-1", "--format=%b", sha).split("\nCo-Authored-By")[0].strip(),
                "summary": sanitize_note(commit_notes["summary"]),
                "adds": adds,
                "dels": dels,
                "files": files,
            }
        )

    hovers, definitions, references, tokens_by_blob = (
        collect_hovers(commits, blobs) if use_lsp and lsp.is_available() else ([], [], [], {})
    )
    python_blobs = {
        f[side] for c in commits for f in c["files"] if f["path"].endswith(".py") for side in ("newBlob", "oldBlob")
    }
    syntax = {blob_id: highlight_python(blobs[blob_id]) for blob_id in python_blobs if blob_id}

    repo = re.sub(r"^.*github\.com[:/]|\.git$", "", git("remote", "get-url", "origin").strip())
    discussion = (
        collect_pr_discussion(repo, notes["pr"], commits)
        if notes["pr"]
        else {"threads": [], "unplacedThreads": [], "conversation": [], "reviewMarks": []}
    )
    data = {
        "repo": repo,
        "branch": git("rev-parse", "--abbrev-ref", "HEAD").strip(),
        "headSha": git("rev-parse", "HEAD").strip(),
        "heading": notes["heading"],
        "intro": notes["intro"],
        "ticket": notes["ticket"],
        "pr": notes["pr"],
        "commits": commits,
        "blobs": blobs,
        "commentable": commentable,
        "hovers": hovers,
        "definitions": definitions,
        "tokens": tokens_by_blob,
        "references": references,
        "syntax": syntax,
        **discussion,
    }
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    page = TEMPLATE.read_text().replace("{{TITLE}}", html.escape(notes["title"])).replace("{{DATA}}", payload)
    out.write_text(page)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["skeleton", "page"])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-hovers", action="store_true")
    args = parser.parse_args()
    if args.mode == "skeleton":
        write_skeleton(args.base, args.out)
    else:
        write_page(args.base, args.notes, args.out, not args.no_hovers)


if __name__ == "__main__":
    main()
