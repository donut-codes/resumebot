import { screen } from "@testing-library/react";
import type { StoryLibrary } from "../../api/types/me";
import { json, render, stubFetch } from "../../test-utils";
import StoryLibraryTab from "./StoryLibraryTab";

// Made up: no real person's resume.
const LIB: StoryLibrary = {
  version: 3, built_at: "2026-10-20T10:00:00Z", current: true, building: false,
  roles: [{
    role: "lead", title: "Senior Manager", employer: "Northwind Labs",
    scope: { id: "s1", text: "Led a practice of six.", from: ["b1"], held: false },
    lines: [
      { id: "l1", text: "Cut review time in half.", from: ["b1"], held: false },
      { id: "l2", text: "Ran a vague thing.", from: ["b2"], held: true, why_held: "no result stated" },
    ],
  }],
  achievements: [{ id: "a1", text: "Launched a statewide project.", from: [], held: false }],
  stories: [{ id: "st1", title: "The risk register", index: 1, blocks: [{ id: "k1", for: "situation", text: "Risks lived in email.", from: [], held: false }] }],
};

describe("StoryLibraryTab", () => {
  it("shows the status, each role's lines, a held line muted with its reason, achievements and stories", async () => {
    stubFetch(() => json(LIB));
    render(<StoryLibraryTab />);
    expect(await screen.findByText("Cut review time in half.")).toBeInTheDocument();
    expect(screen.getByText(/Version 3 built/)).toBeInTheDocument();
    expect(screen.getByText("Senior Manager, Northwind Labs")).toBeInTheDocument();
    expect(screen.getByText("Led a practice of six.")).toBeInTheDocument();
    const held = screen.getByText(/Ran a vague thing\./).closest("li")!;
    expect(held).toHaveAttribute("data-held", "true");
    expect(held).toHaveTextContent("no result stated");
    expect(screen.getByText("Launched a statewide project.")).toBeInTheDocument();
    expect(screen.getByText("Risks lived in email.")).toBeInTheDocument();
  });

  it("says so when no library has been built", async () => {
    stubFetch(() => json({ ...LIB, version: null, built_at: null, roles: [], achievements: [], stories: [] }));
    render(<StoryLibraryTab />);
    expect(await screen.findByText("No story library has been built yet.")).toBeInTheDocument();
  });
});
