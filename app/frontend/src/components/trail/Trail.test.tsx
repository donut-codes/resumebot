import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { fromOwnerLocalValue } from "../../lib/dates";
import type { TrailEntry, TrailPage } from "../../api/types/agents";
import { ApiError } from "../../api/client";
import Trail, { type TrailLoadOptions } from "./Trail";

function entry(overrides: Partial<TrailEntry> = {}): TrailEntry {
  return {
    id: `e-${Math.random()}`, at: "2026-10-01T12:00:00+00:00", who: "Scout", actor: "agent", event: "search_done",
    sentence: "Finished a search.", mark: "on", details: null, link: null, ...overrides,
  };
}

/** A time on the owner's clock on October `day`, 2026, as the API would send it (an instant). */
function at(day: number, hour: number, minute: number): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return fromOwnerLocalValue(`2026-10-${p(day)}T${p(hour)}:${p(minute)}`)!;
}

function page(items: TrailEntry[], next_before: string | null = null): TrailPage {
  return { items, next_before };
}

describe("Trail", () => {
  it("reads as a chat: the speaker in bold, a quiet time, then the message with its mark", async () => {
    render(<Trail load={() => Promise.resolve(page([entry({ at: at(3, 22, 18) })]))} loadKey="a" />);
    expect(await screen.findByText("Finished a search.")).toBeInTheDocument();
    const who = screen.getByText("Scout");
    expect(who.tagName).toBe("STRONG");
    const head = who.parentElement as HTMLElement;
    expect(within(head).getByText("10:18 pm")).toBeInTheDocument();
    // the speaker comes before the message in the page
    expect(who.compareDocumentPosition(screen.getByText("Finished a search."))).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByRole("img", { name: "Matched the plan" })).toBeInTheDocument();
  });

  it("puts consecutive messages from one speaker under one name", async () => {
    render(
      <Trail
        load={() => Promise.resolve(page([
          entry({ id: "1", sentence: "First.", at: at(3, 22, 18) }),
          entry({ id: "2", sentence: "Second.", at: at(3, 22, 12) }),
          entry({ id: "3", sentence: "Third.", who: "resumebot", actor: "app", at: at(3, 22, 5) }),
          entry({ id: "4", sentence: "Fourth.", at: at(3, 21, 40) }),
        ]))}
        loadKey="a"
      />,
    );
    await screen.findByText("First.");
    expect(screen.getAllByText("Scout")).toHaveLength(2); // one run of two, then another run of one
    expect(screen.getAllByText("resumebot")).toHaveLength(1);
    // a message after the first in a run carries its own quiet time; the first one's is on the name
    expect(screen.getAllByText("10:18 pm")).toHaveLength(1);
    expect(screen.getByText("10:12 pm")).toBeInTheDocument();
  });

  it("shows the date only as a small divider when the day changes", async () => {
    render(
      <Trail
        load={() => Promise.resolve(page([
          entry({ id: "1", sentence: "Late.", at: at(3, 22, 18) }),
          entry({ id: "2", sentence: "Earlier.", at: at(3, 9, 5) }),
          entry({ id: "3", sentence: "Yesterday.", at: at(2, 23, 50) }),
        ]))}
        loadKey="a"
      />,
    );
    await screen.findByText("Late.");
    expect(screen.getAllByText(/^October 3/)).toHaveLength(1);
    expect(screen.getAllByText(/^October 2/)).toHaveLength(1);
    expect(screen.queryByText(/October 3.*pm/)).toBeNull(); // never the date on the line itself
    // the same speaker on a new day starts a new run under the divider
    expect(screen.getAllByText("Scout")).toHaveLength(2);
  });

  it("leaves a neutral line unmarked and marks an off-plan one", async () => {
    render(
      <Trail
        load={() => Promise.resolve(page([
          entry({ id: "1", mark: "neutral", sentence: "Search started." }),
          entry({ id: "2", mark: "off", sentence: "A role didn't go to plan." }),
        ]))}
        loadKey="a"
      />,
    );
    await screen.findByText("Search started.");
    expect(screen.queryAllByRole("img", { name: "Matched the plan" })).toHaveLength(0);
    expect(screen.getAllByRole("img", { name: "Didn't match the plan" })).toHaveLength(1);
  });

  it("gives a message at most one Show more, holding words, evidence and steps", async () => {
    render(
      <Trail
        load={() => Promise.resolve(page([entry({
          details: { words: "I screened it out.", evidence: "pay is under the floor", steps: [{ at: "2026-10-01T12:00:00+00:00", text: "Read the posting" }] },
        })]))}
        loadKey="a"
      />,
    );
    await screen.findByText("Finished a search.");
    expect(screen.getAllByRole("button", { name: /show more|steps|evidence/i })).toHaveLength(1);
    expect(screen.queryByText("pay is under the floor")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Show more/ }));
    expect(screen.getByText(/I screened it out\./)).toBeInTheDocument();
    expect(screen.getByText("pay is under the floor")).toBeInTheDocument();
    expect(screen.getByText("Read the posting")).toBeInTheDocument();
  });

  it("shows no Show more when there is nothing behind a message", async () => {
    render(<Trail load={() => Promise.resolve(page([entry()]))} loadKey="a" />);
    await screen.findByText("Finished a search.");
    expect(screen.queryByRole("button", { name: /^Show more/ })).toBeNull();
  });

  it("opens a collapsed line into its own lines, each with its own Details", async () => {
    const child = (n: number) =>
      entry({ id: `c${n}`, sentence: `Checked role ${n}.`, event: "check_on_plan", details: { words: `Words ${n}`, evidence: null, steps: [] } });
    render(
      <Trail
        load={() => Promise.resolve(page([entry({ id: "g", sentence: "8 items matched the plan.", count: 8, entries: [child(1), child(2)] })]))}
        loadKey="a"
      />,
    );
    await screen.findByText("8 items matched the plan.");
    expect(screen.queryByText("Checked role 1.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Show the list/ }));
    expect(screen.getByText("Checked role 1.")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: /^Show more/ })[0]);
    expect(screen.getByText(/Words 1/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Hide the list/ }));
    expect(screen.queryByText("Checked role 1.")).toBeNull();
  });

  it("offers Everything and Needs a look only when asked, and reloads with the one picked", async () => {
    const seen: TrailLoadOptions[] = [];
    const load = (o: TrailLoadOptions) => {
      seen.push(o);
      return Promise.resolve(page([entry()]));
    };
    const { rerender } = render(<Trail load={load} loadKey="a" />);
    await screen.findByText("Finished a search.");
    expect(screen.queryByRole("button", { name: "Needs a look" })).toBeNull();
    rerender(<Trail load={load} loadKey="a" filters />);
    fireEvent.click(screen.getByRole("button", { name: "Needs a look" }));
    await waitFor(() => expect(seen.at(-1)?.filter).toBe("needs_look"));
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(expect.arrayContaining(["Everything", "Needs a look"]));
  });

  it("says what it means when a filter finds nothing", async () => {
    render(<Trail load={() => Promise.resolve(page([]))} loadKey="a" filters empty="Nothing yet." />);
    expect(await screen.findByText("Nothing yet.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Needs a look" }));
    expect(await screen.findByText("Nothing needs a look.")).toBeInTheDocument();
  });

  it("pages older lines with the cursor the server gave", async () => {
    const seen: TrailLoadOptions[] = [];
    const load = (o: TrailLoadOptions) => {
      seen.push(o);
      return Promise.resolve(
        o.before ? page([entry({ id: "old", sentence: "Older line." })], null) : page([entry({ id: "new" })], "2026-09-30T00:00:00Z"),
      );
    };
    render(<Trail load={load} loadKey="a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Older" }));
    expect(await screen.findByText("Older line.")).toBeInTheDocument();
    expect(seen.at(-1)?.before).toBe("2026-09-30T00:00:00Z");
    expect(screen.queryByRole("button", { name: "Older" })).toBeNull();
  });

  it("keeps the Older button and says so when the older lines would not load", async () => {
    let fail = true;
    const load = (o: TrailLoadOptions) =>
      o.before
        ? fail ? Promise.reject(new ApiError("No.", 500)) : Promise.resolve(page([entry({ id: "old", sentence: "Older line." })]))
        : Promise.resolve(page([entry({ id: "new" })], "2026-09-30T00:00:00Z"));
    render(<Trail load={load} loadKey="a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Older" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the older ones.");
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Older" }));
    expect(await screen.findByText("Older line.")).toBeInTheDocument();
  });

  it("drops older lines that arrive after the filter changed, so the new list is not mixed with the old one's", async () => {
    let release: (p: TrailPage) => void = () => {};
    const load = (o: TrailLoadOptions) => {
      if (o.before) return new Promise<TrailPage>((resolve) => { release = resolve; });
      return Promise.resolve(page([entry({ id: `first-${o.filter}`, sentence: `First page of ${o.filter}.` })], o.filter === "all" ? "2026-09-30T00:00:00Z" : null));
    };
    render(<Trail load={load} loadKey="a" filters />);
    fireEvent.click(await screen.findByRole("button", { name: "Older" }));
    fireEvent.click(screen.getByRole("button", { name: "Needs a look" }));
    expect(await screen.findByText("First page of needs_look.")).toBeInTheDocument();
    await act(async () => release(page([entry({ id: "late", sentence: "Late line from the old filter." })])));
    expect(screen.queryByText("Late line from the old filter.")).toBeNull();
    expect(screen.getByText("First page of needs_look.")).toBeInTheDocument();
  });

  it("does not offer Older when paging is off", async () => {
    render(<Trail load={() => Promise.resolve(page([entry()], "2026-09-30T00:00:00Z"))} loadKey="a" paged={false} />);
    await screen.findByText("Finished a search.");
    expect(screen.queryByRole("button", { name: "Older" })).toBeNull();
  });

  it("shows the message when loading fails", async () => {
    render(<Trail load={() => Promise.reject(new ApiError("That didn't load.", 500))} loadKey="a" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("That didn't load.");
  });

  it("reloads from the top when its key changes", async () => {
    const load = vi.fn(() => Promise.resolve(page([entry()])));
    const { rerender } = render(<Trail load={load} loadKey="a" />);
    await screen.findByText("Finished a search.");
    rerender(<Trail load={load} loadKey="b" />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
  });

  it("links a line's search and role when it is given a way to open them", async () => {
    const onOpenSearch = vi.fn();
    const onOpenRole = vi.fn();
    render(
      <Trail
        load={() => Promise.resolve(page([
          entry({ id: "1", sentence: "Reported 12 roles.", link: { kind: "search_run", id: "r1" } }),
          entry({ id: "2", sentence: "Kept a role.", link: { kind: "opportunity", id: "o1" } }),
        ]))}
        loadKey="a"
        onOpenSearch={onOpenSearch}
        onOpenRole={onOpenRole}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "See the search" }));
    expect(onOpenSearch).toHaveBeenCalledWith("r1");
    fireEvent.click(screen.getByRole("button", { name: "See the role" }));
    expect(onOpenRole).toHaveBeenCalledWith("o1");
  });

  it("shows plain text, no links, without openers", async () => {
    render(<Trail load={() => Promise.resolve(page([entry({ link: { kind: "search_run", id: "r1" } })]))} loadKey="a" />);
    await screen.findByText("Finished a search.");
    expect(screen.queryByRole("button", { name: "See the search" })).toBeNull();
  });

  it("spaces a line's list and its link apart", async () => {
    render(
      <Trail
        load={() => Promise.resolve(page([
          entry({
            id: "g", event: "role_screened", sentence: "2 roles were screened out.", count: 2,
            link: { kind: "search_run", id: "r1" }, entries: [entry({ id: "c1" }), entry({ id: "c2" })],
          }),
        ]))}
        loadKey="a"
        onOpenSearch={vi.fn()}
      />,
    );
    const list = await screen.findByRole("button", { name: /Show the list/ });
    expect(list.parentElement).toHaveTextContent("Show the list · See the search");
  });

  it("a line for a search an agent was handed offers 'What was sent', and only when it can open", async () => {
    const lines = [entry({ id: "1", sentence: "Started a search.", sent_task_id: "r9" }), entry({ id: "2", sentence: "You asked." })];
    const onOpenSent = vi.fn();
    const { rerender } = render(<Trail load={() => Promise.resolve(page(lines))} loadKey="a" onOpenSent={onOpenSent} />);
    const buttons = await screen.findAllByRole("button", { name: "What was sent" });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(onOpenSent).toHaveBeenCalledWith("r9");
    rerender(<Trail load={() => Promise.resolve(page(lines))} loadKey="a" />);
    expect(screen.queryByRole("button", { name: "What was sent" })).toBeNull();
  });
});
