// SHEP-3: the window's task row menu (mods/tasks-menu) offers Accept when ready, a check when it is on and its note under
// it, and a pick runs herdr-task auto-accept on the task's machine and folder (--off when it was on). No window runs here:
// gpui and herdr are stand-ins (fake-gpui.ts, fake-herdr.ts); the menu is drawn and clicked as the window would.
import { beforeEach, expect, mock, test } from "bun:test";
import { fakeHerdr } from "./fake-herdr";
import { built, byId, dispatch, fakeGpuiBase, fakeGpuiKit } from "./fake-gpui";

let rowProps: Record<string, unknown> = {};
const ran: { cmd: string; args: string[] }[] = [];
const theme = new Proxy({}, { get: () => "#000000" });
mock.module("herdr", () => fakeHerdr({
  props: () => rowProps, theme: () => theme, appHome: () => "/app", shared: () => null, setShared: () => {}, closeOverlay: () => {},
  settings: () => ({}), layout: () => ({}), spaces: () => [], openOverlay: () => {}, notify: () => {},
  exec: async (cmd: string, args: string[]) => { ran.push({ cmd, args }); return { code: 0, stdout: "{}", stderr: "" }; },
}));
mock.module("gpui-kit", () => fakeGpuiKit());
mock.module("gpui-base", () => fakeGpuiBase());

const { default: TasksMenu } = await import("../mods/tasks-menu/main.js");
const cx = { focus_handle: () => ({ focus() {} }), notify() {} };
const open = (props: Record<string, unknown>) => {
  rowProps = { menu: "row", key: ":/f#1", ref: "APP-1", folder: "/f", machine: "vm", status: "working", ...props };
  built.length = 0;
  const m: any = new TasksMenu();
  m.init(rowProps, cx);
  m.render(cx);
  return m;
};
const texts = () => built.flatMap((e: any) => e.calls.filter(([n]: any) => n === "child").map(([, a]: any) => a[0])).filter((x: unknown) => typeof x === "string");

beforeEach(() => { ran.length = 0; });

test("a Working task's menu: Accept when ready with its note, unchecked; a pick turns it on there", async () => {
  open({ auto: false });
  expect(texts()).toContain("Accept when ready");
  expect(texts()).toContain("Merges it by itself once it's Ready. Checks still apply.");
  const row = byId("tm-auto-accept:0");
  expect(row).toBeTruthy();
  dispatch(row, "on_click");
  await Bun.sleep(0);
  expect(ran).toEqual([{ cmd: "/app/mods/tasks/bin/herdr-task", args: ["--on", "vm", "auto-accept", "APP-1", "--json", "--folder", "/f"] }]);
});

test("on: a check beside it, and a pick turns it off", async () => {
  const svgs = () => built.filter((e: any) => e.kind === "svg").length;
  open({ auto: false, machine: null });
  const off = svgs();
  open({ auto: true, machine: null });
  // menuRow draws one more mark for a setting that is on: its check
  expect(svgs()).toBe(off + 1);
  const row: any = byId("tm-auto-accept:0");
  dispatch(row, "on_click");
  await Bun.sleep(0);
  expect(ran[0].args).toEqual(["auto-accept", "APP-1", "--off", "--json", "--folder", "/f"]);
});

test("Ready and off, or done: not offered (Review is right there)", () => {
  open({ status: "ready", auto: false });
  expect(texts()).not.toContain("Accept when ready");
  open({ status: "done" });
  expect(texts()).not.toContain("Accept when ready");
  open({ status: "ready", auto: true });
  expect(texts()).toContain("Accept when ready");
});
