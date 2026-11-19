// Answers to what an agent asks through its PermissionRequest hook (mods/chat/ask-hook.sh) or Cursor's bridge
// (herdr-task cursor-chat): each request waits in data/chat-asks/req/<id>.json, its id <terminal>.<seconds>.<pid>,
// until an answer lands in ans/<id>.json. The one writer every window copy of a card uses (the chat's cards, a
// worker's card in its Assistant's chat, a permission's toast): first answer wins. An answer goes only while its
// request still waits (its file is there, its hook or bridge alive, no answer yet), and never over another one: it is
// linked into place, which fails when one is there already. How it went (end/<id>, "Allowed in the chat") goes with it,
// for every other copy of the card.
// Shared: the source is lib/answers.js; bin/sync-shared copies it into each mod in lib/COPIES.
// Edit lib/answers.js, never a mod's copy, then run bin/sync-shared.

import { exec, appHome } from "herdr";

export const ASKS_DIR = () => `${appHome()}/data/chat-asks`;
// A request's id as the hook and the bridge make it: never a path.
export const askId = (id) => typeof id === "string" && /^[\w.-]+$/.test(id) && !id.includes("..") && id.split(".").length >= 3;

// $1 the folder, $2 the id, $3 how it went ("" for none), $4 the answer ("" is no decision: the terminal asks).
// Exit 0 sent, 3 no longer waiting, 4 answered already, 5 couldn't write.
export const ANSWER_ONCE = `d=$1; id=$2; req="$d/req/$id.json"; ans="$d/ans/$id.json"
[ -f "$req" ] || exit 3
kill -0 "\${id##*.}" 2>/dev/null || exit 3
[ -e "$ans" ] && exit 4
mkdir -p "$d/ans" "$d/end" || exit 5
tmp="$d/ans/.$id.$$.tmp"
printf '%s' "$4" > "$tmp" || { rm -f "$tmp"; exit 5; }
if ln "$tmp" "$ans" 2>/dev/null; then rm -f "$tmp"; else rm -f "$tmp"; exit 4; fi
[ -z "$3" ] || { printf '%s\\n' "$3" > "$d/end/$id.tmp" && mv "$d/end/$id.tmp" "$d/end/$id"; }
exit 0`;

// -> "sent" | "gone" (no longer waiting) | "taken" (answered already) | "failed".
export async function answerOnce(id, text, how, dir = ASKS_DIR()) {
  if (!askId(id)) return "gone";
  const r = await exec("sh", ["-c", ANSWER_ONCE, "x", dir, id, how ?? "", String(text ?? "")]).catch(() => null);
  return r?.code === 0 ? "sent" : r?.code === 3 ? "gone" : r?.code === 4 ? "taken" : "failed";
}

// The chat's way (a card's answer, already closed on its card): the same, its outcome not needed.
export const answerAsk = (dir, id, text, how) => answerOnce(id, text, how, dir);

// Which of these requests still wait (their file there, their hook or bridge alive, no answer yet).
export async function stillWaiting(ids, dir = ASKS_DIR()) {
  const ok = ids.filter(askId);
  if (!ok.length) return new Set();
  const r = await exec("sh", ["-c", 'd=$1; shift; for id; do [ -f "$d/req/$id.json" ] && [ ! -e "$d/ans/$id.json" ] && kill -0 "${id##*.}" 2>/dev/null && echo "$id"; done; true', "x", dir, ...ok]).catch(() => null);
  if (!r || r.code !== 0) return null;
  return new Set(String(r.stdout ?? "").split("\n").filter(Boolean));
}
