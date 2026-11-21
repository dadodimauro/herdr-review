// The review of one branch or PR, from the files that the herdr-review skill
// writes in its review folder (props: { folder, view? }). Topics or files on
// the left, the diff with Claude's notes and the threads on the right.
// Comments are drafts until Finish review sends them to a Claude session or
// posts them to GitHub. Keys: j/k hunk, n/p file, ]/[ open thread, N/P next
// topic or file in the list, s split, w wrap, o open in nvim, g GitHub, ? help.

import { View, div, image } from "gpui-kit";
import { h_flex, v_flex, TextView, Textarea, TextareaState, ScrollHandle } from "gpui-base";
import { agents, appHome, checking, exec, hc, nextCommand, notify, props, readTextFile, settings, theme, watchFolder, writeTextFile } from "herdr";
import { loadDocs, newId, nowIso, readJson, runSkill, writeDoc } from "./data.js";
import { GUTTER, hunkRows, lineRow, snippetFor, splitPairs, splitRow } from "./diff.js";
import { openInNvim } from "./nvim.js";
import { ME, cancelGithub, deliverToClaude, planGithub, postGithub, saveClaudeBatch } from "./send.js";

const SEVERITY = { critical: ["Critical", "ansi1"], major: ["Major", "ansi3"], minor: ["Minor", "ansi4"], nit: ["Nit", "dim"] };
const STATUS = { draft: "Draft", to_claude: "Sent to Claude", handled: "Handled", to_github: "Going to GitHub", posted: "On GitHub" };
const FILE_STATUS = { added: "A", deleted: "D", modified: "M", renamed: "R" };
const EVENTS = [["COMMENT", "Comment"], ["APPROVE", "Approve"], ["REQUEST_CHANGES", "Request changes"]];
const KEYS = [
  ["j / k", "next / previous hunk"],
  ["n / p", "next / previous file"],
  ["] / [", "next / previous open thread"],
  ["N / P", "next / previous topic (or file) in the list"],
  ["s", "split or unified view"],
  ["w", "wrap long lines"],
  ["o", "open the selected line (or the hunk) in nvim"],
  ["c", "comment on the selected line"],
  ["g", "check GitHub"],
  ["f", "finish review"],
  ["Esc", "close the comment box or the dialog"],
];

// A short stable hash (djb2), for the diagram cache names.
const hash = (text) => {
  let h = 5381;
  for (let i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) >>> 0;
  return h.toString(16);
};

const who = (author) => (author === ME ? "You" : author === "claude" ? "Claude" : String(author ?? "?"));
const when = (iso) => (iso ? String(iso).slice(0, 16).replace("T", " ") : "");
const base = (path) => String(path ?? "").split("/").pop();
const excerpt = (text, n = 90) => {
  const one = String(text ?? "").replace(/\s+/g, " ").trim();
  return one.length > n ? `${one.slice(0, n - 1)}…` : one;
};

export default class Review extends View {
  init(initial, cx) {
    const p = initial ?? props() ?? {};
    this.folder = p.folder ?? null;
    this.data = null;
    this.notes = null;
    this.herdr = {};
    this.error = null;
    this.docs = { comments: new Map(), batches: new Map(), ghthreads: new Map() };
    this.threads = [];
    this.byLine = new Map();
    this.byFile = new Map();
    this.general = [];
    this.replies = new Map();
    // props.view: {kind: "topic", id} or {kind: "file", path} opens there
    this.view = p.view ?? { kind: "overview" };
    this.list = "topics";
    this.hideResolved = false;
    this.split = Boolean(p.split);
    this.wrap = true;
    // Live: the diff follows the checkout (live.py runs when a file there changes)
    this.live = true;
    this.liveBusy = false;
    this.liveAgain = false;
    this.liveTimer = false;
    this.liveError = null;
    // LSP: hovers, definitions and uses from ty (lsp_tokens.py), after each load and live update
    this.lspBusy = false;
    this.lspAgain = false;
    this.lspError = null;
    this.lineSel = null;
    this.composer = null;
    this.expanded = new Set();
    this.finish = null;
    this.gh = null; // Check GitHub: { busy, changes, rebuild, error }
    this.help = false;
    this.saving = false;
    this.nav = [];
    this.cursor = -1;
    this.scroll = ScrollHandle.new();
    this.keys = cx.focus_handle();
    if (!this.folder) {
      this.error = "No review folder. Open this mod with props { folder }: the herdr-review skill does it.";
      return;
    }
    cx.spawn(async (cx) => {
      try {
        await this.loadAll();
        if (!checking()) {
          // Live runs lsp_tokens.py after its update; without live, run it now
          if (this.live) this.runLive(cx);
          else this.runLsp(cx);
          cx.spawn(async (cx) => this.watchCheckout(cx));
        }
      } catch (err) {
        this.error = `Could not load the review: ${err.message ?? err}`;
      }
      cx.notify();
    });
    if (checking()) return;
    cx.spawn(async (cx) => this.watch(cx));
    // pane.go gives this pane the keyboard
    cx.spawn(async (cx) => {
      for (;;) {
        if ((await nextCommand()) === "focus") this.keys.focus();
        cx.notify();
      }
    });
  }

  // ---- data ---------------------------------------------------------------

  async loadReview() {
    const data = await readJson(`${this.folder}/review.json`);
    this.rowsByPath = new Map();
    for (const file of data.files) {
      const all = [];
      for (const hunk of file.hunks) {
        hunk.rows = hunkRows(hunk);
        all.push(...hunk.rows);
      }
      this.rowsByPath.set(file.path, all);
    }
    this.data = data;
    this.notes = await readJson(`${this.folder}/notes.json`, null);
  }

  async loadAll() {
    await this.loadReview();
    this.herdr = await readJson(`${this.folder}/herdr.json`, {});
    this.docs = await loadDocs(this.folder);
    this.reindex();
  }

  // db/ changes: Claude's replies, a new batch status, a rebuild or a GitHub refresh (meta.json).
  async watch(cx) {
    for (;;) {
      let paths;
      try {
        paths = await watchFolder(`${this.folder}/db`);
      } catch {
        return;
      }
      if (!paths.length) return;
      try {
        if (paths.some((p) => p.endsWith("/meta.json") || p === `${this.folder}/db`)) await this.loadReview();
        this.herdr = await readJson(`${this.folder}/herdr.json`, this.herdr);
        this.docs = await loadDocs(this.folder);
        this.reindex();
        this.error = null;
      } catch (err) {
        this.error = String(err.message ?? err);
      }
      cx.notify();
    }
  }

  // Merges fields into a document (and removes `remove`). One more try after
  // a fresh read when another writer changed the file.
  async updateDoc(collection, id, fields, remove = []) {
    let entry = this.docs[collection].get(id) ?? null;
    for (let attempt = 0; ; attempt++) {
      const doc = { ...(entry?.doc ?? { v: 1 }), ...fields };
      for (const key of remove) delete doc[key];
      try {
        const next = await writeDoc(this.folder, collection, id, doc, entry);
        this.docs[collection].set(id, next);
        this.reindex();
        return next;
      } catch (err) {
        if (attempt) throw err;
        this.docs = await loadDocs(this.folder);
        entry = this.docs[collection].get(id) ?? null;
      }
    }
  }

  async deleteDoc(id) {
    await exec("rm", ["-f", `${this.folder}/db/comments/${id}.json`]);
    this.docs.comments.delete(id);
    this.reindex();
  }

  // The line of a thread in the current diff: the same line when its text is
  // still lineText, else the nearest line with that text, else null.
  place(anchor, lineText) {
    const rows = (this.rowsByPath.get(anchor.path) ?? []).filter((r) => r.side === anchor.side);
    const exact = rows.find((r) => r.n === anchor.line);
    if (exact && (typeof lineText !== "string" || exact.text === lineText)) return exact.n;
    if (typeof lineText !== "string") return null;
    const same = rows.filter((r) => r.text === lineText);
    if (!same.length) return null;
    return same.reduce((a, b) => (Math.abs(b.n - anchor.line) < Math.abs(a.n - anchor.line) ? b : a)).n;
  }

  reindex() {
    if (!this.data) return;
    const roots = [];
    this.replies = new Map();
    for (const entry of this.docs.comments.values()) {
      const to = entry.doc.replyTo;
      if (!to) roots.push(entry);
      else {
        if (!this.replies.has(to)) this.replies.set(to, []);
        this.replies.get(to).push(entry);
      }
    }
    for (const list of this.replies.values()) list.sort((a, b) => String(a.doc.createdAt).localeCompare(String(b.doc.createdAt)));
    const threads = [];
    for (const entry of roots) {
      const a = entry.doc.anchor ?? { type: "general" };
      const th = { key: entry.id, local: entry, resolved: Boolean(entry.doc.resolved), type: a.type, path: a.path ?? null };
      if (a.type === "line") {
        const at = this.place(a, entry.doc.lineText);
        th.side = a.side;
        th.line = at;
        th.startLine = at && a.startLine ? a.startLine + (at - a.line) : null;
        th.outdated = at === null;
      }
      threads.push(th);
    }
    for (const g of this.data.github?.threads ?? []) {
      const over = this.docs.ghthreads.get(String(g.id))?.doc;
      threads.push({
        key: `gh:${g.id}`,
        gh: g,
        resolved: over ? Boolean(over.resolved) : Boolean(g.resolved),
        type: g.file ? "file" : "line",
        path: g.path,
        side: g.side,
        line: g.file ? null : g.line,
        startLine: g.startLine,
        outdated: !g.file && g.line == null,
      });
    }
    threads.sort((a, b) => String(this.createdAt(a)).localeCompare(String(this.createdAt(b))));
    this.threads = threads;
    this.byLine = new Map();
    this.byFile = new Map();
    this.general = [];
    for (const th of threads) {
      if (th.type === "general") this.general.push(th);
      else if (th.line) this.push(this.byLine, `${th.path}|${th.side}|${th.line}`, th);
      else this.push(this.byFile, th.path, th);
    }
  }

  push(map, key, value) {
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(value);
  }

  createdAt(th) {
    return th.local ? th.local.doc.createdAt : th.gh?.comments?.[0]?.at;
  }

  openCount(paths) {
    const set = new Set(paths);
    return this.threads.filter((th) => !th.resolved && th.path && set.has(th.path)).length;
  }

  // ---- composer -----------------------------------------------------------

  openComposer(spec, value = "") {
    this.composer?.state.release();
    const state = TextareaState.new({ placeholder: spec.mode === "reply" ? "Reply…" : "Leave a comment (Markdown)…", rows: 4, value });
    state.set_auto_grow(3, 14);
    state.on("submit", (e, cx) => {
      if (e.secondary) this.saveComposer(cx);
    });
    this.composer = { ...spec, state };
    state.focus();
  }

  closeComposer() {
    this.composer?.state.release();
    this.composer = null;
    this.lineSel = null;
    this.keys.focus();
  }

  selectLine(file, row, shift) {
    if (row.n == null) return null;
    const s = this.lineSel;
    const keep = shift && s && s.path === file.path && s.side === row.side;
    const pivot = keep ? s.pivot : row.n;
    this.lineSel = { path: file.path, side: row.side, start: Math.min(pivot, row.n), end: Math.max(pivot, row.n), pivot };
    return this.lineSel;
  }

  commentOnSelection() {
    const s = this.lineSel;
    if (!s) return;
    const rows = this.rowsByPath.get(s.path) ?? [];
    const lineText = rows.find((r) => r.side === s.side && r.n === s.end)?.text ?? "";
    const anchor = { type: "line", path: s.path, side: s.side, line: s.end };
    if (s.start < s.end) anchor.startLine = s.start;
    const old = this.composer?.mode === "new" ? this.composer.state.value() : "";
    const sel = this.lineSel;
    this.openComposer({ mode: "new", key: `line:${s.path}|${s.side}|${s.end}`, anchor, lineText, snippet: snippetFor(rows, s.side, s.start, s.end) }, old);
    this.lineSel = sel;
  }

  // A click selects a line (shift-click: a range). The bar under the
  // selection comments on it or opens it in nvim (keys c and o).
  clickLine(file, row, shift) {
    if (this.composer?.mode === "new" && !this.composer.state.value().trim()) this.closeComposer();
    this.selectLine(file, row, shift);
    this.keys.focus();
  }

  openSelection() {
    const s = this.lineSel;
    if (s) {
      const row = (this.rowsByPath.get(s.path) ?? []).find((r) => r.side === s.side && r.n === s.end);
      this.openFile(s.path, row ? this.newLine(s.path, row) : s.end);
      return;
    }
    const cur = this.nav[this.cursor];
    if (cur?.path) this.openFile(cur.path, cur.line ?? 1);
  }

  selectionBar(t, indent) {
    const s = this.lineSel;
    const what = s.start < s.end ? `Lines ${s.start}–${s.end}` : `Line ${s.end}`;
    return h_flex().id("sel-bar").ml(indent).mr(12).my(4).gap(6).items_center()
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .child(div().text_size(12).text_color(t.muted).child(`${what}${s.side === "LEFT" ? " (old)" : ""}`))
      .child(this.button("sel-comment", "Comment  c", () => this.commentOnSelection(), { small: true, primary: true }))
      .child(this.button("sel-open", "Open in nvim  o", () => this.openSelection(), { small: true }))
      .child(this.button("sel-clear", "✕", () => { this.lineSel = null; }, { small: true }));
  }

  suggest() {
    const c = this.composer;
    if (!c?.anchor || c.anchor.type !== "line" || c.anchor.side !== "RIGHT") return;
    const rows = (this.rowsByPath.get(c.anchor.path) ?? []).filter((r) => r.new !== null);
    const first = c.anchor.startLine ?? c.anchor.line;
    const lines = rows.filter((r) => r.new >= first && r.new <= c.anchor.line).map((r) => r.text);
    const cur = c.state.value();
    c.state.set_value(`${cur}${cur && !cur.endsWith("\n") ? "\n" : ""}\`\`\`suggestion\n${lines.join("\n")}\n\`\`\`\n`);
    c.state.focus();
  }

  async saveComposer(cx) {
    const c = this.composer;
    if (!c || this.saving) return;
    const body = c.state.value().trim();
    if (!body) return;
    this.saving = true;
    try {
      if (c.mode === "edit") await this.updateDoc("comments", c.editId, { body, editedAt: nowIso() });
      else {
        const doc = { v: 1, author: ME, body, createdAt: nowIso(), status: "draft" };
        if (c.mode === "reply") doc.replyTo = c.replyTo;
        else {
          Object.assign(doc, { kind: "comment", anchor: c.anchor, resolved: false, buildId: this.data.buildId, headSha: this.data.headSha });
          if (c.anchor.type === "line") {
            doc.lineText = c.lineText;
            if (c.snippet) doc.snippet = c.snippet;
          }
        }
        const id = newId("c");
        this.docs.comments.set(id, await writeDoc(this.folder, "comments", id, doc, null));
        this.reindex();
      }
      this.closeComposer();
    } catch (err) {
      notify({ title: "Could not save the comment", body: String(err.message ?? err), level: "error" });
    }
    this.saving = false;
    cx.notify();
  }

  async toggleResolved(th) {
    const at = nowIso();
    if (th.local) await this.updateDoc("comments", th.local.id, { resolved: !th.resolved, resolvedBy: ME, resolvedAt: at });
    else await this.updateDoc("ghthreads", String(th.gh.id), { v: 1, resolved: !th.resolved, by: ME, at, synced: false });
  }

  // ---- editor and GitHub -----------------------------------------------------

  // The checkout of the reviewed code: the files there are the new side.
  checkout() {
    return this.notes?.build?.checkout ?? null;
  }

  openFile(path, line) {
    const root = this.checkout();
    if (!root) {
      notify({ title: "No checkout", body: "notes.json names no checkout for this review.", level: "warning" });
      return;
    }
    openInNvim(`${root}/${path}`, root, line).catch((err) => notify({ title: "Could not open nvim", body: String(err.message ?? err), level: "error" }));
  }

  // The new-side line nearest to a row (a removed line has none).
  newLine(path, row) {
    if (row.new !== null) return row.new;
    const rows = this.rowsByPath.get(path) ?? [];
    const i = rows.indexOf(row);
    for (let k = i; k < rows.length; k++) if (rows[k].new !== null) return rows[k].new;
    for (let k = i; k >= 0; k--) if (rows[k].new !== null) return rows[k].new;
    return 1;
  }

  openToken(token) {
    const def = token[3] >= 0 ? this.data.definitions?.[token[3]] : null;
    if (!def?.inRepo) return;
    this.openFile(def.path, def.line);
  }

  checkGithub(cx) {
    const pr = this.notes?.pr ?? this.data.pr;
    if (!pr || this.gh?.busy) return;
    if (!this.herdr.skill) {
      this.gh = { error: "herdr.json names no skill. Ask Claude to run /herdr-review again." };
      return;
    }
    this.gh = { busy: true };
    cx.spawn(async (cx) => {
      try {
        const r = await runSkill(this, "github_refresh.py", ["--folder", this.folder], 180000);
        const last = r.stdout.trim().split("\n").pop() || "{}";
        const res = JSON.parse(last);
        this.gh = res.ok ? { changes: res.changes ?? [], rebuild: res.rebuild } : { error: res.error || r.stderr.trim() || "github_refresh failed" };
        // The watch loads review.json again (meta.json changed); load now too, in case the watch was reclaimed
        if (res.ok) {
          await this.loadReview();
          this.reindex();
        }
      } catch (err) {
        this.gh = { error: String(err.message ?? err) };
      }
      cx.notify();
    });
  }

  askRebuild() {
    const session = this.claudeSessions().find((a) => a.address === this.herdr.session)?.address ?? this.claudeSessions()[0]?.address;
    if (!session) {
      notify({ title: "No Claude session", body: "Start Claude and type /herdr-review in the review's checkout.", level: "warning" });
      return;
    }
    hc(["send", session, `/herdr-review rebuild ${this.folder}`]);
    notify({ title: "Asked Claude to rebuild the review", body: `Session ${session}`, level: "info" });
    this.gh = null;
  }

  // ---- Mermaid diagram --------------------------------------------------------

  // The diagram as a PNG from mmdc (mermaid-cli), in the theme's appearance.
  // Cached in mods/review/.cache (a hidden folder: saving there does not reload the mod).
  ensureDiagram(cx) {
    const src = this.data?.diagram;
    if (!src || checking()) return;
    const dark = Boolean(settings().dark);
    const key = `${hash(src)}-${dark ? "dark" : "light"}`;
    if (this.diagram?.key === key) return;
    this.diagram = { key, busy: true };
    cx.spawn(async (cx) => {
      const rel = `mods/review/.cache/${key}.png`;
      const dir = `${appHome()}/mods/review/.cache`;
      const out = `${appHome()}/${rel}`;
      try {
        await exec("mkdir", ["-p", dir]);
        if ((await exec("test", ["-s", out])).code !== 0) {
          const input = `${dir}/${key}.mmd`;
          const old = await readTextFile(input).catch(() => null);
          if (old !== src) await writeTextFile(input, src, old);
          const r = await exec("mmdc", ["-q", "-i", input, "-o", out, "-t", dark ? "dark" : "default", "-b", "transparent", "-s", "2"], { timeout: 90000 });
          if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim().split("\n").slice(-4).join(" ") || "mmdc failed");
        }
        // The PNG is at scale 2: half its pixels is its size on screen
        const size = await exec("sips", ["-g", "pixelWidth", "-g", "pixelHeight", out]);
        const w = Number(/pixelWidth: (\d+)/.exec(size.stdout)?.[1] ?? 0) / 2;
        const h = Number(/pixelHeight: (\d+)/.exec(size.stdout)?.[1] ?? 0) / 2;
        if (this.diagram?.key === key) this.diagram = { key, path: rel, w, h };
      } catch (err) {
        if (this.diagram?.key === key) this.diagram = { key, error: String(err.message ?? err) };
      }
      cx.notify();
    });
  }

  diagramBlock(t, src) {
    const g = this.diagram;
    const source = () => div().p(10).rounded(8).bg(t.surface).font_family(this.mono).text_size(12).text_color(t.muted)
      .children(src.split("\n").map((l) => div().whitespace_nowrap().child(l.replace(/ /g, "\u00a0") || "\u00a0")));
    if (g?.path && g.w > 0) {
      const scale = Math.min(1, 1000 / g.w);
      return div().mx(18).p(12).rounded(8).border_1().border_color(t.border).bg(t.surface).flex().justify_center()
        .child(image(g.path).w(g.w * scale).h(g.h * scale));
    }
    return v_flex().mx(18).gap(6)
      .child(div().text_size(12).text_color(g?.error ? t.ansi1 : t.dim).child(g?.error ? `Mermaid could not draw it: ${g.error}` : g?.busy ? "Drawing the diagram…" : "Mermaid source"))
      .child(source());
  }

  // ---- live diff ------------------------------------------------------------

  // A change in the checkout (not in .git, caches or editor temp files) runs live.py.
  async watchCheckout(cx) {
    const root = this.checkout();
    if (!root) return;
    const skip = /(^|\/)(\.git|node_modules|__pycache__|\.venv|\.mypy_cache|\.pytest_cache|\.ruff_cache)(\/|$)|\.pyc$|~$|\.sw[a-p]$|(^|\/)4913$/;
    for (;;) {
      let paths;
      try {
        paths = await watchFolder(root);
      } catch {
        return;
      }
      if (!paths.length) return;
      if (!this.live) continue;
      const rel = paths.map((p) => (p === root ? "" : p.startsWith(`${root}/`) ? p.slice(root.length + 1) : null)).filter((p) => p !== null);
      if (rel.some((p) => p === "" || !skip.test(p))) this.scheduleLive(cx);
    }
  }

  // Many saves in a short time make one run.
  scheduleLive(cx) {
    if (this.liveTimer) return;
    this.liveTimer = true;
    cx.timer.after(400, (cx) => {
      this.liveTimer = false;
      this.runLive(cx);
    });
  }

  runLive(cx) {
    if (!this.live || !this.herdr.skill) return;
    if (this.liveBusy) {
      this.liveAgain = true;
      return;
    }
    this.liveBusy = true;
    cx.spawn(async (cx) => {
      try {
        const r = await runSkill(this, "live.py", ["--folder", this.folder], 120000);
        const res = JSON.parse(r.stdout.trim().split("\n").pop() || "{}");
        if (res.ok) {
          await this.loadReview();
          this.reindex();
          this.liveError = null;
          // The code changed: the LSP data of the new lines comes after
          this.runLsp(cx);
        } else this.liveError = res.error || r.stderr.trim() || "live.py failed";
      } catch (err) {
        this.liveError = String(err.message ?? err);
      }
      this.liveBusy = false;
      if (this.liveAgain) {
        this.liveAgain = false;
        this.runLive(cx);
      }
      cx.notify();
    });
  }

  runLsp(cx) {
    if (!this.herdr.skill || checking()) return;
    if (this.lspBusy) {
      this.lspAgain = true;
      return;
    }
    this.lspBusy = true;
    cx.spawn(async (cx) => {
      try {
        const r = await runSkill(this, "lsp_tokens.py", ["--folder", this.folder], 180000);
        const res = JSON.parse(r.stdout.trim().split("\n").pop() || "{}");
        if (res.ok) {
          await this.loadReview();
          this.reindex();
          this.lspError = null;
        } else this.lspError = res.error || r.stderr.trim() || "lsp_tokens.py failed";
      } catch (err) {
        this.lspError = String(err.message ?? err);
      }
      this.lspBusy = false;
      if (this.lspAgain) {
        this.lspAgain = false;
        this.runLsp(cx);
      }
      cx.notify();
    });
  }

  // ---- keys ---------------------------------------------------------------

  step(kind, dir) {
    const nav = this.nav;
    if (!nav.length) return;
    const ok = (i) => (kind === "hunk" ? nav[i].hunk : kind === "file" ? nav[i].fileHead : nav[i].open > 0);
    let i = this.cursor;
    for (let n = 0; n < nav.length; n++) {
      i += dir;
      if (i < 0 || i >= nav.length) return;
      if (ok(i)) {
        this.cursor = i;
        this.scroll.scroll_to_item(i);
        return;
      }
    }
  }

  stepList(dir) {
    const d = this.data;
    const ids = this.list === "topics" ? d.groups.map((g) => ({ kind: "topic", id: g.id })) : d.files.map((f) => ({ kind: "file", path: f.path }));
    const views = [{ kind: "overview" }, ...ids];
    const v = this.view;
    const at = views.findIndex((x) => x.kind === v.kind && (x.id ?? x.path) === (v.id ?? v.path));
    const next = views[Math.max(0, Math.min(views.length - 1, at + dir))];
    this.go(next);
  }

  go(view) {
    this.view = view;
    this.cursor = -1;
    this.lineSel = null;
    this.scroll.scroll_to_item(0);
  }

  key(e, cx) {
    if (e.key === "escape") {
      if (this.composer) this.closeComposer();
      else if (this.finish) this.closeFinish();
      else if (this.gh && !this.gh.busy) this.gh = null;
      else if (this.help) this.help = false;
      else return;
      cx.stop_propagation();
      cx.notify();
      return;
    }
    // Letters type text while a comment box or a dialog is open
    if (this.composer || this.finish || e.modifiers?.platform || e.modifiers?.control || e.modifiers?.alt) return;
    const k = e.key_char ?? e.key;
    switch (k) {
      case "j": this.step("hunk", 1); break;
      case "k": this.step("hunk", -1); break;
      case "n": this.step("file", 1); break;
      case "p": this.step("file", -1); break;
      case "]": this.step("thread", 1); break;
      case "[": this.step("thread", -1); break;
      case "N": this.stepList(1); break;
      case "P": this.stepList(-1); break;
      case "s": this.split = !this.split; break;
      case "w": this.wrap = !this.wrap; break;
      case "c": this.commentOnSelection(); break;
      case "g": this.checkGithub(cx); break;
      case "f": this.openFinish(); break;
      case "?": this.help = !this.help; break;
      case "o": this.openSelection(); break;
      default:
        return;
    }
    cx.stop_propagation();
    cx.notify();
  }

  // ---- render: frame --------------------------------------------------------

  render(cx) {
    const t = theme();
    this.t = t;
    this.mono = settings().font_family ?? "JetBrains Mono";
    const root = v_flex().id("review").size_full().relative().bg(t.background).text_color(t.foreground).text_size(13)
      .track_focus(this.keys)
      .on_key_down((e, cx) => this.key(e, cx))
      .on_mouse_down("left", () => {
        if (!this.composer && !this.finish) this.keys.focus();
      });
    if (!this.data) return root.child(div().p(20).text_color(t.muted).child(this.error ?? "Loading the review…"));
    if (this.view.kind === "overview") this.ensureDiagram(cx);
    return root
      .child(this.header(t))
      .when(Boolean(this.error), (e) => e.child(div().px(14).py(4).bg(`${t.ansi1}22`).text_color(t.ansi1).text_size(12).child(this.error)))
      .child(h_flex().flex_1().min_h(0).child(this.sidebar(t)).child(this.content(t)))
      .when(Boolean(this.gh) && !this.finish, (e) => e.child(this.githubPanel(t)))
      .when(this.help, (e) => e.child(this.helpPanel(t)))
      .when(Boolean(this.finish), (e) => e.child(this.finishPanel(t)));
  }

  button(id, label, onClick, { primary = false, small = false, disabled = false, on = false } = {}) {
    const t = this.t;
    return h_flex()
      .id(id)
      .flex_none()
      .h(small ? 24 : 30)
      .px(small ? 8 : 12)
      .items_center()
      .rounded(6)
      .text_size(small ? 12 : 13)
      .border_1()
      .border_color(primary ? t.solid : on ? t.accent : t.input)
      .bg(primary ? t.solid : on ? t.selected : t.raised)
      .text_color(primary ? t.solid_foreground : t.foreground)
      .when(disabled, (e) => e.opacity(0.45))
      .when(!disabled, (e) => e.cursor_pointer().hover((s) => s.opacity(0.85)).on_click((ev, cx) => {
        onClick(ev, cx);
        cx.notify();
      }))
      .child(label);
  }

  chip(text, color) {
    const t = this.t;
    return div().flex_none().px(6).py(1).rounded(4).text_size(11).bg(`${color ?? t.dim}26`).text_color(color ?? t.muted).child(text);
  }

  drafts() {
    return [...this.docs.comments.values()].filter((e) => e.doc.status === "draft" && e.doc.author === ME && e.doc.kind !== "finding");
  }

  header(t) {
    const d = this.data;
    const pr = this.notes?.pr ?? d.pr;
    const sub = [`${d.branch} → ${d.base}`, `${d.stats.files} files`, `+${d.stats.adds} −${d.stats.dels}`];
    if (pr) sub.push(`PR #${pr}`);
    if (d.dirty) sub.push("uncommitted work");
    if (d.live?.at) sub.push(`live ${new Date(d.live.at).toTimeString().slice(0, 8)}${d.live.needNotes?.length ? ` · ${d.live.needNotes.length} hunk${d.live.needNotes.length === 1 ? "" : "s"} changed after the review` : ""}`);
    if (this.liveError) sub.push(`live: ${this.liveError}`);
    if (this.lspBusy) sub.push("LSP…");
    else if (this.lspError) sub.push(`LSP: ${this.lspError}`);
    const n = this.drafts().length;
    return h_flex()
      .h(52)
      .flex_none()
      .px(14)
      .gap(6)
      .items_center()
      .bg(t.chrome)
      .border_b_1()
      .border_color(t.border)
      .child(
        v_flex()
          .flex_1()
          .min_w(0)
          .child(div().font_semibold().text_size(14).truncate().child(d.title || d.heading))
          .child(div().text_size(12).text_color(t.muted).truncate().child(sub.join("  ·  "))),
      )
      .child(this.button("hd-live", this.liveBusy ? "Live…" : "Live", (_e, cx) => {
        this.live = !this.live;
        if (this.live) this.runLive(cx);
      }, { small: true, on: this.live }))
      .child(this.button("hd-split", "Split", () => { this.split = !this.split; }, { small: true, on: this.split }))
      .child(this.button("hd-wrap", "Wrap", () => { this.wrap = !this.wrap; }, { small: true, on: this.wrap }))
      .child(this.button("hd-resolved", "Hide resolved", () => { this.hideResolved = !this.hideResolved; }, { small: true, on: this.hideResolved }))
      .when(Boolean(pr), (e) => e.child(this.button("hd-gh", this.gh?.busy ? "Checking…" : "Check GitHub", (_e, cx) => this.checkGithub(cx), { small: true, disabled: Boolean(this.gh?.busy) })))
      .child(this.button("hd-help", "?", () => { this.help = !this.help; }, { small: true }))
      .child(this.button("finish", n ? `Finish review (${n})` : "Finish review", () => this.openFinish(), { primary: true }));
  }

  sidebar(t) {
    const d = this.data;
    const item = (id, active, title, sub, open, view) =>
      v_flex()
        .id(id)
        .px(12)
        .py(7)
        .gap(2)
        .cursor_pointer()
        .border_l_2()
        .border_color(active ? t.accent : "#00000000")
        .when(active, (e) => e.bg(t.selected))
        .hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => { this.go(view); cx.notify(); })
        .child(h_flex().gap(6).items_center().child(div().flex_1().min_w(0).truncate().text_color(active ? t.foreground : t.muted).child(title))
          .when(open > 0, (e) => e.child(this.chip(String(open), t.accent))))
        .when(Boolean(sub), (e) => e.child(div().text_size(11).text_color(t.dim).truncate().child(sub)));
    const tab = (id, label) =>
      div().id(`list-${id}`).flex_1().py(6).text_size(12).cursor_pointer().flex().justify_center()
        .text_color(this.list === id ? t.foreground : t.dim)
        .when(this.list === id, (e) => e.border_b_1().border_color(t.accent))
        .on_click((_e, cx) => { this.list = id; cx.notify(); })
        .child(label);
    const v = this.view;
    const entries =
      this.list === "topics"
        ? d.groups.map((g) => {
            const paths = g.files.map((i) => d.files[i].path);
            return item(`g-${g.id}`, v.kind === "topic" && v.id === g.id, g.title, `${paths.length} file${paths.length === 1 ? "" : "s"}`, this.openCount(paths), { kind: "topic", id: g.id });
          })
        : d.files.map((f) => {
            const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : "";
            return item(`f-${f.path}`, v.kind === "file" && v.path === f.path, `${FILE_STATUS[f.status] ?? "M"}  ${base(f.path)}`, `${dir ? `${dir} · ` : ""}+${f.adds} −${f.dels}`, this.openCount([f.path]), { kind: "file", path: f.path });
          });
    return v_flex()
      .w(280)
      .flex_none()
      .h_full()
      .bg(t.chrome)
      .border_r_1()
      .border_color(t.border)
      .child(h_flex().flex_none().border_b_1().border_color(t.border).child(tab("topics", "Topics")).child(tab("files", "Files")))
      .child(
        v_flex()
          .id("side-list")
          .flex_1()
          .min_h(0)
          .overflow_y_scrollbar()
          .py(4)
          .child(item("overview", v.kind === "overview", "Overview", "Summary, findings, conversation", this.general.filter((th) => !th.resolved).length, { kind: "overview" }))
          .children(entries),
      );
  }

  // The content is a flat list of children, so that the keys can scroll to
  // a hunk (ScrollHandle reveals direct children only). this.nav keeps what
  // each child is.
  content(t) {
    const v = this.view;
    const d = this.data;
    const items = [];
    if (v.kind === "topic") {
      const g = d.groups.find((x) => x.id === v.id);
      if (g) {
        items.push({ el: v_flex().px(18).pt(16).pb(4).gap(4).child(div().text_size(16).font_semibold().child(g.title)).child(TextView.html(`gsum-${g.id}`, g.summary || "").text_color(t.muted)) });
        for (const i of g.files) this.fileItems(d.files[i], d.files[i].hunks.filter((h) => h.group === g.id), t, items);
      }
    } else if (v.kind === "file") {
      const f = d.files.find((x) => x.path === v.path);
      if (f) this.fileItems(f, f.hunks, t, items);
    } else for (const el of this.overview(t)) items.push({ el });
    items.push({ el: div().h(40) });
    this.nav = items.map(({ el, ...meta }) => meta);
    if (this.cursor >= items.length) this.cursor = -1;
    return v_flex().id("content").flex_1().min_w(0).h_full().overflow_y_scrollbar().track_scroll(this.scroll)
      .children(items.map(({ el }, i) => (i === this.cursor && items[i].hunk ? el.border_l_4().border_color(t.accent) : el)));
  }

  // ---- render: overview -----------------------------------------------------

  prBox(t) {
    const pr = this.data.github?.pr;
    if (!pr) return null;
    const counts = {};
    for (const c of pr.checks ?? []) counts[c.result] = (counts[c.result] ?? 0) + 1;
    const failing = (pr.checks ?? []).filter((c) => ["failure", "error", "timed_out", "cancelled"].includes(c.result));
    const decision = { APPROVED: ["Approved", t.ansi2], CHANGES_REQUESTED: ["Changes requested", t.ansi1], REVIEW_REQUIRED: ["Review required", t.ansi3] }[pr.reviewDecision];
    return v_flex().mx(18).p(12).gap(6).rounded(8).border_1().border_color(t.border).bg(t.surface).text_size(12)
      .child(h_flex().gap(6).items_center()
        .child(this.chip(pr.state ?? "?", pr.state === "open" ? t.ansi2 : pr.state === "merged" ? t.ansi5 : t.dim))
        .when(Boolean(decision), (e) => e.child(this.chip(decision[0], decision[1])))
        .child(div().flex_1().min_w(0).truncate().text_color(t.muted).child(`${pr.author ?? "?"} · ${pr.headRef} → ${pr.baseRef} · ${pr.commits ?? "?"} commit${pr.commits === 1 ? "" : "s"}`)))
      .when(Boolean(pr.reviewers?.length), (e) => e.child(div().text_color(t.muted).child(`Reviewers: ${pr.reviewers.map((r) => `${r.login} (${String(r.state ?? "requested").toLowerCase().replace("_", " ")})`).join(", ")}`)))
      .when(Boolean(pr.checks?.length), (e) => e.child(div().text_color(failing.length ? t.ansi1 : t.muted)
        .child(`Checks: ${Object.entries(counts).map(([k, n]) => `${n} ${k}`).join(", ")}${failing.length ? ` · failing: ${failing.slice(0, 5).map((c) => c.name).join(", ")}` : ""}`)))
      .when(Boolean(pr.fetchedAt), (e) => e.child(div().text_size(11).text_color(t.dim).child(`From GitHub at ${when(pr.fetchedAt)} · Check GitHub (g) loads it again`)));
  }

  overview(t) {
    const d = this.data;
    const section = (title) => div().px(18).pt(18).pb(6).text_size(12).font_semibold().text_color(t.dim).child(title.toUpperCase());
    const out = [
      v_flex().px(18).pt(16).gap(6)
        .child(div().text_size(18).font_semibold().child(d.heading || d.title))
        .child(TextView.html("intro", d.intro || "").text_color(t.muted))
        .when(Boolean(d.effort), (e) => e.child(div().text_size(12).text_color(t.dim).child(`Review effort ${d.effort.level}/5 · about ${d.effort.minutes} min`))),
    ];
    const box = this.prBox(t);
    if (box) out.push(section(`PR #${this.notes?.pr ?? d.pr ?? ""}`), box);
    out.push(section("Topics"));
    out.push(v_flex().px(18).gap(2).children(d.groups.map((g, i) =>
      h_flex().id(`ov-g-${g.id}`).gap(8).py(3).cursor_pointer().hover((s) => s.text_color(t.accent))
        .on_click((_e, cx) => { this.go({ kind: "topic", id: g.id }); cx.notify(); })
        .child(div().w(18).text_color(t.dim).child(`${i + 1}.`))
        .child(div().flex_1().min_w(0).truncate().child(g.title)))));
    const findings = this.threads.filter((th) => th.local?.doc.kind === "finding");
    if (findings.length) {
      out.push(section(`Findings (${findings.length})`));
      out.push(v_flex().px(18).gap(2).children(findings.map((th) => {
        const doc = th.local.doc;
        const [label, color] = SEVERITY[doc.severity] ?? ["?", "dim"];
        return h_flex().id(`ov-f-${th.key}`).gap(8).py(3).items_center().cursor_pointer().hover((s) => s.bg(t.hover))
          .when(th.resolved, (e) => e.opacity(0.5))
          .on_click((_e, cx) => { this.go({ kind: "file", path: th.path }); cx.notify(); })
          .child(this.chip(label, t[color] ?? t.dim))
          .child(div().flex_1().min_w(0).truncate().child(doc.title || excerpt(doc.body)))
          .child(div().text_size(11).text_color(t.dim).child(`${base(th.path)}:${th.line ?? doc.anchor?.line}`));
      })));
    }
    if (d.diagram) {
      out.push(section("Diagram"));
      out.push(this.diagramBlock(t, d.diagram));
    }
    out.push(section("Conversation"));
    for (const c of d.github?.conversation ?? []) {
      out.push(this.card(`conv-${c.url ?? c.at}`, t).child(this.entryHead(`@${c.author}`, c.at, [this.chip("GitHub", t.dim)]))
        .child(TextView.markdown(`md-conv-${c.url ?? c.at}`, c.body || "").selectable()));
    }
    for (const th of this.general) out.push(this.threadCard(th, t, 18));
    if (this.composer?.key === "general") out.push(this.composerBox(t, 18));
    else out.push(div().px(18).pt(8).child(this.button("add-general", "Add a general comment", () => this.openComposer({ mode: "new", key: "general", anchor: { type: "general" } }), { small: true })));
    const batches = [...this.docs.batches.values()].sort((a, b) => String(b.doc.createdAt).localeCompare(String(a.doc.createdAt)));
    if (batches.length) {
      out.push(section("Sent"));
      out.push(v_flex().px(18).gap(4).children(batches.slice(0, 20).map(({ id, doc }) =>
        h_flex().gap(8).items_center().text_size(12)
          .child(this.chip(doc.status, doc.status === "failed" ? t.ansi1 : doc.status === "done" ? t.ansi2 : t.accent))
          .child(div().flex_none().text_color(t.muted).child(`${doc.target === "github" ? "GitHub" : doc.target === "claude" ? "Claude" : doc.target} · ${(doc.ids ?? []).length} comments · ${when(doc.createdAt)}`))
          .child(div().flex_1().min_w(0).truncate().text_color(t.dim).child(doc.note ?? (doc.sentTo ? `sent to ${doc.sentTo}` : id)))
          .when(doc.target === "claude" && doc.status === "pending", (e) => e.child(this.button(`again-${id}`, "Send again", (_e, cx) => {
            const sessions = this.claudeSessions();
            const session = sessions.find((a) => a.address === (doc.sentTo ?? this.herdr.session))?.address ?? sessions[0]?.address;
            if (session) this.deliver(id, session, cx);
            else notify({ title: "No Claude session", body: "Start Claude, then Send again.", level: "warning" });
          }, { small: true }))))));
    }
    return out;
  }

  // ---- render: files, lines, threads -----------------------------------------

  // A file as several children: its header (with its note and file threads)
  // and one child per hunk, drawn as one card by their side borders.
  fileItems(file, hunks, t, items) {
    const fileThreads = this.byFile.get(file.path) ?? [];
    const key = `file:${file.path}`;
    const side = (el) => el.mx(14).border_l_1().border_r_1().border_color(t.border);
    const first = file.hunks[0]?.rows?.find((r) => r.new !== null)?.new ?? 1;
    const head = v_flex().mx(14).mt(14).rounded_t(8).border_1().border_color(t.border).overflow_hidden()
      .child(
        h_flex().h(36).px(10).gap(8).items_center().bg(t.surface)
          .child(this.chip(FILE_STATUS[file.status] ?? "M", file.status === "added" ? t.ansi2 : file.status === "deleted" ? t.ansi1 : t.dim))
          .child(div().flex_1().min_w(0).truncate().font_family(this.mono).text_size(12).child(file.oldPath ? `${file.oldPath} → ${file.path}` : file.path))
          .child(div().text_size(12).text_color(t.ansi2).child(`+${file.adds}`))
          .child(div().text_size(12).text_color(t.ansi1).child(`−${file.dels}`))
          .when(file.status !== "deleted", (e) => e.child(this.button(`fo-${file.path}`, "nvim", () => this.openFile(file.path, first), { small: true })))
          .child(this.button(`fc-${file.path}`, "Comment on file", () => this.openComposer({ mode: "new", key, anchor: { type: "file", path: file.path } }), { small: true })),
      )
      .when(Boolean(file.note), (e) => e.child(this.noteBox(`fn-${file.path}`, file.note, t)))
      .children(fileThreads.map((th) => this.threadCard(th, t, 10)))
      .when(this.composer?.key === key, (e) => e.child(this.composerBox(t, 10)));
    items.push({ el: head, fileHead: true, path: file.path, line: first, open: fileThreads.filter((th) => !th.resolved).length });
    for (const h of hunks) {
      const open = h.rows.reduce((n, r) => n + (this.byLine.get(`${file.path}|${r.side}|${r.n}`) ?? []).filter((th) => !th.resolved).length, 0);
      items.push({ el: side(this.hunkBlock(file, h, t)), hunk: h.id, path: file.path, line: h.rows.find((r) => r.new !== null)?.new ?? h.newStart, open });
    }
    items.push({ el: div().mx(14).h(8).rounded_b(8).border_1().border_t_0().border_color(t.border) });
  }

  noteBox(id, html, t) {
    return h_flex().px(12).py(8).gap(8).items_start().bg(`${t.accent}12`).border_b_1().border_color(t.border)
      .child(div().flex_none().text_size(11).font_semibold().text_color(t.accent).child("NOTE"))
      .child(TextView.html(id, html).flex_1().min_w(0).text_size(12).text_color(t.foreground));
  }

  hunkBlock(file, hunk, t) {
    const rows = hunk.rows;
    const out = [];
    const changed = this.data.live?.needNotes?.includes(hunk.id);
    out.push(h_flex().px(12).py(3).gap(8).items_center().bg(t.raised)
      .child(div().font_family(this.mono).text_size(11).text_color(t.dim).child(`@@ −${hunk.oldStart} +${hunk.newStart} @@`))
      .when(changed, (e) => e.child(this.chip("changed after the review · no note", t.ansi3))));
    if (hunk.note) out.push(this.noteBox(`hn-${hunk.id}`, hunk.note, t));
    const syntax = this.data.syntax ?? {};
    const tokens = this.data.tokens ?? {};
    const sel = this.lineSel;
    const blobOf = (row, side) => (side === "LEFT" ? file.oldBlob : file.newBlob);
    const numOf = (row, side) => String(side === "LEFT" ? row.old : row.new);
    const ctxFor = (key) => ({
      t,
      mono: this.mono,
      key,
      wrap: this.wrap,
      lsp: this.data,
      spans: (row, side) => syntax[blobOf(row, side)]?.[numOf(row, side)] ?? null,
      tokens: (row, side) => tokens[blobOf(row, side)]?.[numOf(row, side)] ?? null,
      selected: (row) => Boolean(sel && sel.path === file.path && sel.side === row.side && row.n >= sel.start && row.n <= sel.end),
      onGutter: (r, e, cx) => {
        this.clickLine(file, r, e.modifiers?.shift);
        cx.notify();
      },
      onToken: (token) => this.openToken(token),
      onOpen: (row, side, cx) => {
        this.openFile(file.path, side === "LEFT" ? this.newLine(file.path, row) : row.new ?? this.newLine(file.path, row));
        cx.notify();
      },
    });
    // Notes, threads and the composer of one row, after its line
    const after = (row, i) => {
      const lineNote = hunk.lineNotes?.[`${row.side === "LEFT" ? "L" : "R"}${row.n}`];
      if (lineNote) {
        out.push(h_flex().pl(GUTTER * 2 + 16).pr(12).py(4).gap(8).items_start().bg(`${t.accent}0d`)
          .child(div().flex_none().text_size(11).text_color(t.accent).child("ⓘ"))
          .child(TextView.html(`ln-${hunk.id}-${row.side}-${i}`, lineNote).flex_1().min_w(0).text_size(12).text_color(t.muted)));
      }
      const here = `${file.path}|${row.side}|${row.n}`;
      for (const th of this.byLine.get(here) ?? []) out.push(this.threadCard(th, t, this.split ? 16 : GUTTER * 2 + 16));
      if (this.composer?.key === `line:${here}`) out.push(this.composerBox(t, this.split ? 16 : GUTTER * 2 + 16));
      else if (sel && sel.path === file.path && sel.side === row.side && sel.end === row.n) out.push(this.selectionBar(t, this.split ? 16 : GUTTER * 2 + 16));
    };
    if (this.split) {
      splitPairs(rows).forEach(([left, right], i) => {
        out.push(splitRow(left, right, ctxFor(`${hunk.id}-${i}`)));
        const seen = new Set();
        for (const row of [left, right]) {
          if (row && !seen.has(row)) {
            seen.add(row);
            after(row, i);
          }
        }
      });
    } else {
      rows.forEach((row, i) => {
        out.push(lineRow(row, ctxFor(`${hunk.id}-${i}`)));
        after(row, i);
      });
    }
    return v_flex().children(out);
  }

  card(id, t, indent = 18) {
    return v_flex().id(id).ml(indent).mr(12).my(5).p(10).gap(6).rounded(8).border_1().border_color(t.border).bg(t.surface);
  }

  entryHead(author, at, chips = []) {
    const t = this.t;
    return h_flex().gap(6).items_center().text_size(12)
      .child(div().font_semibold().child(author))
      .child(div().text_color(t.dim).child(when(at)))
      .children(chips);
  }

  threadCard(th, t, indent) {
    if (this.hideResolved && th.resolved) return div();
    const open = !th.resolved || this.expanded.has(th.key);
    const local = th.local?.doc;
    const isFinding = local?.kind === "finding";
    const headChips = [];
    if (isFinding) {
      const [label, color] = SEVERITY[local.severity] ?? ["?", "dim"];
      headChips.push(this.chip(label, t[color] ?? t.dim));
      if (local.category) headChips.push(this.chip(local.category, t.dim));
    }
    if (th.gh) headChips.push(this.chip("GitHub", t.dim));
    if (local && local.status && local.status !== "draft") headChips.push(this.chip(STATUS[local.status] ?? local.status, t.accent));
    if (local?.status === "draft" && !isFinding) headChips.push(this.chip("Draft", t.ansi3));
    if (th.outdated) headChips.push(this.chip("line changed", t.ansi3));
    if (th.resolved) headChips.push(this.chip("Resolved", t.ansi2));
    const where = th.line ? `${th.startLine ? `${th.startLine}–` : ""}${th.line}` : "";
    const title = isFinding ? local.title || "Finding" : th.gh ? `@${th.gh.comments?.[0]?.author ?? "?"}` : who(local?.author);
    const head = h_flex().id(`th-head-${th.key}`).gap(6).items_center().cursor_pointer()
      .on_click((_e, cx) => {
        if (this.expanded.has(th.key)) this.expanded.delete(th.key);
        else this.expanded.add(th.key);
        cx.notify();
      })
      .child(div().flex_none().text_color(t.dim).text_size(11).child(open ? "▾" : "▸"))
      .children(headChips)
      .child(div().flex_1().min_w(0).truncate().font_semibold().text_size(12).child(title))
      .when(Boolean(where), (e) => e.child(div().text_size(11).text_color(t.dim).child(where)));
    const card = this.card(`th-${th.key}`, t, indent).when(th.resolved, (e) => e.opacity(open ? 0.85 : 0.6)).child(head);
    if (!open) return card.child(div().text_size(12).text_color(t.muted).truncate().child(excerpt(local?.body ?? th.gh?.comments?.[0]?.body)));
    // The entries: the root and its replies (local), or the GitHub comments and the local replies to them
    const entries = [];
    if (local) entries.push({ id: th.local.id, author: who(local.author), at: local.createdAt, body: local.body, local: th.local, root: true });
    for (const c of th.gh?.comments ?? []) entries.push({ id: `gh-${c.id}`, author: `@${c.author}`, at: c.at, body: c.body });
    for (const r of this.replies.get(th.local ? th.local.id : th.key) ?? []) entries.push({ id: r.id, author: who(r.doc.author), at: r.doc.createdAt, body: r.doc.body, local: r });
    entries.forEach((en, i) => {
      const mine = en.local && en.local.doc.author === ME && en.local.doc.status === "draft";
      const editing = this.composer?.mode === "edit" && this.composer.editId === en.id;
      const chips = [];
      if (en.local && !en.root && en.local.doc.status) chips.push(this.chip(STATUS[en.local.doc.status] ?? en.local.doc.status, en.local.doc.status === "draft" ? t.ansi3 : t.accent));
      card.child(
        v_flex().gap(4).when(i > 0, (e) => e.pt(6).border_t_1().border_color(t.border))
          .child(this.entryHead(en.author, en.at, chips)
            .when(mine && !editing, (e) => e
              .child(div().flex_1())
              .child(this.button(`ed-${en.id}`, "Edit", () => this.openComposer({ mode: "edit", key: `edit:${en.id}`, editId: en.id }, en.body), { small: true }))
              .child(this.button(`del-${en.id}`, "Delete", (_e, cx) => cx.spawn(async (cx) => { await this.deleteDoc(en.id); cx.notify(); }), { small: true }))))
          .child(editing ? this.composerBox(t, 0) : TextView.markdown(`md-${en.id}`, en.body || "").selectable()),
      );
    });
    if (isFinding && local.suggestion) {
      card.child(v_flex().p(8).rounded(6).bg(`${t.ansi2}1a`).font_family(this.mono).text_size(12)
        .child(div().text_size(11).text_color(t.dim).child("Suggested change"))
        .children(String(local.suggestion).split("\n").map((l) => div().whitespace_nowrap().text_color(t.ansi2).child(`+ ${l}`.replace(/ /g, " ")))));
    }
    if (th.gh?.url) card.child(div().text_size(11).text_color(t.dim).child(th.gh.url));
    const replying = this.composer?.key === `thread:${th.key}`;
    if (replying) card.child(this.composerBox(t, 0));
    else {
      card.child(h_flex().gap(6).pt(2)
        .child(this.button(`rp-${th.key}`, "Reply", () => this.openComposer({ mode: "reply", key: `thread:${th.key}`, replyTo: th.local ? th.local.id : th.key }), { small: true }))
        .child(this.button(`rs-${th.key}`, th.resolved ? "Unresolve" : "Resolve", (_e, cx) => cx.spawn(async (cx) => {
          try {
            await this.toggleResolved(th);
          } catch (err) {
            notify({ title: "Could not change the thread", body: String(err.message ?? err), level: "error" });
          }
          cx.notify();
        }), { small: true }))
        .when(Boolean(th.path && th.line && th.side === "RIGHT"), (e) => e.child(this.button(`op-${th.key}`, "Open in nvim", () => this.openFile(th.path, th.line), { small: true }))));
    }
    return card;
  }

  composerBox(t, indent) {
    const c = this.composer;
    const a = c.anchor;
    const label =
      c.mode === "reply" ? "Reply" : c.mode === "edit" ? "Edit comment"
        : a.type === "general" ? "General comment"
          : a.type === "file" ? `Comment on ${base(a.path)}`
            : `Comment on ${a.side === "LEFT" ? "old " : ""}line${a.startLine ? `s ${a.startLine}–${a.line}` : ` ${a.line}`}`;
    const canSuggest = c.mode === "new" && a?.type === "line" && a.side === "RIGHT";
    return v_flex()
      .id("composer")
      .ml(indent)
      .mr(indent ? 12 : 0)
      .my(6)
      .p(8)
      .gap(6)
      .rounded(8)
      .border_1()
      .border_color(t.accent)
      .bg(t.background)
      // A press here must not move the keyboard away from the text
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .on_key_down((e, cx) => {
        if (e.keystroke === "cmd-enter" || e.keystroke === "ctrl-enter") this.saveComposer(cx);
        else if (e.key === "escape") {
          this.closeComposer();
          cx.notify();
        } else return;
        cx.stop_propagation();
      })
      .child(div().text_size(12).text_color(t.muted).child(label))
      .child(Textarea.new(c.state).w_full().text_size(13))
      .child(
        h_flex().gap(6).items_center()
          .child(div().flex_1().text_size(11).text_color(t.dim).child("Markdown · ⌘↵ saves a draft · Esc cancels"))
          .when(canSuggest, (e) => e.child(this.button("cm-suggest", "Suggest change", () => this.suggest(), { small: true })))
          .child(this.button("cm-cancel", "Cancel", () => this.closeComposer(), { small: true }))
          .child(this.button("cm-save", c.mode === "edit" ? "Save" : "Save draft", (_e, cx) => this.saveComposer(cx), { small: true, primary: true })),
      );
  }

  // ---- dialogs ---------------------------------------------------------------

  modal(t, panel) {
    return div().id("modal-backdrop").absolute().inset_0().bg("#00000066").flex().items_center().justify_center()
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .child(panel.on_mouse_down("left", (_e, cx) => cx.stop_propagation()));
  }

  githubPanel(t) {
    const g = this.gh;
    const panel = v_flex().id("gh-panel").w(560).max_h(560).p(16).gap(10).rounded(10).border_1().border_color(t.border).bg(t.popover)
      .child(h_flex().items_center().child(div().flex_1().text_size(16).font_semibold().child("GitHub"))
        .when(!g.busy, (e) => e.child(this.button("gh-close", "✕", () => { this.gh = null; }, { small: true }))));
    if (g.busy) return this.modal(t, panel.child(div().py(24).flex().justify_center().text_color(t.muted).child("Reading the PR from GitHub…")));
    if (g.error) return this.modal(t, panel.child(div().text_size(12).text_color(t.ansi1).child(g.error)));
    panel
      .child(div().text_size(12).text_color(t.muted).child(g.changes.length ? `${g.changes.length} change${g.changes.length === 1 ? "" : "s"} since the last build or check. The threads, the conversation and the PR box are new now.` : "Nothing new on GitHub."))
      .child(v_flex().id("gh-changes").max_h(320).overflow_y_scrollbar().gap(4).children(g.changes.map((c, i) => h_flex().gap(6).items_start().text_size(12).child(div().text_color(t.dim).child("•")).child(div().flex_1().min_w(0).child(c)))))
      .when(Boolean(g.rebuild), (e) => e
        .child(div().text_size(12).text_color(t.ansi3).child("The code is not the code of this review (new commits or changed files). New and changed hunks need Claude's notes."))
        .child(h_flex().justify_end().child(this.button("gh-rebuild", "Ask Claude to rebuild", () => this.askRebuild(), { primary: true }))));
    return this.modal(t, panel);
  }

  helpPanel(t) {
    const panel = v_flex().id("help-panel").w(440).p(16).gap(6).rounded(10).border_1().border_color(t.border).bg(t.popover)
      .child(h_flex().items_center().pb(4).child(div().flex_1().text_size(16).font_semibold().child("Keys")).child(this.button("help-close", "✕", () => { this.help = false; }, { small: true })))
      .children(KEYS.map(([k, what]) => h_flex().gap(10).text_size(12).child(div().w(70).flex_none().font_family(this.mono).text_color(t.accent).child(k)).child(div().text_color(t.muted).child(what))))
      .child(div().pt(6).text_size(11).text_color(t.dim).child("Click a line number to select it, shift-click for a range. Click a token with a definition to open it in nvim."));
    return this.modal(t, panel);
  }

  // ---- Finish review -------------------------------------------------------

  claudeSessions() {
    return agents().filter((a) => a.program?.key === "claude" || /claude/i.test(a.app ?? ""));
  }

  openFinish() {
    const sessions = this.claudeSessions();
    const session = sessions.find((a) => a.address === this.herdr.session)?.address ?? sessions[0]?.address ?? null;
    this.finish = {
      target: "claude",
      event: "COMMENT",
      body: TextareaState.new({ placeholder: "Optional. Shows at the top of the review on GitHub.", rows: 3 }),
      picked: new Map(),
      session,
      step: "pick",
      error: null,
    };
  }

  closeFinish() {
    this.finish?.body.release();
    this.finish = null;
    this.keys.focus();
  }

  // Items by group, as the page's Finish panel lists them.
  finishItems() {
    const F = this.finish;
    const mine = this.drafts();
    const findings = [];
    const gh = [];
    if (F.target === "github") {
      for (const th of this.threads) {
        const doc = th.local?.doc;
        if (doc?.kind === "finding" && !th.resolved && !["to_github", "posted"].includes(doc.status) && !doc.githubId) findings.push(th.local);
      }
      for (const g of this.data.github?.threads ?? []) {
        const over = this.docs.ghthreads.get(String(g.id))?.doc;
        if (over && !over.synced && Boolean(over.resolved) !== Boolean(g.resolved)) gh.push({ g, resolve: Boolean(over.resolved) });
      }
    }
    return { mine, findings, gh };
  }

  isPicked(key, group) {
    const F = this.finish;
    return F.picked.has(key) ? F.picked.get(key) : group !== "findings";
  }

  where(doc) {
    if (doc.replyTo) {
      if (doc.replyTo.startsWith("gh:")) return "Reply · GitHub thread";
      const root = this.docs.comments.get(doc.replyTo)?.doc;
      return `Reply · ${root ? this.where(root) : "deleted thread"}`;
    }
    const a = doc.anchor ?? {};
    if (a.type === "general") return "General";
    if (a.type === "file") return `${base(a.path)} · file`;
    return `${base(a.path)}:${a.startLine ? `${a.startLine}–` : ""}${a.line}`;
  }

  async submitFinish(cx) {
    const F = this.finish;
    const { mine, findings, gh } = this.finishItems();
    const ids = [...mine.filter((e) => this.isPicked(e.id, "mine")), ...findings.filter((e) => this.isPicked(e.id, "findings"))].map((e) => e.id);
    const ghPicked = gh.filter((x) => this.isPicked(`gh:${x.g.id}`, "gh"));
    F.error = null;
    if (F.target === "claude") {
      if (!ids.length || !F.session) return;
      F.step = "busy";
      F.busyText = "Saving the batch…";
      cx.notify();
      try {
        const batchId = await saveClaudeBatch(this, ids);
        // The batch is saved: the dialog closes, the message goes in the background
        this.closeFinish();
        this.deliver(batchId, F.session, cx);
      } catch (err) {
        F.step = "pick";
        F.error = String(err.message ?? err);
      }
      cx.notify();
      return;
    }
    F.step = "busy";
    F.busyText = "Making the GitHub plan (nothing is posted yet)…";
    cx.notify();
    try {
      const plan = await planGithub(this, ids, {
        event: F.event,
        body: F.body.value().trim(),
        resolveGithub: ghPicked.filter((x) => x.resolve).map((x) => x.g.id),
        unresolveGithub: ghPicked.filter((x) => !x.resolve).map((x) => x.g.id),
      });
      F.batchId = plan.batchId;
      F.planText = plan.text;
      F.planOk = plan.ok;
      F.step = "plan";
      if (!plan.ok) await cancelGithub(this, plan.batchId, "the plan failed");
    } catch (err) {
      F.step = "pick";
      F.error = String(err.message ?? err);
    }
    cx.notify();
  }

  deliver(batchId, session, cx) {
    cx.spawn(async (cx) => {
      try {
        await deliverToClaude(this, batchId, session);
        notify({ title: "Review sent to Claude", body: `Session ${session}. The replies show here when Claude writes them.`, level: "success" });
      } catch (err) {
        notify({ title: "Claude did not get the review", body: String(err.message ?? err), level: "warning", timeout: 12000 });
      }
      cx.notify();
    });
  }

  async post(cx) {
    const F = this.finish;
    F.step = "busy";
    F.busyText = "Posting to GitHub…";
    cx.notify();
    try {
      const res = await postGithub(this, F.batchId);
      this.closeFinish();
      notify({ title: res.failed ? "Posted with failures" : "Review posted to GitHub", body: res.note, level: res.failed ? "warning" : "success" });
      // The new GitHub threads: load the PR again
      this.checkGithub(cx);
    } catch (err) {
      F.step = "plan";
      F.planOk = false;
      F.error = String(err.message ?? err);
    }
    cx.notify();
  }

  async cancelPlan(cx) {
    const F = this.finish;
    try {
      if (F.planOk) await cancelGithub(this, F.batchId, "the person said no");
    } catch (err) {
      notify({ title: "Could not reset the drafts", body: String(err.message ?? err), level: "error" });
    }
    this.closeFinish();
    cx.notify();
  }

  radio(id, on, title, sub, onClick, disabled = false) {
    const t = this.t;
    return h_flex().id(id).gap(10).p(10).items_start().rounded(8).border_1().border_color(on ? t.accent : t.border)
      .when(disabled, (e) => e.opacity(0.45))
      .when(!disabled, (e) => e.cursor_pointer().hover((s) => s.bg(t.hover)).on_click(onClick))
      .child(div().flex_none().pt(1).text_color(on ? t.accent : t.dim).child(on ? "◉" : "○"))
      .child(v_flex().flex_1().min_w(0).gap(2).child(div().font_semibold().child(title)).child(div().text_size(12).text_color(t.muted).child(sub)));
  }

  check(id, on, what, text, onClick) {
    const t = this.t;
    return h_flex().id(id).gap(8).py(4).items_start().cursor_pointer().on_click(onClick)
      .child(div().flex_none().text_color(on ? t.accent : t.dim).child(on ? "☑" : "☐"))
      .child(v_flex().flex_1().min_w(0).child(div().text_size(11).text_color(t.dim).child(what)).child(div().text_size(12).truncate().child(text)));
  }

  finishPanel(t) {
    const F = this.finish;
    const pr = this.data.pr;
    const panel = v_flex().id("finish-panel").w(600).max_h(640).p(16).gap(12).rounded(10).border_1().border_color(t.border).bg(t.popover)
      .child(h_flex().items_center().child(div().flex_1().text_size(16).font_semibold().child("Finish your review"))
        .child(this.button("fin-close", "✕", (_e, cx) => { if (F.step === "plan") cx.spawn(async (cx) => this.cancelPlan(cx)); else this.closeFinish(); }, { small: true })));
    if (F.step === "busy") panel.child(div().py(30).flex().justify_center().text_color(t.muted).child(F.busyText));
    else if (F.step === "plan") {
      panel
        .child(div().text_size(12).text_color(t.muted).child(F.planOk ? "This is what github.py will post. Nothing is on GitHub yet." : "The plan failed. Nothing was posted; the comments are drafts again."))
        .child(v_flex().id("plan-text").max_h(380).overflow_y_scrollbar().p(10).rounded(6).bg(t.background).font_family(this.mono).text_size(12)
          .children(String(F.planText || "(no output)").split("\n").map((l) => div().child(l || " "))))
        .when(Boolean(F.error), (e) => e.child(div().text_size(12).text_color(t.ansi1).child(F.error)))
        .child(h_flex().justify_end().gap(8)
          .child(this.button("plan-no", F.planOk ? "Cancel (keep drafts)" : "Close", (_e, cx) => cx.spawn(async (cx) => this.cancelPlan(cx))))
          .when(F.planOk, (e) => e.child(this.button("plan-yes", "Post to GitHub", (_e, cx) => cx.spawn(async (cx) => this.post(cx)), { primary: true }))));
    } else {
      const { mine, findings, gh } = this.finishItems();
      const toggle = (key, group) => (_e, cx) => { F.picked.set(key, !this.isPicked(key, group)); cx.notify(); };
      const items = [];
      const group = (title, list) => { if (list.length) items.push(div().pt(6).text_size(11).font_semibold().text_color(t.dim).child(title.toUpperCase()), ...list); };
      group("Your drafts", mine.map((e) => this.check(`fi-${e.id}`, this.isPicked(e.id, "mine"), this.where(e.doc), excerpt(e.doc.body, 120), toggle(e.id, "mine"))));
      group("Claude's open findings, not on GitHub yet", findings.map((e) => this.check(`fi-${e.id}`, this.isPicked(e.id, "findings"), this.where(e.doc), `${SEVERITY[e.doc.severity]?.[0] ?? ""}: ${e.doc.title ?? ""}`, toggle(e.id, "findings"))));
      group("GitHub thread changes", gh.map((x) => this.check(`fi-gh-${x.g.id}`, this.isPicked(`gh:${x.g.id}`, "gh"), `${base(x.g.path)}${x.g.line ? `:${x.g.line}` : ""}`, `${x.resolve ? "Resolve" : "Unresolve"} the thread by @${x.g.comments?.[0]?.author ?? "?"}`, toggle(`gh:${x.g.id}`, "gh"))));
      const n = mine.filter((e) => this.isPicked(e.id, "mine")).length + findings.filter((e) => this.isPicked(e.id, "findings")).length;
      const ghN = gh.filter((x) => this.isPicked(`gh:${x.g.id}`, "gh")).length;
      const sessions = this.claudeSessions();
      const ready = F.target === "claude" ? n > 0 && Boolean(F.session) : n > 0 || ghN > 0 || F.event !== "COMMENT" || F.body.value().trim() !== "";
      const label = F.target === "claude" ? `Send ${n} comment${n === 1 ? "" : "s"} to Claude` : `Plan the review (${n})`;
      panel
        .child(this.radio("fin-claude", F.target === "claude", "Send to Claude", "Claude changes the code and replies in each thread.", (_e, cx) => { F.target = "claude"; cx.notify(); }))
        .child(this.radio("fin-github", F.target === "github", "Post to GitHub as a review", !pr ? "No PR: ask Claude to rebuild with the PR number." : `You see the plan first. Then it posts as one review on PR #${pr}.`, (_e, cx) => { F.target = "github"; cx.notify(); }, !pr));
      if (F.target === "claude") {
        panel.child(v_flex().gap(4)
          .child(div().text_size(11).font_semibold().text_color(t.dim).child("CLAUDE SESSION"))
          .when(!sessions.length, (e) => e.child(div().text_size(12).text_color(t.ansi3).child("No Claude session is running. Start one, then send.")))
          .children(sessions.map((a) => h_flex().id(`ses-${a.address}`).gap(8).px(8).py(5).rounded(6).cursor_pointer()
            .when(F.session === a.address, (e) => e.bg(t.selected))
            .hover((s) => s.bg(t.hover))
            .on_click((_e, cx) => { F.session = a.address; cx.notify(); })
            .child(div().flex_none().text_color(F.session === a.address ? t.accent : t.dim).child(F.session === a.address ? "◉" : "○"))
            .child(div().flex_1().min_w(0).truncate().text_size(12).child(`${a.space_name} › ${a.tab_name} · ${a.title || a.program?.name || a.address}`))
            .child(div().flex_none().text_size(11).text_color(t.dim).child(`${a.address}${a.address === this.herdr.session ? " · started this review" : ""}`)))));
      } else {
        panel.child(h_flex().gap(6).children(EVENTS.map(([value, text]) => this.button(`ev-${value}`, `${F.event === value ? "◉" : "○"} ${text}`, () => { F.event = value; }, { small: true }))))
          .child(Textarea.new(F.body).w_full().text_size(13));
      }
      panel
        .child(v_flex().id("fin-items").max_h(240).overflow_y_scrollbar().children(items.length ? items : [div().text_size(12).text_color(t.dim).child("No drafts. Click a line number to comment.")]))
        .when(Boolean(F.error), (e) => e.child(div().text_size(12).text_color(t.ansi1).child(F.error)))
        .child(h_flex().justify_end().gap(8)
          .child(this.button("fin-cancel", "Cancel", () => this.closeFinish()))
          .child(this.button("fin-submit", label, (_e, cx) => cx.spawn(async (cx) => this.submitFinish(cx)), { primary: true, disabled: !ready })));
    }
    return this.modal(t, panel);
  }
}
