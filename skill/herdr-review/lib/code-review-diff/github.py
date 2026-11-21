#!/usr/bin/env python3
"""Post one batch of review-page comments to GitHub as one PR review.

github.py plan --pr 11431 --docs <dir> --batch <id> --out plan.json --owner <user id> [--names names.json]
github.py post --plan plan.json --out result.json
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DIFF_DIR = Path(__file__).resolve().parent.parent / "create-diff"
sys.path.insert(0, str(DIFF_DIR))  # create-diff/build.py does "import lsp" at the top
_spec = importlib.util.spec_from_file_location("create_diff_build", DIFF_DIR / "build.py")
assert _spec is not None and _spec.loader is not None
diff_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diff_build)

git = diff_build.git
gh_api = diff_build.gh_api
HUNK_HEADER: re.Pattern[str] = diff_build.HUNK_HEADER

PLAN_MAX_AGE = timedelta(hours=1)
REVIEW_EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")
OPEN_BATCH_STATUSES = ("pending", "working")
MOVE_WINDOW = 200  # the page uses the same window to find a moved line
LINES_ABOVE = 3  # the page's snippet keeps up to 3 lines above the comment
BODY_LIMIT = 65_536  # GitHub refuses a longer comment body
EXCERPT_LENGTH = 80
FENCE_LANGS = {".py": "python"}
SUGGESTION_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*suggestion[ \t]*$", re.MULTILINE)
MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>])")
THREADS_QUERY = (
    "query($o:String!,$r:String!,$n:Int!,$endCursor:String){repository(owner:$o,name:$r){pullRequest(number:$n)"
    "{reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor}"
    "nodes{id comments(first:1){nodes{databaseId}}}}}}}"
)
THREAD_MUTATIONS = {
    "resolve": "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}",
    "unresolve": "mutation($id:ID!){unresolveReviewThread(input:{threadId:$id}){thread{isResolved}}}",
}

Doc = dict[str, Any]
Lines = dict[int, tuple[int, str]]


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    head: str
    files: dict[str, dict[str, Lines] | None]  # None: GitHub sends no patch (binary or too big)
    threads: dict[int, str]  # first comment id -> review thread node id
    opened_by_viewer: bool


def repo_name() -> str:
    return re.sub(r"^.*github\.com[:/]|\.git$", "", git("remote", "get-url", "origin").strip())


def unwrap_doc(raw: Doc, file_id: str) -> tuple[str, Doc]:
    """Id and body of one saved db document: the body itself, or a wrapper with the body under "data"."""
    if isinstance(raw.get("data"), dict) and "v" not in raw:
        return str(raw.get("id") or file_id).rsplit("/", 1)[-1], raw["data"]
    return file_id, raw


def load_collection(docs_dir: Path, collection: str) -> dict[str, Doc]:
    return dict(
        unwrap_doc(json.loads(path.read_text()), path.stem) for path in sorted((docs_dir / collection).glob("*.json"))
    )


def parse_patch(patch: str) -> dict[str, Lines]:
    """Lines of a GitHub file patch that a review comment can use: side -> line number -> (hunk index, text)."""
    sides: dict[str, Lines] = {"LEFT": {}, "RIGHT": {}}
    hunk = -1
    old_no = new_no = old_left = new_left = 0
    for row in patch.split("\n"):
        if match := HUNK_HEADER.match(row):
            hunk += 1
            old_no, new_no = int(match[1]), int(match[3])
            old_left, new_left = int(match[2] or 1), int(match[4] or 1)
            continue
        if row.startswith("\\") or (old_left <= 0 and new_left <= 0):
            continue
        text = row[1:].removesuffix("\r")
        if row[:1] != "+":
            sides["LEFT"][old_no] = (hunk, text)
            old_no += 1
            old_left -= 1
        if row[:1] != "-":
            sides["RIGHT"][new_no] = (hunk, text)
            new_no += 1
            new_left -= 1
    return sides


def find_line(lines: Lines, wanted: int, text: str | None, above: list[str] | None = None) -> int | None:
    if wanted in lines and (text is None or lines[wanted][1] == text):
        return wanted
    if text is None:
        return None
    # The line moved. Its text alone is not enough ("return None" can be on many lines): the lines just above it
    # must also be as the page saw them.
    above = above or []
    found = [
        no
        for no, (_, line_text) in lines.items()
        if line_text == text
        and abs(no - wanted) <= MOVE_WINDOW
        and all(lines.get(no - len(above) + offset, (0, None))[1] == row for offset, row in enumerate(above))
    ]
    return min(found, key=lambda no: (abs(no - wanted), no), default=None)


def lines_above(known: dict[int, str], line: int) -> list[str]:
    return [known[no] for no in range(line - LINES_ABOVE, line) if no in known]


def anchored_texts(doc: Doc) -> dict[int, str]:
    """Text of the comment's lines as the page saw them, by line number."""
    snippet = doc.get("snippet") or {}
    known = (
        {int(snippet["start"]) + offset: text for offset, text in enumerate(snippet.get("lines") or [])}
        if "start" in snippet
        else {}
    )
    if isinstance(doc.get("lineText"), str):
        known[doc["anchor"]["line"]] = doc["lineText"]
    return known


def map_line_anchor(anchor: Doc, known: dict[int, str], lines: Lines) -> Doc | None:
    """The PR diff place of a line anchor, or None when its line is not in the diff.

    `exact` is true when the PR shows the same lines with the same text, so a suggestion is safe to apply.
    """
    line = find_line(lines, anchor["line"], known.get(anchor["line"]), lines_above(known, anchor["line"]))
    if line is None:
        return None
    first = anchor.get("startLine") or anchor["line"]
    start = None
    if first < anchor["line"]:
        start = find_line(lines, first + line - anchor["line"], known.get(first), lines_above(known, first))
        if start is not None and not (start < line and lines[start][0] == lines[line][0]):
            start = None
    mapped_first = start or line
    exact = line - mapped_first == anchor["line"] - first and all(
        lines.get(mapped_first + offset, (0, None))[1] == known[first + offset]
        for offset in range(line - mapped_first + 1)
        if first + offset in known
    )
    return {"line": line, "startLine": start, "exact": exact}


def place(side: str, line: int, start: int | None = None) -> str:
    mark = side[0]
    return f"{mark}{start}-{mark}{line}" if start else f"{mark}{line}"


def lines_label(anchor: Doc) -> str:
    first = anchor.get("startLine") or anchor["line"]
    numbers = f"lines {first}-{anchor['line']}" if first < anchor["line"] else f"line {anchor['line']}"
    return f"old {numbers}" if anchor["side"] == "LEFT" else numbers


def fence_lang(path: str) -> str:
    suffix = Path(path).suffix
    return FENCE_LANGS.get(suffix, suffix.removeprefix("."))


def code_block(text: str, info: str) -> str:
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    return f"{fence}{info}\n{text.removesuffix('\n')}\n{fence}"


def quote(rows: list[str]) -> str:
    return "\n".join(f"> {row}" if row else ">" for row in rows) + "\n\n"


def quote_lines(label: str, texts: list[str], lang: str) -> str:
    return quote([label, *(["", *code_block("\n".join(texts), lang).split("\n")] if texts else [])])


def excerpt(text: str, length: int = EXCERPT_LENGTH) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= length else flat[: length - 1] + "…"


def escape_markdown(text: str) -> str:
    return MARKDOWN_SPECIAL.sub(r"\\\1", text)


def author_prefix(author: str, owner: str | None, names: dict[str, str]) -> str:
    # Everything goes out from the owner's GitHub account, so other people's words need their name
    if author in ("claude", owner):
        return ""
    return f"**{escape_markdown(names.get(author, author))}:** "


def plain_suggestions(body: str, lang: str) -> str:
    # GitHub applies a suggestion to the lines the comment sits on, which are not the lines it was written for
    return SUGGESTION_FENCE.sub(rf"\g<1>\g<2>{lang}", body)


def text_body(doc: Doc, owner: str | None, names: dict[str, str], lang: str, exact_right: bool) -> str:
    body = str(doc.get("body") or "").strip()
    if not exact_right:
        body = plain_suggestions(body, lang)
    if doc.get("kind") != "finding":
        return author_prefix(str(doc.get("author")), owner, names) + body
    severity = str(doc.get("severity") or "").capitalize()
    label = f"Claude review · {severity}" if severity else "Claude review"
    text = f"**{label}:** {doc.get('title') or ''}".rstrip() + f"\n\n{body}"
    if suggestion := doc.get("suggestion"):
        block = code_block(suggestion, "suggestion" if exact_right else lang)
        text += f"\n\n{block}" if exact_right else f"\n\nSuggested change:\n\n{block}"
    return text.strip()


def thread_body(
    root: Doc, replies: list[Doc], owner: str | None, names: dict[str, str], lang: str, exact_right: bool
) -> str:
    # A review cannot reply to its own comments, so replies written in the page go under their first comment
    parts = [text_body(doc, owner, names, lang, exact_right) for doc in [root, *replies]]
    return "\n\n---\n\n".join(parts)


def single(endpoint: str, payload: Doc, where: str) -> Doc:
    return {"to": "single", "endpoint": endpoint, "input": payload, "where": where}


def within_limit(route: Doc) -> Doc:
    """A skip in place of a comment that GitHub would refuse. One refused review comment stops the whole review."""
    body = str((route.get("comment") or route.get("input") or route).get("body") or "")
    if len(body) <= BODY_LIMIT:
        return route
    return {"to": "skip", "reason": f"the text has {len(body)} characters, and GitHub takes at most {BODY_LIMIT}"}


def route_root(doc: Doc, replies: list[Doc], pull: PullRequest, names: dict[str, str], owner: str | None) -> Doc:
    anchor = doc.get("anchor") or {}
    kind, path = anchor.get("type"), str(anchor.get("path") or "")
    lang = fence_lang(path)
    issues = f"repos/{pull.repo}/issues/{pull.number}/comments"
    pulls = f"repos/{pull.repo}/pulls/{pull.number}/comments"

    def body(exact_right: bool, before: str = "") -> str:
        return before + thread_body(doc, replies, owner, names, lang, exact_right)

    def file_comment(text: str) -> Doc:
        return {"commit_id": pull.head, "path": path, "subject_type": "file", "body": text}

    if kind == "general":
        return single(issues, {"body": body(False)}, "PR conversation comment")
    if kind == "file" and path in pull.files:
        return single(pulls, file_comment(body(False)), f"file comment on {path}")
    if kind == "file" and path:
        before = quote([f"`{path}` (this file is not in the PR):"])
        return single(issues, {"body": body(False, before)}, f"PR conversation comment ({path} is not in the PR)")
    line, start = anchor.get("line"), anchor.get("startLine")
    if kind != "line" or not path or anchor.get("side") not in ("LEFT", "RIGHT") or not isinstance(line, int):
        return {"to": "skip", "reason": "the comment has no valid anchor"}
    anchor = anchor | {"startLine": start if isinstance(start, int) and 0 < start < line else None}

    known = anchored_texts(doc)
    first = anchor["startLine"] or line
    texts = [known[no] for no in range(first, anchor["line"] + 1) if no in known]
    label = lines_label(anchor)
    if path not in pull.files:
        before = quote_lines(f"`{path}`, {label} (this file is not in the PR):", texts, lang)
        return single(issues, {"body": body(False, before)}, f"PR conversation comment ({path} is not in the PR)")
    sides = pull.files[path]
    if sides is None or (mapped := map_line_anchor(anchor, known, sides[anchor["side"]])) is None:
        before = quote_lines(f"{label.capitalize()} (not in the PR diff):", texts, lang)
        where = f"file comment on {path}, quotes {place(anchor['side'], anchor['line'], anchor.get('startLine'))}"
        return single(pulls, file_comment(body(False, before)), f"{where} (not in the PR diff)")

    exact_right = mapped["exact"] and anchor["side"] == "RIGHT"
    comment: Doc = {"path": path, "line": mapped["line"], "side": anchor["side"]}
    if mapped["startLine"]:
        comment |= {"start_line": mapped["startLine"], "start_side": anchor["side"]}
    comment["body"] = body(exact_right)
    old_start = first if first < anchor["line"] else None
    where = f"review comment on {path} {place(anchor['side'], mapped['line'], mapped['startLine'])}"
    if (mapped["startLine"], mapped["line"]) != (old_start, anchor["line"]):
        where += f" (was {place(anchor['side'], anchor['line'], old_start)})"
    if doc.get("suggestion") and not exact_right:
        where += ", suggestion as plain code"
    return {"to": "review", "comment": comment, "where": where}


def route_reply(
    doc: Doc,
    comments: dict[str, Doc],
    batch_roots: dict[str, bool],
    pull: PullRequest,
    names: dict[str, str],
    owner: str | None,
) -> Doc:
    reply_to = str(doc["replyTo"])
    body = author_prefix(str(doc.get("author")), owner, names) + str(doc.get("body") or "").strip()
    if reply_to.startswith("gh:"):
        gh_id = reply_to.removeprefix("gh:")
        if gh_id.isdigit() and int(gh_id) in pull.threads:
            return {"to": "reply", "in_reply_to": int(gh_id), "body": body, "where": f"reply to GitHub thread {gh_id}"}
        return {"to": "skip", "reason": f"PR #{pull.number} has no GitHub thread {gh_id}"}
    root = comments.get(reply_to) or {}
    github_id = str(root.get("githubId") or "")
    # A root on a line is in the PR conversation too when its file was not in the PR: its link tells where it went
    general = (root.get("anchor") or {}).get("type") == "general"
    if github_id.isdigit() and (general or "#issuecomment-" in str(root.get("githubUrl"))):
        # PR conversation comments have no replies on GitHub: quote the first comment instead
        endpoint = f"repos/{pull.repo}/issues/{pull.number}/comments"
        before = quote([excerpt(str(root.get("body") or ""), 120)])
        return single(endpoint, {"body": before + body}, f"PR conversation comment (reply to {reply_to})")
    if github_id.isdigit():
        where = f"reply to GitHub comment {github_id} (thread {reply_to})"
        return {"to": "reply", "in_reply_to": int(github_id), "body": body, "where": where}
    if batch_roots.get(reply_to):
        return {"to": "attached", "root": reply_to, "where": f"added under {reply_to} in the same GitHub comment"}
    if reply_to in batch_roots:
        return {"to": "skip", "reason": f"its first comment {reply_to} is skipped"}
    return {"to": "skip", "reason": "the thread is not on GitHub; include its first comment"}


def build_plan(
    batch_id: str,
    batch: Doc,
    comments: dict[str, Doc],
    pull: PullRequest,
    names: dict[str, str],
    owner: str | None,
    now: datetime,
) -> tuple[Doc, list[str]]:
    """The GitHub calls that post one batch, and a plain summary of where each item goes."""
    ids = [str(doc_id) for doc_id in dict.fromkeys(batch.get("ids") or [])]
    warnings: list[str] = []
    event = batch.get("event") or "COMMENT"
    if event != "COMMENT" and pull.opened_by_viewer:
        warnings.append(f"You opened this PR and GitHub refuses {event} from the PR author: the review uses COMMENT.")
        event = "COMMENT"
    review_body = str(batch.get("body") or "").strip()
    if review_body:
        review_body = author_prefix(str(batch.get("author")), owner, names) + review_body
        if batch.get("author") not in ("claude", owner, *names):
            warnings.append(f"No display name for {batch.get('author')}: GitHub shows the id. Pass --names.")
    plan: Doc = {
        "createdAt": now.isoformat(timespec="seconds"),
        "repo": pull.repo,
        "pr": pull.number,
        "head": pull.head,
        "batch": batch_id,
        "review": {"commit_id": pull.head, "event": event, "body": review_body, "comments": []},
        "reviewDocs": [],
        "singles": [],
        "replies": [],
        "attached": {},
        "resolve": [],
        "unresolve": [],
        "skipped": [],
    }

    routes: dict[str, Doc] = {}
    for doc_id in ids:
        if (doc := comments.get(doc_id)) is None:
            routes[doc_id] = {"to": "skip", "reason": "the comment is not in the docs folder"}
        elif doc.get("githubId") or doc.get("githubUrl") or doc.get("status") == "posted":
            link = doc.get("githubUrl") or doc.get("githubId") or "status posted"
            routes[doc_id] = {"to": "skip", "reason": f"already on GitHub ({link})"}
    roots = [doc_id for doc_id in ids if doc_id not in routes and not comments[doc_id].get("replyTo")]
    attachments: dict[str, list[Doc]] = {doc_id: [] for doc_id in roots}
    for doc_id in ids:
        if doc_id not in routes and (reply_to := comments[doc_id].get("replyTo")) in attachments:
            attachments[reply_to].append(comments[doc_id])
    for doc_id in roots:
        replies = sorted(attachments.get(doc_id, []), key=lambda reply: str(reply.get("createdAt") or ""))
        routes[doc_id] = within_limit(route_root(comments[doc_id], replies, pull, names, owner))
    batch_roots = {doc_id: routes[doc_id]["to"] != "skip" for doc_id in roots}
    for doc_id in ids:
        if doc_id not in routes:
            routes[doc_id] = within_limit(route_reply(comments[doc_id], comments, batch_roots, pull, names, owner))
    if (author := str(batch.get("author"))) not in ("claude", owner):
        name = names.get(author, author)
        warnings.append(f"{name} sent this batch. It posts from the owner's GitHub account, with {name}'s name first.")

    summary = [f"Plan for PR #{pull.number} ({pull.repo}) at head {pull.head[:9]}, batch {batch_id}, {len(ids)} items:"]
    for doc_id in ids:
        route = routes[doc_id]
        match route["to"]:
            case "review":
                plan["review"]["comments"].append(route["comment"])
                plan["reviewDocs"].append(doc_id)
            case "single":
                plan["singles"].append({"doc": doc_id, "endpoint": route["endpoint"], "input": route["input"]})
            case "reply":
                plan["replies"].append({"doc": doc_id, "in_reply_to": route["in_reply_to"], "body": route["body"]})
            case "attached":
                plan["attached"][doc_id] = route["root"]
            case _:
                plan["skipped"].append({"doc": doc_id, "reason": route["reason"]})
        doc = comments.get(doc_id) or {}
        author = str(doc.get("author") or "?")
        who = "Claude" if author == "claude" else names.get(author, author)
        text = str((doc.get("title") if doc.get("kind") == "finding" else doc.get("body")) or "")
        summary.append(f"- {doc_id} -> {route.get('where') or 'skipped: ' + route['reason']}")
        summary.append(f'    {who}: "{excerpt(text)}"')
        if route["to"] != "skip" and author not in ("claude", owner, *names):
            warnings.append(f"No display name for {author}: GitHub shows the id. Pass --names.")

    for key, field in (("resolve", "resolveGithub"), ("unresolve", "unresolveGithub")):
        for value in batch.get(field) or []:
            gh_id = str(value).removeprefix("gh:")
            if gh_id.isdigit() and (node_id := pull.threads.get(int(gh_id))):
                plan[key].append({"ghId": gh_id, "nodeId": node_id})
                summary.append(f"- {key} GitHub thread {gh_id}")
            else:
                reason = f"PR #{pull.number} has no GitHub thread {gh_id}"
                plan["skipped"].append({"doc": f"gh:{gh_id}", "reason": reason})
                summary.append(f"- gh:{gh_id} -> skipped: {reason}")

    review = plan["review"]
    summary.insert(
        1,
        "No review: it has no comments, no summary and event COMMENT."
        if review_is_empty(review)
        else f'Review: {review["event"]}, {len(review["comments"])} line comments, summary "{excerpt(review_body)}".',
    )
    who = names.get(owner, owner) if owner else "nobody"
    summary.insert(2, f"It posts from the GitHub account of {who}. Other people's comments start with their name.")
    summary += [f"Warning: {warning}" for warning in dict.fromkeys(warnings)]
    return plan, summary


def review_is_empty(review: Doc) -> bool:
    return review["event"] == "COMMENT" and not review["body"] and not review["comments"]


def plan_posts(plan: Doc) -> bool:
    """True when post sends at least one POST: the review, a single comment, a reply or a thread (un)resolve."""
    calls = ("singles", "replies", "resolve", "unresolve")
    return not review_is_empty(plan["review"]) or any(plan[key] for key in calls)


def batch_problem(batch: Doc) -> str | None:
    if batch.get("target") != "github":
        return f"its target is {batch.get('target')!r}, not 'github'."
    if batch.get("status") not in OPEN_BATCH_STATUSES:
        return f"its status is {batch.get('status')!r}. Only a pending or working batch can go to GitHub."
    if (batch.get("event") or "COMMENT") not in REVIEW_EVENTS:
        return f"its review event {batch.get('event')!r} is not one of {', '.join(REVIEW_EVENTS)}."
    if (length := len(str(batch.get("body") or ""))) > BODY_LIMIT:
        return f"its review summary has {length} characters, and GitHub takes at most {BODY_LIMIT}."
    return None


def plan_problem(plan: Doc, head_now: str, now: datetime) -> str | None:
    age = now - datetime.fromisoformat(plan["createdAt"])
    if age > PLAN_MAX_AGE:
        return f"The plan is {int(age.total_seconds() // 60)} minutes old (the limit is 60)."
    if plan["head"] != head_now:
        return f"The PR head is now {head_now[:9]}, not {plan['head'][:9]}."
    return None


def comment_places(comment: Doc) -> set[tuple[str, int]]:
    """(side, line) of a fetched review comment. The review comments endpoint sends no `line`: read the diff hunk."""
    if comment.get("line") and comment.get("side"):
        return {(comment["side"], comment["line"])}
    places: set[tuple[str, int]] = set()
    old_no = new_no = 0
    for row in str(comment.get("diff_hunk") or "").rstrip("\n").split("\n"):
        if match := HUNK_HEADER.match(row):
            old_no, new_no = int(match[1]), int(match[3])
        elif not row.startswith("\\"):
            places = set()
            if row[:1] != "+":
                places.add(("LEFT", old_no))
                old_no += 1
            if row[:1] != "-":
                places.add(("RIGHT", new_no))
                new_no += 1
    return places


def match_review_comments(
    sent: list[Doc], docs: list[str], fetched: list[Doc], review_url: str | None
) -> dict[str, Doc]:
    """GitHub id and link of each review comment, found by path and line in the order they were sent."""
    unused = sorted(fetched, key=lambda comment: int(comment["id"]))
    posted: dict[str, Doc] = {}
    for doc_id, comment in zip(docs, sent):
        found = next(
            (
                candidate
                for candidate in unused
                if candidate.get("path") == comment["path"]
                and (comment["side"], comment["line"]) in comment_places(candidate)
            ),
            None,
        )
        if found is None:
            posted[doc_id] = {"id": None, "url": review_url}
            continue
        unused.remove(found)
        posted[doc_id] = {"id": found["id"], "url": found["html_url"]}
    return posted


def gh_error(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        text = " ".join(part.strip() for part in (error.stderr, error.stdout) if isinstance(part, str) and part.strip())
        return text[:500] or str(error)
    return str(error)[:500]


def gh_post(endpoint: str, payload: Doc) -> Any:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle)
    try:
        return gh_api(endpoint, "--method", "POST", "--input", handle.name)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def post_plan(plan: Doc, save: Callable[[Doc], None] = lambda result: None) -> Doc:
    """Post the plan. A failed review stops everything; after it, one failed item does not stop the others.

    `save` gets the result after each call, so a run that stops halfway still tells what is on GitHub.
    """
    repo, pr, review = plan["repo"], plan["pr"], plan["review"]
    result: Doc = {"reviewUrl": None, "posted": {}, "resolved": [], "unresolved": [], "failed": [], "complete": False}

    def record() -> None:
        for reply_id, root_id in plan["attached"].items():
            if root_id in result["posted"]:
                result["posted"][reply_id] = result["posted"][root_id]
        save(result)

    if not review_is_empty(review):
        try:
            response = gh_post(f"repos/{repo}/pulls/{pr}/reviews", review)
        except (subprocess.CalledProcessError, ValueError) as error:
            result["failed"].append({"doc": "review", "error": gh_error(error)})
            result["complete"] = True
            record()
            return result
        result["reviewUrl"] = response.get("html_url")
        result["posted"] |= {doc_id: {"id": None, "url": result["reviewUrl"]} for doc_id in plan["reviewDocs"]}
        record()
        fetched: list[Doc] = []
        if plan["reviewDocs"]:
            try:
                pages = gh_api("--paginate", "--slurp", f"repos/{repo}/pulls/{pr}/reviews/{response['id']}/comments")
                fetched = [comment for page in pages for comment in page]
            except (subprocess.CalledProcessError, ValueError) as error:
                print(f"Warning: the review is posted, but its comment links are not known: {gh_error(error)}")
        result["posted"] |= match_review_comments(review["comments"], plan["reviewDocs"], fetched, result["reviewUrl"])
        record()

    calls = [(item["doc"], item["endpoint"], item["input"]) for item in plan["singles"]]
    calls += [
        (item["doc"], f"repos/{repo}/pulls/{pr}/comments/{item['in_reply_to']}/replies", {"body": item["body"]})
        for item in plan["replies"]
    ]
    for doc_id, endpoint, payload in calls:
        try:
            response = gh_post(endpoint, payload)
            result["posted"][doc_id] = {"id": response["id"], "url": response["html_url"]}
        except (subprocess.CalledProcessError, ValueError, KeyError) as error:
            result["failed"].append({"doc": doc_id, "error": gh_error(error)})
        record()

    for key, done in (("resolve", "resolved"), ("unresolve", "unresolved")):
        for item in plan[key]:
            try:
                response = gh_api("graphql", "-f", f"query={THREAD_MUTATIONS[key]}", "-f", f"id={item['nodeId']}")
                if response.get("errors"):
                    raise ValueError(json.dumps(response["errors"]))
                result[done].append(item["ghId"])
            except (subprocess.CalledProcessError, ValueError) as error:
                result["failed"].append({"doc": f"gh:{item['ghId']}", "error": gh_error(error)})
            record()

    result["complete"] = True
    record()
    return result


def fetch_pull_request(repo: str, number: int, check_author: bool) -> PullRequest:
    pull = gh_api(f"repos/{repo}/pulls/{number}")
    files = [file for page in gh_api("--paginate", "--slurp", f"repos/{repo}/pulls/{number}/files") for file in page]
    owner, name = repo.split("/")
    pages = gh_api(
        "graphql",
        "--paginate",
        "--slurp",
        "-f",
        f"query={THREADS_QUERY}",
        "-f",
        f"o={owner}",
        "-f",
        f"r={name}",
        "-F",
        f"n={number}",
    )
    return PullRequest(
        repo=repo,
        number=number,
        head=pull["head"]["sha"],
        files={file["filename"]: parse_patch(file["patch"]) if file.get("patch") else None for file in files},
        threads={
            node["comments"]["nodes"][0]["databaseId"]: node["id"]
            for page in pages
            for node in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
            if node["comments"]["nodes"]
        },
        opened_by_viewer=check_author and gh_api("user")["login"] == pull["user"]["login"],
    )


def pending_review(repo: str, number: int) -> int | None:
    """The review that the gh user started on GitHub and did not submit. GitHub allows one for each person, so a new
    review or a reply fails while it exists."""
    login = gh_api("user")["login"]
    reviews = [
        review for page in gh_api("--paginate", "--slurp", f"repos/{repo}/pulls/{number}/reviews") for review in page
    ]
    return next((r["id"] for r in reviews if r["state"] == "PENDING" and r["user"]["login"] == login), None)


PENDING_REVIEW = (
    "You have a review on PR #{pr} that is not submitted (review {review}). GitHub allows one for each person, so "
    "this post fails. Submit or delete it on GitHub first."
)


def plan_opens_review(plan: Doc) -> bool:
    """True when post opens a review on GitHub: the review, a reply, or a review comment on a line or a file.
    A PR conversation comment and a thread (un)resolve do not, so a pending review does not stop them."""
    return (
        not review_is_empty(plan["review"])
        or bool(plan["replies"])
        or any("/pulls/" in item["endpoint"] for item in plan["singles"])
    )


def write_plan(pr: int, docs_dir: Path, batch_id: str, out: Path, names_path: Path | None, owner: str) -> None:
    if (batch := load_collection(docs_dir, "batches").get(batch_id)) is None:
        raise SystemExit(f"No batch {batch_id} in {docs_dir / 'batches'}.")
    if problem := batch_problem(batch):
        raise SystemExit(f"Batch {batch_id}: {problem}")
    names = json.loads(names_path.read_text()) if names_path else {}
    pull = fetch_pull_request(repo_name(), pr, check_author=(batch.get("event") or "COMMENT") != "COMMENT")
    plan, summary = build_plan(
        batch_id, batch, load_collection(docs_dir, "comments"), pull, names, owner, datetime.now(timezone.utc)
    )
    if plan_opens_review(plan) and (review := pending_review(pull.repo, pr)):
        summary.append(f"Warning: {PENDING_REVIEW.format(pr=pr, review=review)}")
    out.write_text(json.dumps(plan, indent=2))
    print("\n".join(summary))


def write_result(plan_path: Path, out: Path) -> None:
    plan = json.loads(plan_path.read_text())
    if posted_at := plan.get("postedAt"):
        # A new plan of the same comments would post them a second time: their documents are not marked yet
        raise SystemExit(
            f"This plan was posted at {posted_at}. Do not plan these comments again. First write back what "
            f"{plan.get('resultFile') or 'the result file of that run'} lists as posted. When its complete is "
            "false, that run stopped: compare the plan with the PR on GitHub to find what else it posted."
        )
    head_now = gh_api(f"repos/{plan['repo']}/pulls/{plan['pr']}")["head"]["sha"]
    if problem := plan_problem(plan, head_now, datetime.now(timezone.utc)):
        raise SystemExit(f"{problem} Run plan again.")
    if plan_opens_review(plan) and (review := pending_review(plan["repo"], plan["pr"])):
        raise SystemExit(
            f"{PENDING_REVIEW.format(pr=plan['pr'], review=review)} Nothing was posted. Then run post again."
        )

    def save(result: Doc) -> None:
        out.write_text(json.dumps(result, indent=2))

    if plan_posts(plan):
        # Marked before the first POST, so a second run cannot post the review again, even after a crash
        plan |= {"postedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "resultFile": str(out)}
        plan_path.write_text(json.dumps(plan, indent=2))
        # An older result file at this path must not look like the result of this run
        save({"reviewUrl": None, "posted": {}, "resolved": [], "unresolved": [], "failed": [], "complete": False})
    result = post_plan(plan, save)
    save(result)
    print(f"Review: {result['reviewUrl'] or 'not posted'}")
    print(f"Posted {len(result['posted'])} page comments.")
    print(f"GitHub threads: {len(result['resolved'])} resolved, {len(result['unresolved'])} unresolved.")
    for failure in result["failed"]:
        print(f"Failed {failure['doc']}: {failure['error']}")
    if result["failed"]:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--pr", type=int, required=True)
    plan.add_argument("--docs", type=Path, required=True)
    plan.add_argument("--batch", required=True)
    plan.add_argument("--out", type=Path, required=True)
    plan.add_argument("--names", type=Path)
    # No default: gh posts from the account of the person who runs it, who is not always the batch author
    plan.add_argument("--owner", required=True, help="page user id of the person whose GitHub account posts")
    post = commands.add_parser("post")
    post.add_argument("--plan", type=Path, required=True)
    post.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            write_plan(args.pr, args.docs, args.batch, args.out, args.names, args.owner)
        else:
            write_result(args.plan, args.out)
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"{' '.join(error.cmd[:3])} failed: {gh_error(error)}") from error


if __name__ == "__main__":
    main()
