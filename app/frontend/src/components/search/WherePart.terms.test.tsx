import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { JobSystem } from "../../api/types/where";
import { site } from "../../state/searchFixtures";
import { createSearchStore, SearchStoreProvider, type SearchApi } from "../../state/searchStore";
import { render } from "../../test-utils";
import { ToastProvider } from "../toast/ToastProvider";
import SearchScreen from "./SearchScreen";
import { TERMS_WARNING } from "./termsNote";

// The known site terms on Where to look (docs/PRD.md 55.10): one warning line, with the terms link, on a site or job
// system whose terms forbid automated tools; it never blocks anything. Every host is made up.

const TERMS = [{ hosts: ["denies.example.test", "denies-jobs.example.test"], terms_url: "https://denies.example.test/terms" }];
const WARNING = new RegExp(TERMS_WARNING);
const system = (extra: Partial<JobSystem> = {}): JobSystem => ({
  key: "acme", name: "Acme Hire", domains: ["denies-jobs.example.test"], can_read: true, on: true, status: "Read nightly", roles: 0, employer_names: [], ...extra,
});

function setup(sites: ReturnType<typeof site>[], job_systems: JobSystem[] = []) {
  const api = {
    profile: vi.fn(async () => ({})),
    sites: vi.fn(async () => ({ sites, job_systems, watch_in_play: false, terms: TERMS })),
    addSite: vi.fn(async (url: string) => site("new", { url })),
    setJobSystem: vi.fn(),
    siteRoles: vi.fn(async () => ({ total: 0, items: [] })),
    jobSystemRoles: vi.fn(async () => ({ total: 0, items: [] })),
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

async function list() {
  const region = await screen.findByRole("region", { name: "Where to look" });
  await waitFor(() => expect(within(region).getByText("Sites to search")).toBeInTheDocument());
  return region;
}

describe("the site terms warning", () => {
  it("shows on a site to search whose terms forbid, with the terms link, and on none that does not match", async () => {
    const { Screen } = setup([site("a", { url: "https://jobs.denies.example.test", label: "Denies" }), site("b", { url: "https://quiet.example.test", label: "Quiet" })]);
    render(<Screen />, { hash: "#/profile/where" });
    const region = await list();
    expect(within(region).getAllByText(WARNING)).toHaveLength(1);
    const link = within(region).getByRole("link", { name: "Terms of service" });
    expect(link).toHaveAttribute("href", "https://denies.example.test/terms");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("shows on a site to search and not on a site to avoid on the same host", async () => {
    const { Screen } = setup([
      site("a", { url: "https://denies.example.test", label: "Searching" }),
      site("b", { url: "https://denies.example.test/x", label: "Avoided", list: "avoid", on: false }),
    ]);
    render(<Screen />, { hash: "#/profile/where" });
    const region = await list();
    expect(within(region).getAllByText(WARNING)).toHaveLength(1);
    const avoidedRow = within(region).getByRole("button", { name: "denies.example.test" }).closest("li")!;
    expect(avoidedRow).not.toHaveTextContent(TERMS_WARNING);
  });

  it("shows on a job systems row and in its open pane, and the link does not open the pane", async () => {
    const { Screen } = setup([site("a", { url: "https://quiet.example.test" })], [system()]);
    render(<Screen />, { hash: "#/profile/where" });
    const region = await list();
    expect(within(region).getByText(WARNING)).toBeInTheDocument();
    fireEvent.click(within(region).getByRole("link", { name: "Terms of service" }));
    expect(screen.queryByRole("heading", { name: "Acme Hire" })).toBeNull();
    fireEvent.click(within(region).getByRole("button", { name: "Acme Hire" }));
    await screen.findByRole("heading", { name: "Acme Hire" });
    await waitFor(() => expect(screen.getAllByText(WARNING)).toHaveLength(2));
    expect(screen.getAllByRole("link", { name: "Terms of service" })).toHaveLength(2);
  });

  it("shows nothing on a job system that does not match", async () => {
    const { Screen } = setup([], [system({ domains: ["quiet.example.test"] })]);
    render(<Screen />, { hash: "#/profile/where" });
    const region = await list();
    expect(within(region).queryByText(WARNING)).toBeNull();
  });

  it("shows under the address as it is typed, clears for a quiet one, and never stops Add", async () => {
    const { Screen, api } = setup([site("a", { url: "https://quiet.example.test" })]);
    render(<Screen />, { hash: "#/profile/where" });
    await list();
    fireEvent.click(screen.getByRole("button", { name: "Add a site" }));
    const input = await screen.findByLabelText("Site address");
    const add = screen.getByRole("button", { name: "Add" });
    expect(screen.queryByText(WARNING)).toBeNull();
    fireEvent.change(input, { target: { value: "www.denies.example.test/jobs" } });
    expect(screen.getByText(WARNING)).toBeInTheDocument();
    expect(add).toBeEnabled();
    fireEvent.change(input, { target: { value: "quiet.example.test" } });
    expect(screen.queryByText(WARNING)).toBeNull();
    fireEvent.change(input, { target: { value: "denies.example.test" } });
    expect(screen.getByText(WARNING)).toBeInTheDocument();
    fireEvent.click(add);
    await waitFor(() => expect(api.addSite).toHaveBeenCalledWith("denies.example.test", { list: "search", readBy: "auto" }));
  });
});
