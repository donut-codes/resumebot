import { postingSections } from "./postingSections";

describe("postingSections", () => {
  it("splits a run-on posting into its headings, paragraphs and lists, rewriting nothing", () => {
    const text = "Head of Projects at Northwind. Role: lead adoption across functions. Responsibilities: set the vision; " +
      "convert strategy into roadmaps; lead a team; define KPIs. Required: 10+ years leadership; cross-functional change.";
    const s = postingSections(text);
    expect(s.map((x) => x.heading)).toEqual([null, "Role", "Responsibilities", "Required"]);
    expect(s[0].paragraphs).toEqual(["Head of Projects at Northwind."]);
    expect(s[2].items).toEqual(["set the vision", "convert strategy into roadmaps", "lead a team", "define KPIs"]);
    expect(s[3].paragraphs).toEqual(["10+ years leadership; cross-functional change."]);
  });

  it("keeps a posting's own line breaks and bullet lines", () => {
    const s = postingSections("About the team\nWe build.\n\nWhat you'll do:\n- Plan the work\n• Ship it");
    expect(s[s.length - 1]).toMatchObject({ heading: "What you'll do", paragraphs: [], items: ["Plan the work", "Ship it"] });
  });

  it("keeps a list where the posting put it, before the paragraphs that follow it", () => {
    const s = postingSections("Preferred Qualifications:\n- First thing\n- Second thing\nA closing note.\nAnother note.");
    expect(s[0].blocks).toEqual([
      { kind: "ul", items: ["First thing", "Second thing"] },
      { kind: "p", text: "A closing note." },
      { kind: "p", text: "Another note." },
    ]);
  });
});
