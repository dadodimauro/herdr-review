// Opens a file at a line in Neovim: in the tab's Neovim when one runs (the
// same socket name as the my-files mod, so both use one Neovim per tab), else
// in a new Neovim to the right.

import { exec, focused, run, terminals } from "herdr";

const nvimSocket = (tab) => `/tmp/herdr-nvim-${String(tab).replace(/[^A-Za-z0-9_-]/g, "_")}.sock`;

// A Vim string in single quotes: a quote is written twice.
const vimString = (s) => `'${String(s).replace(/'/g, "''")}'`;

export async function openInNvim(file, root, line = 1) {
  const tab = focused()?.tab;
  if (!tab) throw new Error("No tab has the focus.");
  const sock = nvimSocket(tab);
  const alive = await exec("nvim", ["--server", sock, "--remote-expr", "1"], { timeout: 3000 })
    .then((r) => r.code === 0 && r.stdout.trim() === "1")
    .catch(() => false);
  if (alive) {
    // Normal mode first, then :edit at the line (fnameescape keeps odd file names safe)
    const keys = `<C-\\><C-N>:execute 'edit +${Number(line) || 1} ' . fnameescape(${vimString(file)})<CR>`;
    const r = await exec("nvim", ["--server", sock, "--remote-send", keys]);
    if (r.code !== 0) throw new Error(r.stderr.trim() || "nvim --remote-send failed");
    const term = terminals().find((t) => JSON.stringify(t.argv ?? []).includes(sock));
    if (term) run("pane.focus", { terminal: term.address });
    return;
  }
  // A socket file that a stopped Neovim left would make --listen fail
  await exec("rm", ["-f", sock]);
  run("terminal.open", { argv: ["nvim", "--listen", sock, `+${Number(line) || 1}`, file], folder: root, place: "right" });
}
