// SHEP-5: the window's Settings › Tasks has Accept when ready (a space's default for its new tasks; herdr-task reads it
// by the space's id). No window runs here: gpui and herdr are stand-ins (fake-gpui.ts, fake-herdr.ts), and the page is
// drawn by the mod's own tasks(), on Everywhere and in a space's own settings.
import { beforeEach, expect, mock, test } from "bun:test";
import { fakeHerdr } from "./fake-herdr";
import { built, byId, dispatch, element, fakeGpuiBase, fakeGpuiKit } from "./fake-gpui";

mock.module("gpui-kit", () => fakeGpuiKit());
mock.module("gpui-base", () => fakeGpuiBase());
mock.module("herdr", () => fakeHerdr({ theme: () => new Proxy({}, { get: () => "#000" }), home: () => "/home/me", machines: () => [],
  spaces: () => [], props: () => ({}), shared: () => undefined, exec: async () => ({ code: 1, stdout: "", stderr: "" }),
  readTextFile: async () => null }));
const { default: ShepherdrSettings } = await import("../mods/shepherdr-settings/main.js");
const CORE: any = await import("../core/settings.js");

const HQ = "spdf9f8bf6c510f7c2385572c1b576da3e";
// The page with what it draws each row from: the real scoped()/inherited() over cfg, its rows recorded.
function page(cfg: any, scope: string | null) {
  const v: any = Object.create(ShepherdrSettings.prototype);
  const rows: any[] = [], sets: any[] = [];
  const inert = () => new Proxy({}, { get: () => () => "" });
  Object.assign(v, {
    cfg, scope, notes: { start: { box: inert(), savedAt: 0 }, finish: { box: inert(), savedAt: 0 } },
    bb: { scope: null, box: inert() }, bn: { shown: false, box: inert() }, ap: null,
    title: () => element(), stepper: () => element(), noteRow: () => element(),
    row: (_t: any, title: string, about: string, control: any, sc: any) => { rows.push({ title, about, control, sc }); return element(); },
    set: (key: string, field: string, value: unknown) => sets.push([key, field, value]),
  });
  v.tasks(new Proxy({}, { get: () => "#000" }));
  return { row: rows.find((r) => r.title === "Accept when ready"), sets };
}
const on = (id: string) => byId(id)!.calls.some(([n]: [string]) => n === "justify_end");

beforeEach(() => { built.length = 0; });

test("Everywhere: off by default, says which spaces set their own", () => {
  const { row } = page({ everywhere: {}, spaces: { [HQ]: { name: "joão HQ", tasks: { auto_accept: true } } } }, null);
  expect(row.about).toBe(CORE.ABOUT["tasks.auto_accept"]);
  expect(on("auto-accept")).toBe(false);
  expect(row.sc.others.map((o: any) => o.id)).toEqual([HQ]);
});

test("joão HQ's own: on, set for this space with a Reset; another space inherits Everywhere's off", () => {
  const cfg = { everywhere: {}, spaces: { [HQ]: { folder: "/home/me", tasks: { auto_accept: true } }, spB: { folder: "/home/me" } } };
  const hq = page(cfg, HQ);
  expect(on("auto-accept")).toBe(true);
  expect(hq.row.sc).toMatchObject({ mine: true, from: "Inherited from Everywhere" });
  built.length = 0;
  const b = page(cfg, "spB");
  expect(on("auto-accept")).toBe(false);
  expect(b.row.sc).toMatchObject({ mine: false });
  // flipping it in the other space writes that space's own value
  dispatch(byId("auto-accept")!, "on_click", { notify: () => {} });
  expect(b.sets).toEqual([["tasks", "auto_accept", true]]);
});
