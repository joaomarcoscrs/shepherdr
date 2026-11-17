// The service's look at the tailnet (main.ts tailnet()), run in the background: never before the service listens, one
// at a time (a look asked for while one runs is done once more right after it), every minute, and sooner while it
// fails (Tailscale stuck, not running yet, sharing refused) so the web app comes back soon after Tailscale does.

export type LoopTimes = { every: number; retry: number[] };
export const TIMES: LoopTimes = { every: 60_000, retry: [5_000, 15_000, 30_000] };

export class TailnetLoop {
  private running: Promise<void> | null = null;
  private again = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  /** looks in a row that failed (0: the last one went well) */
  fails = 0;

  /** look: true when all went well, false to look again sooner. */
  constructor(private look: () => Promise<boolean>, private times: LoopTimes = TIMES) {}

  /** Looks now, or once more right after the look running now; resolves once that look is done. */
  kick(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.running) {
      this.again = true;
      return this.running;
    }
    this.running = (async () => {
      do {
        this.again = false;
        let ok = false;
        try { ok = await this.look(); } catch (e) { console.error(`tailnet: ${(e as Error).message}`); }
        this.fails = ok ? 0 : this.fails + 1;
      } while (this.again && !this.stopped);
      this.running = null;
      this.schedule();
    })();
    return this.running;
  }

  /** When the next look is: a minute, or sooner after a failed one (5 s, 15 s, 30 s, then every minute). */
  next(): number {
    if (!this.fails) return this.times.every;
    return Math.min(this.times.every, this.times.retry[Math.min(this.fails, this.times.retry.length) - 1] ?? this.times.every);
  }

  private schedule() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (this.stopped) return;
    this.timer = setTimeout(() => { this.timer = null; void this.kick(); }, this.next());
  }

  /** No more looks (the service stopping). */
  stop() {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}
