// Mouse reports Codex typed into its own box (SHEP-13). Codex turns on mouse reports for its box (modes 1000, 1002,
// 1003, 1006), so a view of its terminal sends it every wheel move as "ESC [ < button ; column ; row M". When a
// report's ESC reaches Codex apart from the rest of it (measured with Codex 0.169: 10 ms apart is still read as one,
// 20 ms is not), Codex takes the ESC for the Esc key and types the rest, "[<65;101;61M", into its box. A send pasted
// after it went out with it: "[<64;101;61M[<65;101;61M/model" reached the agent as a message, and Herdr took the box
// for the person's unsent draft. Claude Code reads a split report as one, so only Codex's box needs this.
//
// Before a send to Codex: one key it ignores (F12) waits for a quiet moment (hc presses keys ~3 s after the person's
// last key, a mouse report's included), so a report still coming lands before the box is read, not after; then
// reports that are all the box holds are deleted. A box with any other text is the person's draft and is never
// touched. herdr-task's own sends do the same (mods/tasks/bin/herdr_task/news.py's settle).
//
// Where the reports come from (SHEP-14): herdr-core reads a full-screen app's history (`hc read <t> --lines N`, N > 0)
// by scrolling it, with wheel reports at its screen's middle ("[<65;101;61M" on a 200x120 terminal, every leaked one
// so far), even when the read gives up and returns the screen alone. Codex runs full screen with mouse reports on, so
// each such read sends it a burst (one of 2000 lines of a long session: hundreds), and hc's wait for a quiet moment
// before a send starts over with each. So Codex is never read with history here (historyLines): its screen is all.
// Shared core: the source is core/mouse-leak.js; bin/sync-shared copies it into the mods in lib/COPIES.

/** One report as Codex types it, its ESC taken as a key: "[<button;column;row" then M (press) or m (release). */
export const REPORT = "\\[<\\d{1,3};\\d{1,4};\\d{1,4}[Mm]";
const ONLY = new RegExp(`^\\s*(?:${REPORT}\\s*)+$`);
const ANY = new RegExp(REPORT);
const ALL = new RegExp(REPORT, "g");
/** A key Codex's box ignores (no text, no cursor move; checked with 0.169): pressed only to wait for quiet. */
export const QUIET_KEY = "f12";

/** The text without reports Codex typed; anything else as it was. */
export const stripReports = (text) => String(text ?? "").replace(ALL, "");
/** Whether the text is reports alone (whitespace between them aside). */
export const onlyReports = (text) => ONLY.test(String(text ?? ""));
/** Whether the text holds a report. */
export const hasReport = (text) => ANY.test(String(text ?? ""));

/** Whether the terminal (hc get's record) runs Codex. */
export const isCodex = (t) => (t?.status?.app ?? t?.foreground?.name ?? t?.labels?.["herdr.agent"]) === "codex";

/** How many lines of history to read (`hc read <t> --lines`) from a terminal: n, or none from Codex (reading its
 *  history scrolls it with wheel reports; above). t: its hc get record, or "codex" when the caller knows the CLI. */
export const historyLines = (t, n) => (t === "codex" || isCodex(t) ? 0 : n);

/** Codex's box on its screen (`hc read <t> --lines 0`): its text, its wrapped lines joined, or null when the screen
 *  shows no box (a picker, a question, still starting). The box is the last line starting with "›", near the bottom
 *  (only its footer under it); a long text wraps onto lines indented by two spaces. Its dim placeholder reads as
 *  text here: it never holds a report, so it is never mistaken for one. */
export function codexBox(screen) {
  const lines = String(screen ?? "").split("\n").filter((l) => !/^\[hc\]/.test(l));
  let at = -1;
  for (let i = lines.length - 1; i >= 0; i--) if (/^›(?: |$)/.test(lines[i])) { at = i; break; }
  if (at < 0) return null;
  let end = at + 1, text = lines[at].replace(/^›\s?/, "");
  while (end < lines.length && /^ {2}\S/.test(lines[end])) { text += lines[end].slice(2); end++; }
  // only the footer under it: the model line, the shortcuts, a warnings line
  if (lines.slice(end).filter((l) => l.trim()).length > 5) return null;
  return text.replace(/\s+$/, "");
}

/** The keys that delete a box of reports alone: End, then a backspace per character. null for any other box. */
export function deleteKeys(box) {
  const text = String(box ?? "");
  return onlyReports(text) ? ["end", ...Array([...text].length).fill("backspace")] : null;
}

/** Readies a Codex terminal's box for a send: waits for quiet, then deletes reports that are all it holds.
 *  io: { hc(args) -> { code, stdout } } (`read <t> --lines 0`, `keys <t> ...`). Resolves true when the box is clear
 *  of reports (or shows none), false when reports sit among other text (the person's draft: left as it is). */
export async function settle(io, addr, tries = 3) {
  const read = async () => {
    const r = await io.hc(["read", addr, "--lines", "0"]);
    return r && r.code === 0 ? codexBox(r.stdout) : null;
  };
  if ((await read()) === null) return true;
  await io.hc(["keys", addr, QUIET_KEY]);
  for (let i = 0; i < tries; i++) {
    const box = await read();
    if (box === null || !hasReport(box)) return true;
    const keys = deleteKeys(box);
    if (!keys) return false;
    await io.hc(["keys", addr, ...keys]);
  }
  const box = await read();
  return box === null || !hasReport(box);
}

/** settle() for a terminal that may not be Codex: get(addr) -> its hc get record (or null). Never throws: a check
 *  that fails leaves the send as it was. */
export async function settleIfCodex(io, addr, get) {
  try {
    if (!isCodex(await get(addr))) return true;
    return await settle(io, addr);
  } catch {
    return true;
  }
}
