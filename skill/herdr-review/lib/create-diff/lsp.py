"""Collect hover text, definitions and uses from ty (`ty server`) for the lines shown in the review."""

import io
import json
import os
import shutil
import subprocess
import sys
import token
import tokenize
from keyword import iskeyword
from pathlib import Path
from typing import Any

HOVER_LIMIT = 1500
REFERENCE_LIMIT = 200
REQUEST_BATCH = 100
# ty's hover (plain text) starts with the signature. A name whose hover starts like this, at its own definition,
# gets a list of its uses.
REFERENCE_KINDS = ("def ", "async def ", "class ")


def venv_for(root: Path) -> Path | None:
    """The project's virtualenv: <root>/.venv, else the main checkout's .venv (a git worktree has none)."""
    if (root / ".venv" / "bin" / "python").exists():
        return root / ".venv"
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True
    )
    if out.returncode == 0:
        main = Path(out.stdout.strip()).parent
        if (main / ".venv" / "bin" / "python").exists():
            return main / ".venv"
    return None


class LanguageServer:
    def __init__(self, root: Path) -> None:
        self.root = root
        # ty finds the project's packages through VIRTUAL_ENV
        venv = venv_for(root)
        self.process = subprocess.Popen(
            ["ty", "server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=root,
            env={**os.environ, **({"VIRTUAL_ENV": str(venv)} if venv else {})},
        )
        self.next_id = 0
        folders = [{"uri": root.as_uri(), "name": root.name}]
        self.request(
            "initialize",
            {"processId": None, "rootUri": root.as_uri(), "workspaceFolders": folders, "capabilities": {}},
        )
        self.notify("initialized", {})

    def send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode()
        assert self.process.stdin is not None
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.process.stdin.flush()

    def read(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        length = 0
        while line := self.process.stdout.readline().strip():
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        message: dict[str, Any] = json.loads(self.process.stdout.read(length))
        return message

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def answer_server_request(self, message: dict[str, Any]) -> None:
        result: object = None
        if message["method"] == "workspace/configuration":
            python = str(self.root / ".venv" / "bin" / "python")
            result = [{"pythonPath": python} for _ in message["params"]["items"]]
        self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def request_many(self, calls: list[tuple[str, dict[str, Any]]]) -> list[object]:
        results: list[object] = []
        for start in range(0, len(calls), REQUEST_BATCH):
            results += self.request_batch(calls[start : start + REQUEST_BATCH])
        return results

    def request_batch(self, calls: list[tuple[str, dict[str, Any]]]) -> list[object]:
        ids = []
        for method, params in calls:
            self.next_id += 1
            ids.append(self.next_id)
            self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        results: dict[int, object] = {}
        while len(results) < len(ids):
            message = self.read()
            if "method" in message and "id" in message:
                self.answer_server_request(message)
            elif "id" in message and "method" not in message:
                results[message["id"]] = message.get("result")
        return [results[i] for i in ids]

    def request(self, method: str, params: dict[str, Any]) -> object:
        return self.request_many([(method, params)])[0]

    def close(self) -> None:
        self.process.kill()


def find_names(text: str, wanted_lines: set[int]) -> list[tuple[int, int, int]]:
    names = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == token.NAME and not iskeyword(tok.string) and tok.start[0] in wanted_lines:
                names.append((tok.start[0], tok.start[1], len(tok.string)))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return names


class Collector:
    """Hover and definition tables shared by every blob, deduplicated."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.server = LanguageServer(root)
        self.hovers: list[str] = []
        self.hover_ids: dict[str, int] = {}
        self.definitions: list[dict[str, Any]] = []
        self.definition_ids: dict[tuple[Any, ...], int] = {}
        self.references: list[list[dict[str, Any]]] = []
        self.file_lines: dict[Path, list[str]] = {}

    def add_hover(self, result: object) -> int:
        if not isinstance(result, dict):
            return -1
        contents = result.get("contents")
        text = contents.get("value", "") if isinstance(contents, dict) else str(contents or "")
        text = text.strip()[:HOVER_LIMIT]
        if not text:
            return -1
        if text not in self.hover_ids:
            self.hover_ids[text] = len(self.hovers)
            self.hovers.append(text)
        return self.hover_ids[text]

    def add_definition(self, result: object, doc_path: str, blob_id: str) -> int:
        locations = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
        if not locations:
            return -1
        location = locations[0]
        uri = location.get("targetUri") or location.get("uri", "")
        target_range = location.get("targetSelectionRange") or location.get("range")
        if not uri or not target_range:
            return -1
        line = target_range["start"]["line"] + 1
        file_path = Path(uri.removeprefix("file://"))
        try:
            relative = str(file_path.relative_to(self.root))
        except ValueError:
            relative = None
        same_doc = relative == doc_path
        key = (relative or str(file_path), line, blob_id if same_doc else None)
        if key not in self.definition_ids:
            self.definition_ids[key] = len(self.definitions)
            self.definitions.append(
                {
                    "path": relative or file_path.name,
                    "line": line,
                    "inRepo": relative is not None,
                    "sameDoc": same_doc,
                    "blob": blob_id if same_doc else None,
                }
            )
        return self.definition_ids[key]

    def read_line(self, path: Path, line: int) -> str:
        if path not in self.file_lines:
            try:
                self.file_lines[path] = path.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                self.file_lines[path] = []
        lines = self.file_lines[path]
        return lines[line - 1] if 0 < line <= len(lines) else ""

    def add_references(self, result: object, doc_path: str, doc_lines: list[str]) -> int:
        if not isinstance(result, list) or not result:
            return -1
        usages = []
        for location in result[:REFERENCE_LIMIT]:
            file_path = Path(location["uri"].removeprefix("file://"))
            line = location["range"]["start"]["line"] + 1
            try:
                relative = str(file_path.relative_to(self.root))
            except ValueError:
                continue
            same_doc = relative == doc_path
            text = doc_lines[line - 1] if same_doc and line <= len(doc_lines) else self.read_line(file_path, line)
            usages.append({"path": relative, "line": line, "text": text.strip(), "sameDoc": same_doc})
        if not usages:
            return -1
        self.references.append(usages)
        return len(self.references) - 1

    def collect(self, doc_path: str, blob_id: str, text: str, wanted_lines: set[int]) -> dict[int, list[list[int]]]:
        uri = (self.root / doc_path).as_uri()
        self.server.notify(
            "textDocument/didOpen",
            {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}},
        )
        names = find_names(text, wanted_lines)
        calls = []
        for line, col, _ in names:
            position = {"textDocument": {"uri": uri}, "position": {"line": line - 1, "character": col}}
            calls += [("textDocument/hover", position), ("textDocument/definition", position)]
        results = self.server.request_many(calls)

        definition_sites = []
        for i, (line, col, _) in enumerate(names):
            hover = results[2 * i]
            definition = results[2 * i + 1]
            location = definition[0] if isinstance(definition, list) and definition else definition
            if not isinstance(location, dict) or not isinstance(hover, dict):
                continue
            target = location.get("targetSelectionRange") or location.get("range") or {}
            start = target.get("start", {})
            is_site = (location.get("targetUri") or location.get("uri")) == uri and start == {
                "line": line - 1,
                "character": col,
            }
            hover_text = str((hover.get("contents") or {}).get("value", ""))
            if is_site and hover_text.startswith(REFERENCE_KINDS):
                definition_sites.append(i)
        reference_results = self.server.request_many(
            [
                (
                    "textDocument/references",
                    {
                        "textDocument": {"uri": uri},
                        "position": {"line": names[i][0] - 1, "character": names[i][1]},
                        "context": {"includeDeclaration": False},
                    },
                )
                for i in definition_sites
            ]
        )
        self.server.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

        doc_lines = text.split("\n")
        references_by_name = {}
        for i, result in zip(definition_sites, reference_results):
            references_by_name[i] = self.add_references(result, doc_path, doc_lines)
        tokens: dict[int, list[list[int]]] = {}
        for i, (line, col, length) in enumerate(names):
            hover = self.add_hover(results[2 * i])
            definition = self.add_definition(results[2 * i + 1], doc_path, blob_id)
            references = references_by_name.get(i, -1)
            if hover >= 0 or definition >= 0:
                tokens.setdefault(line, []).append([col, length, hover, definition, references])
        return tokens

    def close(self) -> None:
        self.server.close()


def is_available() -> bool:
    if shutil.which("ty") is None:
        print("ty not found (uv tool install ty): building without hovers", file=sys.stderr)
        return False
    return True
