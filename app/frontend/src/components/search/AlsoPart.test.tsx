import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { Profile, ProfileSaved } from "../../api/types/profile";
import { profileOf } from "../../state/searchFixtures";
import { createSearchStore, SearchStoreProvider, type SearchApi } from "../../state/searchStore";
import { render } from "../../test-utils";
import { ToastProvider } from "../toast/ToastProvider";
import SearchScreen from "./SearchScreen";

// Also sent to agents on a made-up store: the positioning, the rules by area and the notes, each edited or removed
// through the profile save. Nobody's real data.

function made(): Profile {
  const p = profileOf();
  p.criteria = { ...p.criteria, positioning: "A made-up way of presenting.", notes: [{ id: "n1", text: "A made-up criteria note.", source: "old profile" }] };
  p.gates = { ...p.gates, notes: [{ id: "n2", text: "A made-up gates note.", source: "" }] };
  p.settings = {
    ...p.settings,
    rules: [
      { id: "r1", area: "search", says: "Made-up search rule." },
      { id: "r2", area: "packets", says: "Made-up packets rule." },
      { id: "r3", area: "apply", says: "Made-up apply rule." },
      { id: "r4", area: "email", says: "Made-up email rule." },
      { id: "r5", area: "mystery", says: "Made-up odd rule." },
    ],
  };
  return p;
}

function setup(start: Profile = made()) {
  let server = start;
  const api = {
    profile: vi.fn(async () => structuredClone(server)),
    saveProfile: vi.fn(async (part: keyof Profile["versions"], data: unknown): Promise<ProfileSaved> => {
      const version = server.versions[part] + 1;
      server = { ...server, [part]: structuredClone(data), versions: { ...server.versions, [part]: version } };
      return { saved: true, version, profile: structuredClone(server) };
    }),
    profileWhy: vi.fn(),
    profileHistory: vi.fn(async () => ({ kind: "criteria", versions: [] })),
    gatesStopped: vi.fn(async () => ({ stopped: {} })),
    roleTrend: vi.fn(async () => ({ total: 0 })),
    agentReady: vi.fn(async () => ({ ready: true, why: null })),
    sites: vi.fn(async () => ({ sites: [], job_systems: [], watch_in_play: false })),
    fitsNone: vi.fn(async () => ({ pursued: { count: 0, items: [] }, this_week: { count: 0, items: [] } })),
  } as unknown as SearchApi;
  const store = createSearchStore({ api, openLive: (_e, onState) => (onState("open"), { close: () => {} }), resiteMs: 0 });
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
  return { api, Screen };
}

const typeInto = (box: HTMLElement, value: string) => {
  fireEvent.focus(box);
  fireEvent.change(box, { target: { value } });
  fireEvent.blur(box);
};

describe("Also sent to agents", () => {
  it("shows the positioning, the rules by area and the notes as written", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/also" });
    await screen.findByLabelText("Your positioning");
    const pane = screen.getByRole("region", { name: "Also sent to agents" });
    expect(within(pane).getByLabelText("Your positioning")).toHaveValue("A made-up way of presenting.");
    const rules: [string, string][] = [
      ["Rules for the search", "Made-up search rule."],
      ["Rules for packets", "Made-up packets rule."],
      ["Rules for applying", "Made-up apply rule."],
      ["Rules for email", "Made-up email rule."],
    ];
    for (const [title, rule] of rules) {
      expect(within(within(pane).getByRole("group", { name: title })).getByLabelText("Rule")).toHaveValue(rule);
    }
    expect(within(pane).queryByRole("group", { name: "Rules for screening" })).toBeNull();
    expect(within(pane).getByLabelText("old profile")).toHaveValue("A made-up criteria note.");
    expect(within(pane).getByLabelText("Note")).toHaveValue("A made-up gates note.");
  });

  it("saves settings with only that rule changed when a rule is edited", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/also" });
    const group = await screen.findByRole("group", { name: "Rules for packets" });
    typeInto(within(group).getByLabelText("Rule"), "A different made-up rule.");
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const [part, data] = vi.mocked(api.saveProfile).mock.calls[0] as unknown as [string, Profile["settings"]];
    expect(part).toBe("settings");
    expect(data.rules.map((r) => r.says)).toEqual(["Made-up search rule.", "A different made-up rule.", "Made-up apply rule.", "Made-up email rule.", "Made-up odd rule."]);
  });

  it("removes a rule at once and offers Undo on the Saved line", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/also" });
    fireEvent.click(await screen.findByRole("button", { name: /Remove this rule: Made-up search rule/ }));
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    expect((vi.mocked(api.saveProfile).mock.calls[0][1] as Profile["settings"]).rules.map((r) => r.id)).toEqual(["r2", "r3", "r4", "r5"]);
    const pane = screen.getByRole("region", { name: "Also sent to agents" });
    await waitFor(() => expect(within(pane).getByRole("status")).toHaveTextContent("Saved"));
    fireEvent.click(within(within(pane).getByRole("status")).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(2));
    expect((vi.mocked(api.saveProfile).mock.calls[1][1] as Profile["settings"]).rules).toHaveLength(5);
  });

  it("saves the gates when a note of the gates is changed, and nothing else in them", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/also" });
    typeInto(await screen.findByLabelText("Note"), "A reworded made-up note.");
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const [part, data] = vi.mocked(api.saveProfile).mock.calls[0] as unknown as [string, Profile["gates"]];
    expect(part).toBe("gates");
    expect(data.notes).toEqual([{ id: "n2", text: "A reworded made-up note.", source: "" }]);
    expect(data.other).toEqual(made().gates.other);
  });

  it("saves a very long text whole, with no cap on any field", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/also" });
    const long = "x".repeat(20000);
    typeInto(await screen.findByLabelText("Your positioning"), long);
    await waitFor(() => expect(api.saveProfile).toHaveBeenCalledTimes(1));
    const saved = vi.mocked(api.saveProfile).mock.calls[0][1] as Profile["criteria"];
    expect(saved.positioning).toBe(long);
    expect(saved.notes).toHaveLength(1);
  });

  it("has no Add control, and says nothing of who reads the apply and email rules", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/also" });
    await screen.findByLabelText("Your positioning");
    const pane = screen.getByRole("region", { name: "Also sent to agents" });
    expect(within(pane).queryByRole("button", { name: /add/i })).toBeNull();
    for (const title of ["Rules for applying", "Rules for email"]) {
      const group = within(pane).getByRole("group", { name: title });
      expect(group).toHaveTextContent("Kept with your profile.");
      expect(group.textContent).not.toMatch(/nothing reads/i);
    }
  });

  it("does not save a rule emptied to nothing, and puts its words back", async () => {
    const { Screen, api } = setup();
    render(<Screen />, { hash: "#/profile/also" });
    const group = await screen.findByRole("group", { name: "Rules for the search" });
    typeInto(within(group).getByLabelText("Rule"), "   ");
    await waitFor(() => expect(within(group).getByLabelText("Rule")).toHaveValue("Made-up search rule."));
    expect(api.saveProfile).not.toHaveBeenCalled();
  });

  it("shows a rule of an unknown area under Other rules", async () => {
    const { Screen } = setup();
    render(<Screen />, { hash: "#/profile/also" });
    const group = await screen.findByRole("group", { name: "Other rules" });
    expect(within(group).getByLabelText("Rule")).toHaveValue("Made-up odd rule.");
  });

  it("does not show a sheet with nothing in it", async () => {
    const { Screen } = setup(profileOf());
    render(<Screen />, { hash: "#/profile/also" });
    await screen.findByLabelText("Your positioning");
    const pane = screen.getByRole("region", { name: "Also sent to agents" });
    expect(within(pane).getByLabelText("Your positioning")).toHaveValue("");
    expect(within(pane).queryByRole("heading", { name: "Rules" })).toBeNull();
    expect(within(pane).queryByRole("heading", { name: "Notes not placed yet" })).toBeNull();
  });
});
