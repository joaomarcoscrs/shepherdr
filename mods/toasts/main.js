// Our toasts (a clone of Herdr's shipped toast, which only Herdr's notify() reaches): a float over
// the work area, bottom right ({ "floating": [{ "content": { "mod": "toasts" }, "edge": "bottom",
// "align": "end" }] } in the layout). Mods post with toast() from ui.js, which keeps them in
// shared "toasts" (newest last); this draws the last three. Each: a dot (red for an alert or an
// error, amber a warning, green done, the accent otherwise), its title on one line, its body
// wrapped to at most 3 lines, and an X on hover. A click runs its action (going to the agent)
// and closes it. Each goes after its timeout; never while the pointer is on them.
//
// A permission's toast (SHEP-19: n.ask, a waiting request's id, no more) offers its chat card's choices when what is
// allowed fits whole (core/toasts.js askToast(): Allow or Allow once, Deny, Cursor's Allow always where its card has
// it), else a Review that opens the card; an agent's other Needs you has Open chat. A choice is only ever a click on
// its own button, armed once it has shown a moment: never the toast's body (that opens the chat), its X, its timeout,
// a key. Just before it goes, the request is read again: the same request, still waiting, still offering that choice;
// the answer is the card's own (core/asks.js), written once by lib/answers.js (first answer wins). While the pointer is
// on the toasts nothing moves: a new toast waits, an answered or gone one stays in place, saying so.

import { View, div, svg } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { placement, run, shared, setShared, theme, readTextFile, settings, spaces, panes } from "herdr";
import { note } from "./usage.js";
import { TOASTS, TEXT, RADIUS, red, amber, green, button } from "./ui.js";
// How many, how wide, how long and in which color: core/toasts.js, shared with the web app's toasts.
import { SHOWN, WIDTH, LINES, BODY_MAX, ASK_ARM_MS, tone, clip, unexpired, askToast, askAnswer } from "./core/toasts.js";
import { words, howAnswered } from "./core/asks.js";
import { ASKS_DIR, askId, answerOnce, stillWaiting } from "./answers.js";

const LINE = 18;    // the body's line height: LINES (3) lines at most
const MONO = 16;    // an exact line's height (a command, a path)

const color = (level, t) => ({ red: red(), amber: amber(), green: green() }[tone(level)] ?? t.accent);
// What a toast's choice came to, in its place.
const ENDED = { gone: "No longer waiting", taken: "Answered already", changed: "It changed: review it in its chat" };

export default class Toasts extends View {
  init(_props, cx) {
    this.hovered = null;
    // A permission toast's request as read: toast id -> { id, local, text, req, view, state, msg, shownAt }.
    this.asks = new Map();
    // What shows while the pointer is on the toasts (their ids): nothing moves under it.
    this.frozen = null;
    // Usage insights (lib/usage.js): each error toast shown, once (its kind only, never its words).
    this.seen = new Set();
    // Expiry: once a second, and only writes when one goes.
    cx.timer.every(1000, (cx) => {
      const list = shared(TOASTS) ?? [];
      for (const n of list) if (n.level === "error" && !this.seen.has(n.id)) { this.seen.add(n.id); note("error", { type: "toast" }); }
      for (const id of this.seen) if (!list.some((n) => n.id === id)) this.seen.delete(id);
      this.checkAsks(list, cx);
      if (placement()?.hovered) return;
      // Answered here, or no longer waiting: gone once the pointer has left. One that changed still waits: it stays its
      // time, with Open chat.
      const left = unexpired(list).filter((n) => !["sent", "gone", "taken"].includes(this.asks.get(n.id)?.state));
      if (left.length !== list.length) setShared(TOASTS, left);
      for (const id of this.asks.keys()) if (!left.some((n) => n.id === id)) this.asks.delete(id);
    });
  }

  dismiss(id) {
    setShared(TOASTS, (shared(TOASTS) ?? []).filter((n) => n.id !== id));
  }

  // ---- a permission's request -----------------------------------------------------------------

  // Its state, read once (this machine's only: another machine's opens its chat).
  ask(n, cx) {
    if (!n.ask?.id) return null;
    let s = this.asks.get(n.id);
    if (s && s.id === n.ask.id) return s;
    const local = !n.ask.machine && askId(n.ask.id);
    s = { id: n.ask.id, local, text: null, req: null, view: local ? null : { kind: "review", reason: n.body ?? "" }, state: null, msg: null, shownAt: 0 };
    this.asks.set(n.id, s);
    if (local) cx.timer.after(0, (cx) => cx.spawn(async (cx) => {
      const text = await readTextFile(`${ASKS_DIR()}/req/${s.id}.json`).catch(() => null);
      if (text == null) { s.state = "gone"; s.msg = ENDED.gone; s.view = { kind: "review", reason: n.body ?? "" }; cx.notify(); return; }
      let req = null;
      try { req = JSON.parse(text); } catch {}
      s.text = text;
      s.req = req;
      s.view = askToast(req);
      cx.notify();
    }));
    return s;
  }

  // Requests answered elsewhere (the chat's card, the terminal, the web) or ended: said in place, then gone.
  checkAsks(list, cx) {
    const open = list.map((n) => [n, this.asks.get(n.id)]).filter(([, s]) => s?.local && s.text != null && !s.state);
    if (!open.length || this.checking) return;
    this.checking = true;
    cx.spawn(async (cx) => {
      const waiting = await stillWaiting(open.map(([, s]) => s.id)).catch(() => null);
      this.checking = false;
      if (!waiting) return;
      let changed = false;
      for (const [, s] of open) if (!s.state && !waiting.has(s.id)) { s.state = "gone"; s.msg = ENDED.gone; changed = true; }
      if (changed) cx.notify();
    });
  }

  // A choice pressed: only on its own button, once it has shown a moment, once.
  choose(n, s, choice, cx) {
    if (s.state || s.view?.kind !== "choices" || !s.shownAt || Date.now() - s.shownAt < ASK_ARM_MS) return;
    s.state = "sending";
    s.msg = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await this.send(s, choice).catch(() => "failed");
      if (r.sent) { s.state = "sent"; s.msg = r.how ?? "Sent"; }
      else if (r === "failed") { s.state = null; s.msg = "It didn't go. Try again, or answer in its chat."; s.shownAt = Date.now(); }
      else { s.state = r; s.msg = ENDED[r] ?? ENDED.gone; }
      cx.notify();
    });
  }

  // The answer, as the card's: the same request read again (the same file, word for word), still offering this choice,
  // written once. -> { sent, how } | "gone" | "taken" | "changed" | "failed".
  async send(s, choice) {
    const text = await readTextFile(`${ASKS_DIR()}/req/${s.id}.json`).catch(() => null);
    if (text == null) return "gone";
    if (text !== s.text) return "changed";
    let req = null;
    try { req = JSON.parse(text); } catch { return "changed"; }
    const answer = askAnswer(req, choice);
    if (answer == null) return "changed";
    const how = howAnswered({ words: words(req) }, answer, "from a notification");
    const r = await answerOnce(s.id, answer, how);
    return r === "sent" ? { sent: true, how } : r;
  }

  // ---- going there ------------------------------------------------------------------------------

  // A click on a toast (not on its buttons or X): its chat, in its space and tab (n.go), else its action.
  open(n) {
    const g = n.go;
    const s = g?.tab ? spaces().find((x) => (x.tabs ?? []).some((t) => t.id === g.tab)) : null;
    if (s) {
      if (!s.active) run("space.select", { id: s.id });
      run("tab.select", { id: g.tab });
      if (g.pane && panes().some((p) => p.pane === g.pane)) run("pane.focus", { pane: g.pane });
    // Its tab closed since: the agent, wherever it shows now (the tab bar's alert route, lib/inbox.js toAlert()).
    } else if (n.terminal && (g || !n.action?.name)) run(`tabs-tasks.alert-${String(n.terminal).replace(/\//g, "~")}`, {});
    else if (n.action?.name) run(n.action.name, n.action.args ?? {});
    this.dismiss(n.id);
  }

  // ---- drawing ----------------------------------------------------------------------------------

  // A permission's exact text: each line as is, in the terminal's font, never wrapped or clipped (askToast() made the
  // lines to fit).
  exact(lines, t, id) {
    const mono = settings()?.font_family ?? "JetBrains Mono";
    return v_flex().id(id).w_full().mt(2).px(6).py(4).rounded(RADIUS.control).bg(t.hover)
      .children(lines.map((l, i) => div().id(`${id}-${i}`).h(MONO).whitespace_nowrap().font_family(mono).text_size(TEXT.label)
        .line_height(`${MONO}px`).text_color(t.foreground).child(l)));
  }

  // Under the title: the request's choices, its Review, or the body (and Open chat for an agent that needs you).
  body(n, s, t) {
    const v = s?.view;
    const el = v_flex().w_full().min_w(0).gap(4);
    const text = (words) => div().max_h(LINE * LINES).overflow_hidden().line_height(`${LINE}px`).text_size(TEXT.label).text_color(t.muted)
      .child(clip(String(words), BODY_MAX));
    const quiet = (words) => div().text_size(TEXT.label).text_color(t.dim).child(words);
    const openBtn = (label) => this.stopper(h_flex().id(`toast-open-row-${n.id}`).gap(6).mt(4).child(button(`toast-open-${n.id}`, label, { kind: "secondary", size: "sm",
      onClick: () => this.open(n) })));
    if (s && v?.kind === "choices" && (!s.state || s.state === "sending")) {
      if (!s.shownAt) s.shownAt = Date.now();
      const busy = s.state === "sending";
      const btn = (c) => button(`toast-${c.id}-${n.id}`, c.label, { kind: c.look, size: "sm", disabled: busy,
        onClick: (_e, cx) => this.choose(n, s, c.id, cx) });
      el.child(div().text_size(TEXT.label).text_color(t.muted).child(v.lead)).child(this.exact(v.lines, t, `toast-exact-${n.id}`));
      el.child(this.stopper(h_flex().id(`toast-choices-row-${n.id}`).mt(4).gap(6).items_center()
        .children(v.choices.filter((c) => c.id !== "always").map(btn))
        .child(div().flex_1())
        .child(button(`toast-open-${n.id}`, "Open chat", { kind: "plain", size: "sm", onClick: () => this.open(n) }))));
      if (v.always) {
        el.child(div().mt(2).children(v.always.lines.map((l, i) => div().id(`toast-scope-${n.id}-${i}`).whitespace_nowrap()
          .font_family(settings()?.font_family ?? "JetBrains Mono").text_size(TEXT.label).line_height(`${MONO}px`).text_color(t.muted).child(l))));
        el.child(this.stopper(h_flex().id(`toast-always-row-${n.id}`).gap(6).child(btn(v.choices.find((c) => c.id === "always")))));
      }
      if (busy) el.child(quiet("Sending…"));
      else if (s.msg) el.child(div().text_size(TEXT.label).text_color(red()).child(s.msg));
      return el;
    }
    if (s?.state) return el.child(text(s.view?.reason || n.body || "")).child(quiet(s.msg ?? ""))
      .when(s.state !== "sent", (e) => e.child(openBtn("Open chat")));
    // Still reading it: its words only (no button that a choice could take the place of).
    if (s && !v) return n.body ? el.child(text(n.body)) : el;
    if (s) return el.child(text(v.reason || n.body || "")).child(openBtn("Review"));
    if (n.body) el.child(text(n.body));
    // Another Needs you of an agent (a question in words, a prompt its terminal shows): its chat, where to reply.
    if (n.terminal && (n.go || n.action)) el.child(openBtn("Open chat"));
    return el;
  }

  // Buttons on a toast (a row with its id): their press is theirs alone, never the toast's (which opens the chat).
  stopper(el) {
    return el.on_mouse_down("left", (_e, cx) => cx.stop_propagation()).on_click((_e, cx) => cx.stop_propagation());
  }

  item(n, first, t, cx) {
    const on = this.hovered === n.id;
    const s = this.ask(n, cx);
    const act = Boolean(n.action?.name || n.go?.tab || (s && n.terminal));
    return h_flex().id(`toast-${n.id}`).w_full().pl(12).pr(6).py(10).gap(10).items_start().rounded(RADIUS.card)
      .when(!first, (el) => el.border_t(1).border_color(t.border).rounded(0))
      .on_hover((h, cx) => { this.hovered = h ? n.id : this.hovered === n.id ? null : this.hovered; cx.notify(); })
      .when(act, (el) => el.cursor_pointer().hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => { cx.stop_propagation(); this.open(n); }))
      .child(div().size(8).mt(5).flex_none().rounded(4).bg(color(n.level, t)))
      .child(v_flex().flex_1().min_w(0).gap(2)
        .child(div().min_w(0).truncate().text_size(TEXT.body).font_semibold().text_color(t.foreground).child(String(n.title ?? "")))
        .child(this.body(n, s, t)))
      // The X's place is always kept, so nothing moves when it shows. It only closes the toast: never an answer.
      .child(div().id(`toast-x-${n.id}`).size(20).flex_none().flex().items_center().justify_center().rounded(RADIUS.control)
        .when(on, (el) => el.cursor_pointer().text_color(t.dim).hover((s) => s.bg(t.hover).text_color(t.foreground))
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
          .on_click((_e, cx) => { cx.stop_propagation(); this.dismiss(n.id); cx.notify(); })
          .child(svg("mods/workspace-chrome/x.svg").size(14))));
  }

  render(cx) {
    const all = shared(TOASTS) ?? [];
    // While the pointer is on them, the same toasts in the same places: a new one shows once it has left.
    let list = all.slice(-SHOWN);
    if (placement()?.hovered && this.frozen) list = this.frozen.map((id) => all.find((n) => n.id === id)).filter(Boolean);
    else this.frozen = list.map((n) => n.id);
    if (!list.length) return div();
    const t = theme();
    return v_flex().id("toasts").w(WIDTH).children(list.map((n, i) => this.item(n, i === 0, t, cx)));
  }
}
