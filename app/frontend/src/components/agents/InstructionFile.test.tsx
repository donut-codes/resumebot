import { fireEvent, screen, waitFor } from "@testing-library/react";
import type { HarnessFile, HarnessFileReply } from "../../api/types/agents";
import { json, render, stubFetch } from "../../test-utils";
import InstructionFile from "./InstructionFile";

const FILE: HarnessFile = {
  kind: "instructions", name: "jobsearch.scout", title: "Instructions for this step", text: "# Search\nReport with {{report_call}}.",
  chars: 38, placeholders: ["report_call"], version: 1, edited: false, busy: null,
};

function reply(over: Partial<HarnessFile>, versions: HarnessFileReply["history"]["versions"]): HarnessFileReply {
  const { title: _title, ...file } = { ...FILE, ...over };
  return {
    file,
    history: { kind: "instructions", name: file.name, current: file.version, versions },
  };
}

const V1 = { version: 1, at: null, by: "Shipped with the app", why: "As shipped with the app.", chars: 38, current: false, shipped: true };
const V2 = { version: 2, at: "2026-10-12T14:00:00Z", by: "You", why: "say the board", chars: 60, current: true, shipped: false };

function stub(handler: (method: string, url: string, body: unknown) => unknown) {
  const calls: { method: string; url: string; body: unknown }[] = [];
  stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ method, url: String(input), body });
    const out = handler(method, String(input), body);
    return out === undefined ? json({}, 404) : json(out);
  });
  return calls;
}

function open() {
  fireEvent.click(screen.getByRole("button", { name: /Show instructions for this step/ }));
}

describe("InstructionFile", () => {
  it("says 'shipped with the app' for a version a release saved, not 'your change'", () => {
    render(<InstructionFile file={{ ...FILE, version: 2, edited: true, from_app: true }} />);
    expect(screen.getByText(/version 2, shipped with the app/)).toBeInTheDocument();
    expect(screen.queryByText(/your change/)).toBeNull();
  });

  it("shows the version in force and the text, and edits it: Save needs a change and a why, and sends both", async () => {
    const edited = "# Search\nReport with {{report_call}}.\nSay the board.";
    const calls = stub((method) => (method === "PUT" ? reply({ text: edited, chars: 52, version: 2, edited: true }, [V2, { ...V1 }]) : undefined));
    render(<InstructionFile file={FILE} />);
    expect(screen.getByText(/version 1, as shipped/)).toBeInTheDocument();
    open();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const save = screen.getByRole("button", { name: "Save as a new version" });
    expect(screen.getByText(/The app fills in \{\{report_call\}\} for each task/)).toBeInTheDocument();
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Edit jobsearch.scout"), { target: { value: edited } });
    expect(save).toBeDisabled(); // no why yet
    fireEvent.change(screen.getByLabelText("Why are you changing it?"), { target: { value: "say the board" } });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await screen.findByText(/version 2, your change/);
    expect(calls.filter((c) => c.method === "PUT")).toEqual([
      expect.objectContaining({ body: { text: edited, why: "say the board" }, url: expect.stringContaining("/harness/files/instructions/jobsearch.scout") }),
    ]);
    expect(screen.getByText(/Say the board\./)).toBeInTheDocument();
  });

  it("says why a save was refused and keeps the draft", async () => {
    stubFetch(() => json({ detail: "Scout is working with this file right now. Save your change once it has finished." }, 409));
    render(<InstructionFile file={FILE} />);
    open();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Edit jobsearch.scout"), { target: { value: "# Other" } });
    fireEvent.change(screen.getByLabelText("Why are you changing it?"), { target: { value: "why" } });
    fireEvent.click(screen.getByRole("button", { name: "Save as a new version" }));
    expect(await screen.findByText(/Scout is working with this file/)).toBeInTheDocument();
    expect(screen.getByLabelText("Edit jobsearch.scout")).toHaveValue("# Other");
  });

  it("takes a file and a why of any length, and a file too large to send an agent keeps its words and shows the sentence", async () => {
    const sentence =
      "This file is 150,000 characters, and the file sent with it is 20,000. An agent can be sent at most 160,000 of the two at once, so it was not saved. Nothing was cut. Shorten one of them.";
    const sent: unknown[] = [];
    stubFetch((_input: RequestInfo | URL, init?: RequestInit) => {
      sent.push(init?.body ? JSON.parse(String(init.body)) : null);
      return json({ detail: sentence }, 413);
    });
    render(<InstructionFile file={FILE} />);
    open();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Edit jobsearch.scout")).not.toHaveAttribute("maxlength");
    expect(screen.getByLabelText("Why are you changing it?")).not.toHaveAttribute("maxlength");
    const text = "# Search\nOne more line the owner wrote for the agent.\n".repeat(400);
    const why = "One more reason the owner gives. ".repeat(160).trim();
    expect(why.length).toBeGreaterThan(5000);
    fireEvent.change(screen.getByLabelText("Edit jobsearch.scout"), { target: { value: text } });
    fireEvent.change(screen.getByLabelText("Why are you changing it?"), { target: { value: why } });
    fireEvent.click(screen.getByRole("button", { name: "Save as a new version" }));
    // The page's banner says it, in the server's words; the form stays open with everything typed.
    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(sent).toEqual([{ text, why }]);
    expect(screen.getByLabelText("Edit jobsearch.scout")).toHaveValue(text);
    expect(screen.getByLabelText("Why are you changing it?")).toHaveValue(why);
    expect(screen.getByRole("button", { name: "Save as a new version" })).toBeEnabled();
  });

  it("can't be edited while an agent is working with the file, and says so", () => {
    stub(() => undefined);
    render(<InstructionFile file={{ ...FILE, busy: "Scout is working with this file right now." }} />);
    open();
    expect(screen.getByText("Scout is working with this file right now.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeDisabled();
  });

  it("lists the history, views an older version and restores it as a new one", async () => {
    const restored = reply({ text: FILE.text, version: 3, edited: true }, [
      { ...V2, version: 3, why: "Restored version 1.", current: true }, { ...V2, current: false }, V1,
    ]);
    const calls = stub((method, url) => {
      if (method === "POST" && url.includes("/restore")) return restored;
      if (url.includes("/versions/1")) return { version: 1, text: "# The shipped text", by: "Shipped with the app", why: "As shipped with the app.", at: null };
      if (url.includes("/harness/files/")) return reply({ text: "# Mine", version: 2, edited: true }, [V2, V1]);
      return undefined;
    });
    render(<InstructionFile file={{ ...FILE, version: 2, edited: true }} />);
    open();
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    const list = await screen.findByRole("list", { name: "Versions of jobsearch.scout" });
    expect(list.textContent).toContain("Version 2 (in force)");
    expect(list.textContent).toContain("say the board");
    expect(list.textContent).toContain("as shipped");
    expect(screen.getAllByRole("button", { name: /Restore this version/ })).toHaveLength(1); // not on the one in force
    fireEvent.click(screen.getByRole("button", { name: /View\s*version 1/ }));
    expect(await screen.findByText("# The shipped text")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Restore this version/ }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.includes("/restore"))).toBe(true));
    expect(calls.find((c) => c.url.includes("/restore"))!.body).toEqual({ version: 1 });
    await screen.findByText(/version 3, your change/);
  });

  it("shows no banner and no button about a newer shipped version: a release applies on its own", () => {
    render(<InstructionFile file={{ ...FILE, version: 3, edited: true }} />);
    open();
    expect(screen.queryByText(/ships a newer version/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Use the shipped version" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Keep my version/)).not.toBeInTheDocument();
  });
});
