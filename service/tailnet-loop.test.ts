// The service's looks at the tailnet (tailnet-loop.ts): one at a time, sooner while they fail, none once stopped.
import { describe, expect, test } from "bun:test";
import { TailnetLoop } from "./tailnet-loop.ts";

const times = { every: 400, retry: [40, 80, 160] };

describe("looks at the tailnet", () => {
  test("one at a time: asks while one runs make one more look, not a pile", async () => {
    let now = 0, most = 0, looks = 0;
    let open!: () => void;
    const loop = new TailnetLoop(async () => {
      looks++; most = Math.max(most, ++now);
      await new Promise<void>((r) => (open = r));
      now--;
      return true;
    }, times);
    const first = loop.kick();
    for (let i = 0; i < 5; i++) void loop.kick();
    await Bun.sleep(5);
    open();
    await Bun.sleep(5);
    open();
    await first;
    loop.stop();
    expect(looks).toBe(2);
    expect(most).toBe(1);
  });

  test("sooner while it fails (5 s, 15 s, 30 s, then every minute), back to every minute once it works", () => {
    const loop = new TailnetLoop(async () => true, { every: 60_000, retry: [5_000, 15_000, 30_000] });
    const at = (fails: number) => { loop.fails = fails; return loop.next(); };
    expect([at(0), at(1), at(2), at(3), at(4), at(9)]).toEqual([60_000, 5_000, 15_000, 30_000, 30_000, 30_000]);
  });

  test("a stuck tailscale, then it answers: looked at again by itself and recovers", async () => {
    const said: boolean[] = [];
    let up = false;
    const loop = new TailnetLoop(async () => { said.push(up); return up; }, times);
    await loop.kick();
    expect(loop.fails).toBe(1);
    await Bun.sleep(60); // the 40 ms retry
    expect(said.length).toBe(2);
    up = true;
    await Bun.sleep(120); // the 80 ms retry
    expect(said.at(-1)).toBe(true);
    expect(loop.fails).toBe(0);
    loop.stop();
  });

  test("a look that throws counts as failed and the looks go on", async () => {
    let n = 0;
    const loop = new TailnetLoop(async () => { if (++n === 1) throw new Error("boom"); return true; }, times);
    const was = console.error;
    console.error = () => {};
    try {
      await loop.kick();
      expect(loop.fails).toBe(1);
      await Bun.sleep(60);
      expect(n).toBe(2);
      expect(loop.fails).toBe(0);
    } finally { console.error = was; loop.stop(); }
  });

  test("stopped: no more looks", async () => {
    let n = 0;
    const loop = new TailnetLoop(async () => { n++; return false; }, times);
    await loop.kick();
    loop.stop();
    await loop.kick();
    await Bun.sleep(100);
    expect(n).toBe(1);
  });
});
