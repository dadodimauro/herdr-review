// Finish review: a batch for a Claude session (hc send), or a GitHub review
// that this mod plans and posts itself with the skill's lib/code-review-diff/github.py,
// after the person approves the plan.

import { exec, readTextFile, writeTextFile } from "herdr";
import { newId, nowIso, runSkill, writeDoc } from "./data.js";

export const ME = "me";

// The batch document and the item updates. Returns the batch id.
async function makeBatch(r, target, ids, extra) {
  const id = newId("b");
  const batch = { v: 1, target, author: ME, createdAt: nowIso(), ids, status: "pending", ...extra };
  r.docs.batches.set(id, await writeDoc(r.folder, "batches", id, batch, null));
  const status = target === "github" ? "to_github" : "to_claude";
  for (const docId of ids) await r.updateDoc("comments", docId, { status, batch: id });
  return id;
}

// Saves the batch for Claude. Returns its id; deliverToClaude sends the message.
export function saveClaudeBatch(r, ids) {
  return makeBatch(r, "claude", ids, {});
}

// The skill reads the batch from the files: the message only names it.
// hc send waits while the session's input has a draft (or it waits for an
// answer), so it gets 10 s: after that the batch stays pending, to send again.
export async function deliverToClaude(r, batchId, session) {
  let res;
  try {
    res = await exec("hc", ["send", session, `/herdr-review batch ${r.folder} ${batchId}`], { timeout: 10000 });
  } catch {
    throw new Error(`${session} did not take the message in 10 s: its input may have a draft, or it waits for an answer. The batch is saved: clear the input, then Send again (Overview), or type /herdr-review sync there.`);
  }
  if (res.code !== 0) throw new Error(`hc send failed: ${(res.stderr || res.stdout).trim()}. The batch is saved: Send again from Overview.`);
  await r.updateDoc("batches", batchId, { sentTo: session, sentAt: nowIso() });
}

// The skill's github.py, from the review's checkout.
function github(r, args, timeout) {
  return runSkill(r, "lib/code-review-diff/github.py", args, timeout);
}

// Makes the batch and the plan (GitHub reads only). Returns { batchId, text, ok }.
export async function planGithub(r, ids, { event, body, resolveGithub, unresolveGithub }) {
  const extra = { event, body };
  if (resolveGithub.length) extra.resolveGithub = resolveGithub;
  if (unresolveGithub.length) extra.unresolveGithub = unresolveGithub;
  const batchId = await makeBatch(r, "github", ids, extra);
  const dir = `${r.folder}/batch-${batchId}`;
  await exec("mkdir", ["-p", dir]);
  // Only the owner writes in this mod: the one name is the GitHub login, for the plan's text
  const login = await exec("gh", ["api", "user", "--jq", ".login"]).catch(() => null);
  const name = login && login.code === 0 ? login.stdout.trim() : "me";
  await writeTextFile(`${dir}/names.json`, `${JSON.stringify({ [ME]: name })}\n`, null);
  const res = await github(r, ["plan", "--pr", String(r.data.pr), "--docs", `${r.folder}/db`, "--batch", batchId,
    "--names", `${dir}/names.json`, "--owner", ME, "--out", `${dir}/plan.json`], 120000);
  const text = [res.stdout.trim(), res.stderr.trim()].filter(Boolean).join("\n");
  return { batchId, text, ok: res.code === 0 };
}

// The person said no, or the plan failed: the items are drafts again.
export async function cancelGithub(r, batchId, reason) {
  const entry = r.docs.batches.get(batchId);
  for (const docId of entry?.doc.ids ?? []) await r.updateDoc("comments", docId, { status: "draft" }, ["batch"]);
  await r.updateDoc("batches", batchId, { status: "failed", note: `Not posted: ${reason}`, updatedAt: nowIso() });
}

// Posts the approved plan, then writes the result back (GITHUB.md step 7).
export async function postGithub(r, batchId) {
  const dir = `${r.folder}/batch-${batchId}`;
  await r.updateDoc("batches", batchId, { status: "working", updatedAt: nowIso() });
  const res = await github(r, ["post", "--plan", `${dir}/plan.json`, "--out", `${dir}/result.json`], 300000);
  const resultText = await readTextFile(`${dir}/result.json`).catch(() => null);
  if (!resultText) {
    const error = (res.stderr || res.stdout).trim().split("\n").pop() || "github.py post failed";
    await cancelGithub(r, batchId, error);
    throw new Error(error);
  }
  const result = JSON.parse(resultText);
  const plan = JSON.parse((await readTextFile(`${dir}/plan.json`)) ?? "{}");
  const already = new Set((plan.skipped ?? []).filter((s) => String(s.reason).startsWith("already on GitHub")).map((s) => s.doc));
  const ids = r.docs.batches.get(batchId)?.doc.ids ?? [];
  let posted = 0;
  for (const docId of ids) {
    const p = result.posted?.[docId];
    if (p) {
      posted += 1;
      await r.updateDoc("comments", docId, { status: "posted", githubUrl: p.url, ...(p.id ? { githubId: p.id } : {}) });
    } else if (already.has(docId)) {
      await r.updateDoc("comments", docId, { status: "posted" });
    } else {
      await r.updateDoc("comments", docId, { status: "draft" }, ["batch"]);
    }
  }
  for (const ghId of [...(result.resolved ?? []), ...(result.unresolved ?? [])]) {
    if (r.docs.ghthreads.has(String(ghId))) await r.updateDoc("ghthreads", String(ghId), { synced: true });
  }
  const failed = result.failed ?? [];
  const note = [result.reviewUrl, `${posted} posted`, failed.length ? `${failed.length} failed: ${failed.map((f) => f.error).join("; ")}` : ""]
    .filter(Boolean)
    .join(" · ");
  await r.updateDoc("batches", batchId, { status: failed.length ? "failed" : "done", note, updatedAt: nowIso() });
  return { note, failed: failed.length, url: result.reviewUrl };
}
