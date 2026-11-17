import { stageFor, stageOf } from "./stages";

describe("stages", () => {
  it("knows every status and lane, and nothing else", () => {
    expect(stageOf("followup_needed")).toBe("followup_needed");
    expect(stageOf("second_impression")).toBe("second_impression");
    expect(stageOf("ready_to_launch")).toBe("ready_to_launch");
    expect(stageOf("closed")).toBe("closed");
    expect(stageOf("gone_quiet")).toBe("ended");
    expect(stageOf("ended")).toBe("ended");
    expect(stageOf("gated_out")).toBe("gated_out");
    expect(stageOf("made_up")).toBeNull();
    expect(stageOf(null)).toBeNull();
  });

  it("gives a status its whole stage, for its name and its square", () => {
    expect(stageFor("landing_zone")).toMatchObject({ key: "landing_zone", label: "Landing Zone" });
    expect(stageFor("withdrew")).toMatchObject({ key: "ended", label: "Ended" });
    expect(stageFor("made_up")).toBeNull();
    expect(stageFor(undefined)).toBeNull();
  });
});
