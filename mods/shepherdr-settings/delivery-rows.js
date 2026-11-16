// Tasks > Delivery's Open PR rows (SHEP-7): who its pull request is assigned to (GitHub logins, added, nobody taken
// off) and the next step the card names, Everywhere's or a space's own, shown while Open PR is the delivery here.
// herdr-task reads them (settings.py delivery_for); the logins are checked before they're saved (core/settings.js
// prAssign, as herdr-task's logins()). Kept as tasks.pr_assign and tasks.pr_next.

import { div } from "gpui-kit";
import { v_flex, InputState, Input } from "gpui-base";
import { shepherdrFor } from "./coordinator.js";
import { PR_ASSIGN, PR_NEXT, prAssign } from "./core/settings.js";

const FIELDS = { pr_assign: PR_ASSIGN, pr_next: PR_NEXT };

// Made in the window's init (an input needs a live host call): one box per field.
export function deliveryInit(view) {
  const out = {};
  for (const field of Object.keys(FIELDS)) {
    const b = { box: InputState.new({ placeholder: "" }), scope: null, shown: false, err: "", focus: false, setting: false };
    b.box.on("change", (_e, cx) => { if (!b.setting) { b.err = ""; cx.notify(); } });
    b.box.on("focus", (_e, cx) => { b.focus = true; cx.notify(); });
    b.box.on("blur", (_e, cx) => { b.focus = false; save(view, field, b, cx); });
    b.box.on("submit", (_e, cx) => save(view, field, b, cx));
    out[field] = b;
  }
  return out;
}

const valueOf = (view, field) => String(shepherdrFor(view.cfg, view.scope).tasks?.[field] ?? "").trim();

function save(view, field, b, cx) {
  let want = b.box.value().replace(/\s+/g, " ").trim();
  if (want === valueOf(view, field)) return cx.notify();
  if (field === "pr_assign") {
    const v = prAssign(want);
    b.err = v === null ? PR_ASSIGN.bad : "";
    if (b.err) return cx.notify();
    want = v;
  }
  if (!want && field === "pr_next") return view.reset("tasks", field, cx);
  view.set("tasks", field, want.slice(0, 300), cx);
}

// The two rows, for Everywhere and a space (not a machine's page).
export function deliveryRows(view, t) {
  const id = view.scope ?? null;
  if (id && id.startsWith("m:")) return [];
  return Object.entries(FIELDS).map(([field, words]) => {
    const b = view.dr[field];
    if (!b.shown || b.scope !== id) {
      b.shown = true;
      b.scope = id;
      b.err = "";
      b.setting = true;
      b.box.set_value(valueOf(view, field));
      b.setting = false;
    }
    const box = div().relative().w(200)
      .child(Input.new(b.box).w_full().h(30).px(10).rounded(7).border_1().border_color(b.err ? t.blocked : b.focus ? t.accent : t.input)
        .bg(t.background).text_size(13).text_color(t.foreground))
      .when(!b.box.value(), (el) => el.child(div().absolute().top(6).left(11).right(11).truncate().text_size(13).text_color(t.dim)
        .child(words.placeholder)));
    const control = v_flex().items_end().gap(5).child(box)
      .when(Boolean(b.err), (el) => el.child(div().w(200).text_size(12).line_height("17px").text_color(t.blocked).child(b.err)));
    // Reset: back to Everywhere's, shown in the box at once
    const reset = (cx) => {
      b.err = "";
      b.setting = true;
      b.box.set_value(String(view.inherited("tasks", field)?.value ?? "").trim());
      b.setting = false;
      view.reset("tasks", field, cx);
    };
    return view.row(t, words.title, words.about, control, view.scoped("tasks", field, reset));
  });
}
