// Diff lines: numbers, syntax colors, LSP tokens, unified or split rows.

import { div } from "gpui-kit";
import { h_flex } from "gpui-base";

export const ROW = 20;
export const GUTTER = 46;

// The rows of a hunk: { kind: "add" | "del" | "ctx", text, old, new, side, n }.
// side/n are where a comment goes: LEFT and the old number for a removed
// line, RIGHT and the new number for the others (as on GitHub).
export function hunkRows(hunk) {
  let oldNo = hunk.oldStart;
  let newNo = hunk.newStart;
  return hunk.lines.map(([kind, text]) => {
    const row = { kind, text, old: null, new: null };
    if (kind !== "add") row.old = oldNo++;
    if (kind !== "del") row.new = newNo++;
    row.side = kind === "del" ? "LEFT" : "RIGHT";
    row.n = kind === "del" ? row.old : row.new;
    return row;
  });
}

// Split view pairs: context lines face themselves, a run of removed lines
// faces the run of added lines after it. [left | null, right | null].
export function splitPairs(rows) {
  const out = [];
  for (let i = 0; i < rows.length; ) {
    if (rows[i].kind === "ctx") {
      out.push([rows[i], rows[i]]);
      i += 1;
      continue;
    }
    const dels = [];
    const adds = [];
    while (i < rows.length && rows[i].kind === "del") dels.push(rows[i++]);
    while (i < rows.length && rows[i].kind === "add") adds.push(rows[i++]);
    for (let k = 0; k < Math.max(dels.length, adds.length); k++) out.push([dels[k] ?? null, adds[k] ?? null]);
  }
  return out;
}

// build.py's syntax kinds, in the theme's terminal colors.
function kindColor(kind, t) {
  switch (kind) {
    case "k": return t.ansi5;
    case "s": return t.ansi2;
    case "c": return t.dim;
    case "n": return t.ansi3;
    case "b": return t.ansi6;
    case "f": return t.ansi4;
    case "t": return t.ansi3;
    case "d": return t.ansi6;
    default: return null;
  }
}

// [{ text, color, tok }] pieces of a line: syntax spans ([col, len, kind])
// and LSP tokens ([col, len, hover, definition, references]) in UTF-16
// columns, the same units as JS string indexes. tok: index in `tokens` or -1.
function pieces(text, spans, tokens, t) {
  if (!spans?.length && !tokens?.length) return [{ text, color: null, tok: -1 }];
  const kinds = new Array(text.length).fill("");
  for (const [col, len, kind] of spans ?? []) for (let i = Math.max(0, col); i < Math.min(col + len, text.length); i++) kinds[i] = kind;
  const owner = new Array(text.length).fill(-1);
  (tokens ?? []).forEach(([col, len], ti) => {
    if (!/^\w+$/.test(text.slice(col, col + len))) return;
    for (let i = col; i < col + len; i++) if (owner[i] === -1) owner[i] = ti;
  });
  const out = [];
  for (let i = 0; i < text.length; ) {
    let j = i + 1;
    while (j < text.length && kinds[j] === kinds[i] && owner[j] === owner[i]) j++;
    out.push({ text: text.slice(i, j), color: kindColor(kinds[i], t), tok: owner[i] });
    i = j;
  }
  return out;
}

// The tooltip of a token: its hover text, where it is defined, its uses.
function tokenTip(token, lsp) {
  const [, , h, d, r] = token;
  const parts = [];
  if (h >= 0 && lsp.hovers?.[h]) {
    // The signature and the start of the docs: a tooltip is not a page
    const lines = lsp.hovers[h].split("\n").filter((l) => !/^-{3,}$/.test(l));
    const text = lines.slice(0, 14).join("\n");
    parts.push(text.length > 700 ? `${text.slice(0, 699)}…` : lines.length > 14 ? `${text}\n…` : text);
  }
  const def = d >= 0 ? lsp.definitions?.[d] : null;
  if (def) parts.push(`Defined in ${def.path}:${def.line}${def.inRepo ? " (⌘-click: open in nvim)" : ""}`);
  const uses = r >= 0 ? lsp.references?.[r] : null;
  if (uses?.length) parts.push(`${uses.length} use${uses.length === 1 ? "" : "s"}: ${uses.slice(0, 5).map((u) => `${u.path}:${u.line}`).join(", ")}`);
  return parts.join("\n");
}

const SIGN = { add: "+", del: "-", ctx: " " };
const nb = (s) => s.replace(/ /g, " ");

// The code of one line. With wrap, words are separate pieces so that a long
// line breaks at its spaces.
function code(row, ctx, side) {
  const { t } = ctx;
  const els = [];
  pieces(row.text, ctx.spans(row, side), ctx.tokens(row, side), t).forEach((p, i) => {
    if (p.tok >= 0) {
      const token = ctx.tokens(row, side)[p.tok];
      const tip = tokenTip(token, ctx.lsp);
      let el = div().id(`tk-${ctx.key}-${side}-${i}`).flex_none().cursor_pointer().hover((s) => s.underline()).child(nb(p.text));
      if (p.color) el = el.text_color(p.color);
      if (tip) el = el.tooltip(tip);
      // Cmd-click (Ctrl-click elsewhere) opens the definition; a double click still opens the line
      els.push(el.on_click((e, cx) => {
        if (e.modifiers?.platform && e.click_count === 1) ctx.onToken(token, cx);
      }));
      return;
    }
    const chunks = ctx.wrap ? p.text.split(/( +)/).filter(Boolean) : [p.text];
    for (const chunk of chunks) {
      const el = div().flex_none().child(nb(chunk) || " ");
      els.push(p.color ? el.text_color(p.color) : el);
    }
  });
  return h_flex()
    .id(`code-${ctx.key}-${side}`)
    .flex_1()
    .min_w(0)
    .pr(16)
    .text_color(t.foreground)
    // A double click opens the line in nvim
    .on_click((e, cx) => {
      if (e.click_count >= 2) ctx.onOpen(row, side, cx);
    })
    .when(ctx.wrap, (e) => e.flex_wrap())
    .when(!ctx.wrap, (e) => e.whitespace_nowrap())
    .children(els.length ? els : [div().child(" ")]);
}

function tints(kind, t) {
  if (kind === "add") return [`${t.ansi2}22`, `${t.ansi2}33`, t.ansi2];
  if (kind === "del") return [`${t.ansi1}22`, `${t.ansi1}33`, t.ansi1];
  return [null, null, t.dim];
}

function gutter(id, n, row, ctx, gutterTint) {
  const { t } = ctx;
  return div()
    .id(id)
    .w(GUTTER)
    .flex_none()
    .self_stretch()
    .min_h(ROW)
    .pr(8)
    .flex()
    .justify_end()
    .text_color(t.dim)
    .cursor_pointer()
    .when(Boolean(gutterTint), (e) => e.bg(gutterTint))
    .hover((s) => s.bg(t.accent).text_color(t.accent_foreground))
    .on_click((e, cx) => ctx.onGutter(row, e, cx))
    .child(n === null ? "" : String(n));
}

function frame(id, ctx) {
  return h_flex()
    .id(id)
    .w_full()
    .min_h(ROW)
    .flex_none()
    .items_start()
    .font_family(ctx.mono)
    .text_size(12)
    .line_height(`${ROW}px`);
}

// One unified line. ctx: { t, mono, key, wrap, lsp, spans(row, side),
// tokens(row, side), selected(row), onGutter(row, e, cx), onToken(token, cx),
// onOpen(row, side, cx) }.
export function lineRow(row, ctx) {
  const [tint, gutterTint, signColor] = tints(row.kind, ctx.t);
  return frame(`row-${ctx.key}`, ctx)
    .when(Boolean(tint), (e) => e.bg(tint))
    .when(ctx.selected(row), (e) => e.bg(ctx.t.selection))
    .child(gutter(`go-${ctx.key}`, row.old, row, ctx, gutterTint))
    .child(gutter(`gn-${ctx.key}`, row.new, row, ctx, gutterTint))
    .child(div().w(16).flex_none().text_color(signColor).pl(4).child(SIGN[row.kind]))
    .child(code(row, ctx, row.side));
}

// One split pair: the old side on the left, the new side on the right.
export function splitRow(left, right, ctx) {
  const half = (row, side) => {
    const shown = row && (side === "LEFT" ? row.old !== null : row.new !== null) ? row : null;
    if (!shown) return div().flex_1().min_w(0).self_stretch().bg(`${ctx.t.dim}12`);
    const kind = shown.kind === "ctx" ? "ctx" : side === "LEFT" ? "del" : "add";
    const [tint, gutterTint, signColor] = tints(kind, ctx.t);
    // A context line is commented on its RIGHT side, as in the unified view
    const target = shown.kind === "ctx" ? shown : shown;
    return h_flex()
      .flex_1()
      .min_w(0)
      .self_stretch()
      .items_start()
      .when(Boolean(tint), (e) => e.bg(tint))
      .when(ctx.selected(target), (e) => e.bg(ctx.t.selection))
      .child(gutter(`g${side[0]}-${ctx.key}`, side === "LEFT" ? shown.old : shown.new, target, ctx, gutterTint))
      .child(div().w(16).flex_none().text_color(signColor).pl(4).child(SIGN[kind]))
      .child(code(shown, ctx, side));
  };
  return frame(`row-${ctx.key}`, ctx)
    .child(half(left, "LEFT"))
    .child(div().w(1).self_stretch().bg(ctx.t.border))
    .child(half(right, "RIGHT"));
}

// The lines a comment snippet keeps: up to 3 lines above the line, on the
// same side, from the hunk rows.
export function snippetFor(rows, side, start, line) {
  const num = (r) => (side === "LEFT" ? r.old : r.new);
  const sideRows = rows.filter((r) => num(r) !== null);
  const first = Math.max(1, start - 3);
  const lines = sideRows.filter((r) => num(r) >= first && num(r) <= line);
  if (!lines.length) return null;
  return { start: num(lines[0]), lines: lines.map((r) => r.text) };
}
