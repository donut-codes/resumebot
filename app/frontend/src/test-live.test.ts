import { liveHub } from "./state/live";
import { fakeLive, pollTimers } from "./test-live";

describe("fakeLive", () => {
  let live: ReturnType<typeof fakeLive>;
  beforeEach(() => {
    live = fakeLive();
  });
  afterEach(() => live.restore());

  it("hands a changed message to a listener of the changed topic, over a stream that asks for it", async () => {
    const heard = vi.fn();
    const subscription = liveHub.subscribe(["changed"], heard);
    await live.changed("agents", ["a1"]);
    expect(heard).toHaveBeenCalledWith("changed", { area: "agents", ids: ["a1"] });
    expect(live.streams()[0].url).toContain("changed");
    subscription.close();
  });

  it("tells a listener when the stream opens and when it drops", async () => {
    const states: string[] = [];
    const subscription = liveHub.subscribe(["changed"], vi.fn(), (s) => states.push(s));
    await live.opened();
    await live.dropped();
    expect(states).toEqual(["open", "closed"]);
    subscription.close();
  });

  it("lists the polling delays a timer was set with, and no others", () => {
    const timers = pollTimers();
    const a = setTimeout(() => {}, 1500);
    const b = setTimeout(() => {}, 7);
    const c = setInterval(() => {}, 30_000);
    clearTimeout(a);
    clearTimeout(b);
    clearInterval(c);
    expect(timers.seen()).toEqual([1500, 30_000]);
    timers.stop();
  });
});
