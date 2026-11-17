import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { Profile, ProfileSaved } from "../../api/types/profile";
import { profileOf } from "../../state/searchFixtures";
import { createSearchStore, SearchStoreProvider, type SearchApi } from "../../state/searchStore";
import { render } from "../../test-utils";
import { ToastProvider } from "../toast/ToastProvider";
import DialRows from "./DialRows";
import { applyDial, dialSays, readDials } from "./dials";
import SearchLeaveGuard from "./SearchLeaveGuard";
import SearchScreen from "./SearchScreen";

// The measurable dials as rows of Search Settings (docs/PRD.md path 46): one row each with its choice and its band or
// pick list, no default, a Must written to Hard pass and a Prefer to Prioritization (no Avoid: docs/PRD.md 30.10, 46.1).
// Nobody's real data.

function setup(profile: Profile = profileOf()) {
  let server = profile;
  const api = {
    profile: vi.fn(async () => structuredClone(server)),
    saveProfile: vi.fn(async (part: keyof Profile["versions"], data: unknown): Promise<ProfileSaved> => {
      const version = server.versions[part] + 1;
      server = { ...server, [part]: structuredClone(data), versions: { ...server.versions, [part]: version } };
      return { saved: true, version, profile: structuredClone(server) };
    }),
    profileWhy: vi.fn(),
    profileHistory: vi.fn(async () => ({ kind: "gates", versions: [] })),
    gatesStopped: vi.fn(async () => ({ stopped: {} })),
    roleTrend: vi.fn(async () => ({ total: 0 })),
    agentReady: vi.fn(async () => ({ ready: true, why: null })),
    sites: vi.fn(async () => ({ sites: [], job_systems: [], watch_in_play: false })),
    fitsNone: vi.fn(async () => ({ pursued: { count: 0, items: [] }, this_week: { count: 0, items: [] } })),
  } as unknown as SearchApi;
  const store = createSearchStore({ api, openLive: (_e, onState) => (onState("open"), { close: () => {} }), resiteMs: 0 });
  function Rows({ part }: { part: "hard_pass" | "prioritization" }) {
    const [s] = useState(store);
    return (
      <ToastProvider>
        <SearchStoreProvider store={s}>
          <SearchLeaveGuard />
          <DialRows part={part} />
        </SearchStoreProvider>
      </ToastProvider>
    );
  }
  function Screen() {
    const [s] = useState(store);
    return (
      <ToastProvider>
        <SearchStoreProvider store={s}>
          <SearchScreen />
        </SearchStoreProvider>
      </ToastProvider>
    );
  }
  return { api, store, Rows, Screen };
}

const row = (name: string) => screen.getByText(name).closest("li") as HTMLElement;

describe("the dials in Search Settings", () => {
  const choices = (name: string) => within(within(row(name)).getByRole("combobox")).getAllByRole("option").map((o) => o.textContent);
  const rowNames = (rows: HTMLElement[]) => rows.map((r) => r.querySelector(".fi-ss-dial-name")?.textContent);

  // Edited 2026-11-03 (PRD 46.4): this was one test of the eleven rows ("Title words is the eleventh row, so the count is
  // 11", 2026-10-28), every part showing every dial. Hard pass lists only the dials that take a Must, so it is eight rows.
  // Edited again 2026-11-03 (PRD 46.4, the owner's two restored hard passes): Travel and Employer named are Must-only
  // dials, so Hard pass is ten rows; Prioritization is unchanged.
  // Edited 2026-11-05 (PRD 46.4, the owner's "Add roles which lead business development as a gate"): Leads business
  // development takes a Must, so Hard pass is eleven rows.
  it("shows in Hard pass one row for each dial that takes a Must, every one at Don't care", async () => {
    const { Rows, store } = setup();
    await store.reload();
    render(<Rows part="hard_pass" />);
    const rows = await screen.findAllByRole("listitem");
    expect(rows.map((r) => (within(r).getByRole("combobox") as HTMLSelectElement).value)).toEqual(Array(12).fill("dont_care"));
    expect(rowNames(rows)).toEqual([
      "Size", "Ownership", "Industry", "Distance from home", "Years of experience asked", "Degree required", "Freshness",
      "Travel", "Title words", "Employer named", "Leads business development", "Main work",
    ]);
    // Edited 2026-10-27 (PRD 30.10, 46.1): Avoid was in every row; it is gone. A dial with both choices keeps its control.
    expect(choices("Size")).toEqual(["Don't care", "Must", "Prefer"]);
    expect(choices("Title words")).toEqual(["Don't care", "Must"]);
    expect(choices("Travel")).toEqual(["Don't care", "Must"]);
    expect(choices("Employer named")).toEqual(["Don't care", "Must"]);
    expect(choices("Leads business development")).toEqual(["Don't care", "Must", "Prefer"]);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("shows in Prioritization one row for each dial that takes a Prefer, every one at Don't care", async () => {
    const { Rows, store } = setup();
    await store.reload();
    render(<Rows part="prioritization" />);
    const rows = await screen.findAllByRole("listitem");
    // Edited 2026-11-05 (PRD 46.4): Leads business development takes a Prefer, so Prioritization is eleven rows.
    expect(rows.map((r) => (within(r).getByRole("combobox") as HTMLSelectElement).value)).toEqual(Array(11).fill("dont_care"));
    expect(rowNames(rows)).toEqual([
      "Size", "Ownership", "Industry", "Distance from home", "Years of experience asked", "Degree required", "Freshness",
      "Pay", "Work mode", "Clearance", "Leads business development",
    ]);
    // Edited 2026-10-27 (PRD 30.10, 46.1): Clearance (Avoid only) takes Prefer.
    expect(choices("Size")).toEqual(["Don't care", "Must", "Prefer"]);
    expect(choices("Pay")).toEqual(["Don't care", "Prefer"]);
    expect(choices("Work mode")).toEqual(["Don't care", "Prefer"]);
    expect(choices("Clearance")).toEqual(["Don't care", "Prefer"]);
    for (const name of ["Title words", "Travel", "Employer named"]) expect(screen.queryByText(name)).not.toBeInTheDocument();
  });

  it("lists Title words in the Hard pass part of Search Settings and none of the Prefer-only dials", async () => {
    const { Screen, store } = setup();
    await store.reload();
    render(<Screen />, { hash: "#/profile/gates" });
    const hard = await screen.findByRole("region", { name: "Hard pass" });
    const dials = within(hard).getByRole("list", { name: "Measured dials" });
    for (const name of ["Title words", "Travel", "Employer named"]) expect(within(dials).getByText(name)).toBeInTheDocument();
    for (const name of ["Pay", "Work mode", "Clearance"]) expect(within(dials).queryByText(name)).not.toBeInTheDocument();
  });

  it("lists Pay, Work mode and Clearance in the Prioritization part of Search Settings and not Title words", async () => {
    const { Screen, store } = setup();
    await store.reload();
    render(<Screen />, { hash: "#/profile/scoring" });
    const asks = await screen.findByRole("region", { name: "Prioritization" });
    const dials = within(asks).getByRole("list", { name: "Measured dials" });
    for (const name of ["Pay", "Work mode", "Clearance"]) expect(within(dials).getByText(name)).toBeInTheDocument();
    for (const name of ["Title words", "Travel", "Employer named"]) expect(within(dials).queryByText(name)).not.toBeInTheDocument();
  });

  it("saves a Must as a line of Hard pass once it has a value, and nothing before", async () => {
    const { Rows, api, store } = setup();
    await store.reload();
    render(<Rows part="hard_pass" />);
    fireEvent.change(within(row("Ownership")).getByRole("combobox"), { target: { value: "must" } });
    expect(api.saveProfile).not.toHaveBeenCalled();
    fireEvent.click(within(row("Ownership")).getByRole("checkbox", { name: "Public" }));
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    fireEvent.click(within(row("Ownership")).getByRole("checkbox", { name: "Nonprofit" }));
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(2));
    const [part, doc] = vi.mocked(api.saveProfile).mock.calls[1] as unknown as [string, Profile["gates"]];
    expect(part).toBe("gates");
    expect(doc.other.find((g) => g.fact === "ownership")).toMatchObject({ kind: "employer_fact", op: "in", value: "public,nonprofit", roles: [] });
  });

  it("saves a Prefer as an ask the app checks only when it has points, and offers no negative points", async () => {
    const { Rows, api, store } = setup();
    await store.reload();
    render(<Rows part="prioritization" />);
    fireEvent.change(within(row("Distance from home")).getByRole("combobox"), { target: { value: "prefer" } });
    const miles = within(row("Distance from home")).getByLabelText("Distance from home: the number of miles");
    fireEvent.change(miles, { target: { value: "40" } });
    fireEvent.blur(miles);
    expect(api.saveProfile).not.toHaveBeenCalled();
    const pts = within(row("Distance from home")).getByLabelText("Distance from home: points");
    fireEvent.change(pts, { target: { value: "3" } });
    fireEvent.blur(pts);
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const [part, doc] = vi.mocked(api.saveProfile).mock.calls[0] as unknown as [string, Profile["scoring"]];
    expect(part).toBe("scoring");
    expect(doc.asks.find((a) => a.fact === "distance_miles")).toMatchObject({
      checked_by: "app", op: "<=", value: "40", points: 3, says: "The distance from home is within 40 miles.",
    });

    // Edited 2026-10-27 (PRD 30.10): the row also saved an Avoid (op ">", points -3); it has no such choice now.
    const choices = within(row("Distance from home")).getAllByRole("option").map((o) => o.textContent);
    expect(choices).not.toContain("Avoid");
    expect(within(row("Distance from home")).getByLabelText("Distance from home: points")).toHaveAttribute("min", "1");
  });

  // Choosing Must on Freshness and typing 28 never stuck. A number saved as soon as it said enough ("2"),
  // and the row was keyed on what it held, so it was replaced after the first digit and the rest of the typing went nowhere.
  it.each([
    ["Freshness", "days", "28"],
    ["Distance from home", "miles", "75"],
    ["Years of experience asked", "years", "12"],
  ])("saves a Must number once, when its field is left: %s", async (name, unit, typed) => {
    const { Rows, api, store } = setup();
    await store.reload();
    render(<Rows part="hard_pass" />);
    fireEvent.change(within(row(name)).getByRole("combobox"), { target: { value: "must" } });
    const field = () => within(row(name)).getByLabelText(`${name}: the number of ${unit}`) as HTMLInputElement;
    const first = field();
    for (let i = 1; i <= typed.length; i++) fireEvent.change(field(), { target: { value: typed.slice(0, i) } });
    expect(api.saveProfile).not.toHaveBeenCalled();
    fireEvent.blur(field());
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const [part, doc] = vi.mocked(api.saveProfile).mock.calls[0] as unknown as [string, Profile["gates"]];
    expect(part).toBe("gates");
    expect(doc.other.find((g) => g.kind === "job_fact" && g.op === "<=")).toMatchObject({ value: typed, roles: [] });
    // The same input element took the typing and still shows it, and the row now reads as the saved Must.
    expect(field()).toBe(first);
    expect(field().value).toBe(typed);
    expect(within(row(name)).getByRole("combobox")).toHaveValue("must");
  });

  it("saves a Must number when Enter is pressed in its field", async () => {
    const { Rows, api, store } = setup();
    await store.reload();
    render(<Rows part="hard_pass" />);
    fireEvent.change(within(row("Freshness")).getByRole("combobox"), { target: { value: "must" } });
    const field = within(row("Freshness")).getByLabelText("Freshness: the number of days");
    fireEvent.change(field, { target: { value: "28" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
  });

  it("takes a dial back to Don't care by removing its line", async () => {
    const start = profileOf();
    const { gates } = applyDial(start, readDials(start)[3], "must", 50, 0);
    const { Rows, api, store } = setup({ ...start, gates });
    await store.reload();
    render(<Rows part="hard_pass" />);
    expect(within(row("Distance from home")).getByRole("combobox")).toHaveValue("must");
    fireEvent.change(within(row("Distance from home")).getByRole("combobox"), { target: { value: "dont_care" } });
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    expect((vi.mocked(api.saveProfile).mock.calls[0][1] as Profile["gates"]).other.map((g) => g.id)).toEqual(["travel"]);
  });

  it("reads a rule the owner already has as the dial, keeping its sentence and points, and leaves one it cannot show in its list, a negative one included", async () => {
    const base = profileOf();
    const scoring = {
      ...base.scoring,
      asks: [
        ...base.scoring.asks,
        { id: "pe", section: "company" as const, says: "Private equity owned", points: -1, checked_by: "app" as const, fact: "ownership", op: "in", value: "pe" },
        { id: "odd", section: "company" as const, says: "Something the list lacks", points: 1, checked_by: "app" as const, fact: "industry", op: "contains", value: "tech" },
      ],
    };
    const { Screen, store } = setup({ ...base, scoring });
    await store.reload();
    render(<Screen />, { hash: "#/profile/scoring" });
    const list = await screen.findByRole("region", { name: "Prioritization" });
    // Edited 2026-10-27 (PRD 30.10): the -1 line read as the Ownership dial's Avoid; it is no dial's now, it stays in the
    // list as the owner's own line, marked as earning nothing.
    expect(within(row("Ownership")).getByRole("combobox")).toHaveValue("dont_care");
    expect(within(list).getByText("Private equity owned")).toBeInTheDocument();
    expect(within(row("Private equity owned")).getByText("Earns nothing")).toBeInTheDocument();
    expect(within(list).getByText("Something the list lacks")).toBeInTheDocument();
  });

  it("says each dial's line in its own plain words", () => {
    // Edited 2026-11-03 (PRD 46.4): the dials were picked by their place in the list (0, 3, 7); Travel moved Pay along, so
    // they are picked by id. The words are unchanged.
    const dials = readDials(profileOf()).map((s) => s.dial);
    const [size, distance, pay] = ["size", "distance", "pay"].map((id) => dials.find((d) => d.id === id)!);
    expect(dialSays(size, "micro,small")).toBe("The employer's size is under 10 employees or 10 to 99.");
    expect(dialSays(distance, 50)).toBe("The distance from home is within 50 miles.");
    expect(dialSays(pay, 150000)).toBe("The top of the posted pay is at least $150,000 a year.");
  });

  it("offers no way to add a line only a reader could judge, in Hard pass or in Prioritization", async () => {
    const { Screen, store } = setup();
    await store.reload();
    render(<Screen />, { hash: "#/profile/gates" });
    const hard = await screen.findByRole("region", { name: "Hard pass" });
    expect(within(hard).queryByRole("button", { name: /Add a hard pass/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Prioritization" }));
    const asks = await screen.findByRole("region", { name: "Prioritization" });
    for (const name of [/Add an ask/, /any of/i, /first match/i]) {
      expect(within(asks).queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });
  it("saves Title words as a Must on the title once the owner leaves the box, and not on every keystroke", async () => {
    const { Rows, api, store } = setup();
    await store.reload();
    render(<Rows part="hard_pass" />);
    fireEvent.change(within(row("Title words")).getByRole("combobox"), { target: { value: "must" } });
    const box = within(row("Title words")).getByRole("textbox", { name: "Title words: the words and phrases" });
    fireEvent.change(box, { target: { value: "intern,  new grad " } });
    expect(api.saveProfile).not.toHaveBeenCalled();
    fireEvent.blur(box);
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const [part, doc] = vi.mocked(api.saveProfile).mock.calls[0] as unknown as [string, Profile["gates"]];
    expect(part).toBe("gates");
    expect(doc.other.find((g) => g.fact === "title")).toMatchObject({
      kind: "job_fact", op: "excludes", value: "intern, new grad", section: "posting", roles: [],
    });
  });

  it("shows Main work with its own caption and no exceptions box, and Title words keeps its exceptions box", async () => {
    const { Rows, store } = setup();
    await store.reload();
    render(<Rows part="hard_pass" />);
    expect(within(row("Main work")).getByRole("combobox")).toHaveValue("dont_care");
    expect(choices("Main work")).toEqual(["Don't care", "Must"]);
    fireEvent.change(within(row("Main work")).getByRole("combobox"), { target: { value: "must" } });
    expect(
      within(row("Main work")).getByText(/A role whose main work is any of these is screened out\. Separate words and phrases with commas\. A posting that does not say passes\./),
    ).toBeInTheDocument();
    expect(within(row("Main work")).queryByText(/Except in these phrases/)).not.toBeInTheDocument();
    fireEvent.change(within(row("Title words")).getByRole("combobox"), { target: { value: "must" } });
    expect(within(row("Title words")).getByText(/A title with any of these is screened out/)).toBeInTheDocument();
    expect(within(row("Title words")).getByText(/Except in these phrases/)).toBeInTheDocument();
  });

  it("saves Main work as a Must on main_work once the box is left, without exceptions, and removes the line when emptied", async () => {
    const { Rows, api, store } = setup();
    await store.reload();
    render(<Rows part="hard_pass" />);
    fireEvent.change(within(row("Main work")).getByRole("combobox"), { target: { value: "must" } });
    const box = within(row("Main work")).getByRole("textbox", { name: "Main work: the words and phrases" });
    fireEvent.change(box, { target: { value: "wheelwrighting,  harbor ledgering " } });
    expect(api.saveProfile).not.toHaveBeenCalled();
    fireEvent.blur(box);
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const doc = vi.mocked(api.saveProfile).mock.calls[0][1] as unknown as Profile["gates"];
    const line = doc.other.find((g) => g.fact === "main_work")!;
    expect(line).toMatchObject({ id: "dial-main-work-must", kind: "job_fact", op: "excludes", value: "wheelwrighting, harbor ledgering", roles: [] });
    expect(line.except_words).toBeUndefined();
    fireEvent.change(within(row("Main work")).getByRole("combobox"), { target: { value: "dont_care" } });
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(2));
    expect((vi.mocked(api.saveProfile).mock.calls[1][1] as unknown as Profile["gates"]).other.some((g) => g.fact === "main_work")).toBe(false);
  });

  it("says the Main work line as the server does and offers it only in Hard pass", async () => {
    const main = readDials(profileOf()).find((s) => s.dial.id === "main-work")!.dial;
    expect(dialSays(main, "wheelwrighting, harbor ledgering")).toBe("The role's main work has none of: wheelwrighting or harbor ledgering.");
    const { Rows, store } = setup();
    await store.reload();
    render(<Rows part="prioritization" />);
    await screen.findAllByRole("listitem");
    expect(screen.queryByText("Main work")).not.toBeInTheDocument();
  });

  it("reads a saved Title words line back into its row, and takes it back to Don't care by removing the line", async () => {
    const start = profileOf();
    const state = readDials(start).find((s) => s.dial.id === "title")!;
    const { gates } = applyDial(start, state, "must", "intern, entry level", 0);
    const { Rows, api, store } = setup({ ...start, gates });
    await store.reload();
    render(<Rows part="hard_pass" />);
    expect(within(row("Title words")).getByRole("combobox")).toHaveValue("must");
    expect(within(row("Title words")).getByRole("textbox", { name: "Title words: the words and phrases" })).toHaveValue("intern, entry level");
    fireEvent.change(within(row("Title words")).getByRole("combobox"), { target: { value: "dont_care" } });
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    expect((vi.mocked(api.saveProfile).mock.calls[0][1] as Profile["gates"]).other.some((g) => g.fact === "title")).toBe(false);
  });

  it("saves the Title words exceptions on the same line once the owner leaves the box", async () => {
    const start = profileOf();
    const state = readDials(start).find((s) => s.dial.id === "title")!;
    const { gates } = applyDial(start, state, "must", "vice president", 0);
    const { Rows, api, store } = setup({ ...start, gates });
    await store.reload();
    render(<Rows part="hard_pass" />);
    const box = within(row("Title words")).getByRole("textbox", { name: "Title words: except in these phrases" });
    expect(box).toHaveValue("");
    fireEvent.change(box, { target: { value: "assistant vice president,  associate vice president " } });
    expect(api.saveProfile).not.toHaveBeenCalled();
    fireEvent.blur(box);
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const doc = vi.mocked(api.saveProfile).mock.calls[0][1] as unknown as Profile["gates"];
    expect(doc.other.find((g) => g.fact === "title")).toMatchObject({
      value: "vice president", except_words: "assistant vice president, associate vice president",
    });
  });

  it("reads the saved exceptions back, and leaves them off a line that has none", () => {
    const start = profileOf();
    const state = readDials(start).find((s) => s.dial.id === "title")!;
    const { gates } = applyDial(start, state, "must", "vice president", 0, "assistant vice president");
    const read = readDials({ ...start, gates }).find((s) => s.dial.id === "title")!;
    expect(read.except).toBe("assistant vice president");
    const plain = applyDial(start, state, "must", "vice president", 0).gates.other.find((g) => g.fact === "title")!;
    expect("except_words" in plain).toBe(false);
  });

  it("says the Title words exceptions in the sentence only when set", () => {
    const title = readDials(profileOf()).find((s) => s.dial.id === "title")!.dial;
    expect(dialSays(title, "vice president", "assistant vice president, associate vice president")).toBe(
      "The title has none of: vice president, except in: assistant vice president or associate vice president.",
    );
    expect(dialSays(title, "vice president")).toBe("The title has none of: vice president.");
  });

  it("says the Title words line in plain words", () => {
    const title = readDials(profileOf()).find((s) => s.dial.id === "title")!.dial;
    expect(dialSays(title, "intern, new grad")).toBe("The title has none of: intern or new grad.");
    expect(dialSays(title, "intern")).toBe("The title has none of: intern.");
  });

  // The owner's two hard passes that migration 0124 dropped, back as checks the app runs (PRD 46.4, 2026-11-03).
  it("saves Travel as a Must at most a percent, once the field is left, and says it as the server does", async () => {
    const { Rows, api, store } = setup();
    await store.reload();
    render(<Rows part="hard_pass" />);
    fireEvent.change(within(row("Travel")).getByRole("combobox"), { target: { value: "must" } });
    expect(within(row("Travel")).getByText(/At most/)).toBeInTheDocument();
    const field = within(row("Travel")).getByLabelText("Travel: the number of percent");
    fireEvent.change(field, { target: { value: "25" } });
    expect(api.saveProfile).not.toHaveBeenCalled();
    fireEvent.blur(field);
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const [part, doc] = vi.mocked(api.saveProfile).mock.calls[0] as unknown as [string, Profile["gates"]];
    expect(part).toBe("gates");
    expect(doc.other.find((g) => g.fact === "travel_percent")).toMatchObject({
      kind: "job_fact", op: "<=", value: "25", section: "posting", roles: [],
    });
    const travel = readDials(profileOf()).find((s) => s.dial.id === "travel")!.dial;
    expect(dialSays(travel, 25)).toBe("The travel asked is at most 25%.");
    expect(dialSays(travel, 12.5)).toBe("The travel asked is at most 12.5%.");
  });

  it("saves Employer named as a Must the moment it is chosen, with nothing to set, and takes it back by removing the line", async () => {
    const { Rows, api, store } = setup();
    await store.reload();
    render(<Rows part="hard_pass" />);
    fireEvent.change(within(row("Employer named")).getByRole("combobox"), { target: { value: "must" } });
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const [part, doc] = vi.mocked(api.saveProfile).mock.calls[0] as unknown as [string, Profile["gates"]];
    expect(part).toBe("gates");
    expect(doc.other.find((g) => g.fact === "employer_named")).toMatchObject({
      id: "dial-employer-named-must", kind: "job_fact", op: "=", value: "yes", section: "posting", roles: [],
    });
    expect(within(row("Employer named")).queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(within(row("Employer named")).queryByRole("textbox")).not.toBeInTheDocument();
    await waitFor(() => expect(within(row("Employer named")).getByRole("combobox")).toHaveValue("must"));
    fireEvent.change(within(row("Employer named")).getByRole("combobox"), { target: { value: "dont_care" } });
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(2));
    expect((vi.mocked(api.saveProfile).mock.calls[1][1] as Profile["gates"]).other.some((g) => g.fact === "employer_named")).toBe(false);
  });

  // Travel is saved, Employer named is set seconds later while that save (answered only when its re-gate ends, a minute or
  // more) is still going, and a reload then showed it at Don't care:
  // the second change had waited in the page alone and was never sent.
  /** Travel set to Must at 25 and left (save one, held until `release`), then Employer named set to Must (it waits). */
  async function twoQuickChanges() {
    const s = setup();
    await s.store.reload();
    render(<s.Rows part="hard_pass" />);
    let release: () => void = () => {};
    const real = vi.mocked(s.api.saveProfile).getMockImplementation()!;
    vi.mocked(s.api.saveProfile).mockImplementationOnce(async (...args) => {
      await new Promise<void>((r) => (release = r));
      return real(...args);
    });
    fireEvent.change(within(row("Travel")).getByRole("combobox"), { target: { value: "must" } });
    const field = within(row("Travel")).getByLabelText("Travel: the number of percent");
    fireEvent.change(field, { target: { value: "25" } });
    fireEvent.blur(field);
    await waitFor(() => expect(s.api.saveProfile).toHaveBeenCalledTimes(1));
    fireEvent.change(within(row("Employer named")).getByRole("combobox"), { target: { value: "must" } });
    return { ...s, release: () => release() };
  }
  const leaving = () => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  };

  it("keeps a second dial change made while the first save is on its way, and saves it, with both, once the first has landed", async () => {
    const { api, store, release } = await twoQuickChanges();
    expect(within(row("Employer named")).getByRole("combobox")).toHaveValue("must");
    expect(api.saveProfile).toHaveBeenCalledTimes(1);
    release();
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(2));
    const [, doc, , base] = vi.mocked(api.saveProfile).mock.calls[1] as unknown as [string, Profile["gates"], string, number];
    expect(doc.other.map((g) => g.fact)).toEqual(expect.arrayContaining(["travel_percent", "employer_named"]));
    expect(base).toBe(3);
    await waitFor(() => expect(store.getState().saved.state).toBe("saved"));
    expect(within(row("Employer named")).getByRole("combobox")).toHaveValue("must");
    expect(within(row("Travel")).getByRole("combobox")).toHaveValue("must");
  });

  it("asks before the page is closed or reloaded while the second change waits for the first, and not once both are saved", async () => {
    const { api, store, release } = await twoQuickChanges();
    expect(leaving()).toBe(true);
    release();
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(store.getState().pending).toBe(false));
    expect(leaving()).toBe(false);
  });

  it("reads a saved Employer named line back into its row and says it in plain words", async () => {
    const start = profileOf();
    const state = readDials(start).find((s) => s.dial.id === "employer-named")!;
    const { gates } = applyDial(start, state, "must", "yes", 0);
    const { Rows, store } = setup({ ...start, gates });
    await store.reload();
    render(<Rows part="hard_pass" />);
    expect(within(row("Employer named")).getByRole("combobox")).toHaveValue("must");
    expect(dialSays(state.dial, "yes")).toBe("The posting names its employer.");
    // A line the row does not write (another comparison, another value) is not read as the dial.
    const odd = { ...gates, other: gates.other.map((g) => (g.fact === "employer_named" ? { ...g, value: "no" } : g)) };
    expect(readDials({ ...start, gates: odd }).find((s) => s.dial.id === "employer-named")!.choice).toBe("dont_care");
  });

  // The owner's "Add roles which lead business development as a gate" (PRD 46.4, 2026-11-05): a pick of Yes and No, nothing
  // preset; a Must on No stops the roles that lead it.
  it("saves Leads business development as a Must on the value ticked, once it has one, and not before", async () => {
    const { Rows, api, store } = setup();
    await store.reload();
    render(<Rows part="hard_pass" />);
    const name = "Leads business development";
    expect(within(row(name)).getByRole("combobox")).toHaveValue("dont_care");
    fireEvent.change(within(row(name)).getByRole("combobox"), { target: { value: "must" } });
    expect(api.saveProfile).not.toHaveBeenCalled();
    expect(within(row(name)).getAllByRole("checkbox").map((c) => (c as HTMLInputElement).checked)).toEqual([false, false]);
    fireEvent.click(within(row(name)).getByRole("checkbox", { name: "No" }));
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const [part, doc] = vi.mocked(api.saveProfile).mock.calls[0] as unknown as [string, Profile["gates"]];
    expect(part).toBe("gates");
    expect(doc.other.find((g) => g.fact === "leads_bd")).toMatchObject({
      id: "dial-business-development-must", kind: "job_fact", op: "in", value: "no", section: "posting", roles: [],
    });
    await waitFor(() => expect(within(row(name)).getByRole("combobox")).toHaveValue("must"));
    expect(within(row(name)).getByRole("checkbox", { name: "No" })).toBeChecked();
    fireEvent.change(within(row(name)).getByRole("combobox"), { target: { value: "dont_care" } });
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(2));
    expect((vi.mocked(api.saveProfile).mock.calls[1][1] as Profile["gates"]).other.some((g) => g.fact === "leads_bd")).toBe(false);
  });

  it("saves Leads business development as a Prefer with its points, and says each choice as the server does", async () => {
    const { Rows, api, store } = setup();
    await store.reload();
    render(<Rows part="prioritization" />);
    const name = "Leads business development";
    fireEvent.change(within(row(name)).getByRole("combobox"), { target: { value: "prefer" } });
    fireEvent.click(within(row(name)).getByRole("checkbox", { name: "Yes" }));
    expect(api.saveProfile).not.toHaveBeenCalled();
    const pts = within(row(name)).getByLabelText(`${name}: points`);
    fireEvent.change(pts, { target: { value: "3" } });
    fireEvent.blur(pts);
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const [part, doc] = vi.mocked(api.saveProfile).mock.calls[0] as unknown as [string, Profile["scoring"]];
    expect(part).toBe("scoring");
    expect(doc.asks.find((a) => a.fact === "leads_bd")).toMatchObject({
      checked_by: "app", op: "in", value: "yes", points: 3, says: "Whether the role leads business development is yes.",
    });
    const dial = readDials(profileOf()).find((s) => s.dial.id === "business-development")!.dial;
    expect(dialSays(dial, "no")).toBe("Whether the role leads business development is no.");
  });
});
