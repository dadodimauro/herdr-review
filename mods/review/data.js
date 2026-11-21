// Files of one review folder: review.json (the page data that extract.py
// wrote), herdr.json (scripts and the Claude session), notes.json (the
// checkout) and db/<collection>/<id>.json (one file per document, the layout
// that github.py --docs reads).

import { exec, readTextFile, writeTextFile } from "herdr";

export const COLLECTIONS = ["comments", "batches", "ghthreads"];

// readTextFile stops at 1 MiB: a larger file comes through cat.
async function readText(path) {
  try {
    const text = await readTextFile(path);
    if (text !== null) return text;
  } catch {
    // Too large, or not readable: try cat below
  }
  const r = await exec("cat", [path]);
  if (r.code !== 0) throw new Error(r.stderr.trim() || `cannot read ${path}`);
  return r.stdout;
}

export async function readJson(path, fallback) {
  try {
    return JSON.parse(await readText(path));
  } catch (err) {
    if (fallback !== undefined) return fallback;
    throw err;
  }
}

// Every document in one exec: a marker line "<<<review-db <collection>/<id>.json>>>", then the text.
const LIST_SCRIPT = `cd "$1/db" 2>/dev/null || exit 0
for f in comments/*.json batches/*.json ghthreads/*.json; do
  [ -f "$f" ] || continue
  printf '\\n<<<review-db %s>>>\\n' "$f"
  cat "$f"
done`;

// Returns { comments: Map, batches: Map, ghthreads: Map }. Each value is
// { id, doc, raw }: raw is the file text, which a write must match.
export async function loadDocs(folder) {
  const out = Object.fromEntries(COLLECTIONS.map((c) => [c, new Map()]));
  const r = await exec("sh", ["-c", LIST_SCRIPT, "review-db", folder]);
  if (r.code !== 0) throw new Error(r.stderr.trim() || "cannot list db");
  const parts = r.stdout.split(/\n<<<review-db ([a-z]+\/[^\n]+?)>>>\n/);
  for (let i = 1; i + 1 < parts.length; i += 2) {
    const [collection, file] = parts[i].split("/");
    const raw = parts[i + 1];
    try {
      out[collection]?.set(file.replace(/\.json$/, ""), { id: file.replace(/\.json$/, ""), doc: JSON.parse(raw), raw });
    } catch {
      // A file that another writer has not finished: the next change loads it
    }
  }
  return out;
}

// Writes one document. `entry` is what loadDocs gave (null for a new
// document). Rejects when the file changed since that read.
export async function writeDoc(folder, collection, id, doc, entry) {
  const raw = `${JSON.stringify(doc, null, 2)}\n`;
  await writeTextFile(`${folder}/db/${collection}/${id}.json`, raw, entry ? entry.raw : null);
  return { id, doc, raw };
}

export function newId(prefix) {
  const rand = Math.random().toString(36).slice(2, 8);
  return `${prefix}-${Date.now().toString(36)}${rand}`;
}

export const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");

// Runs a Python script of the herdr-review skill (herdr.json "skill"). The skill needs no project: uv gives it
// Pygments (syntax colors) and a Python of its own. It runs in the review's checkout.
export function runSkill(r, script, args, timeout) {
  const { skill } = r.herdr ?? {};
  if (!skill) throw new Error("herdr.json names no skill. Ask Claude to run /herdr-review again.");
  return exec("uv", ["run", "--no-project", "--with", "pygments", "python", `${skill}/${script}`, ...args], {
    cwd: r.notes?.build?.checkout || r.folder,
    timeout,
  });
}
