import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ErrorBoundary from "./ErrorBoundary";

function Broken({ bad }: { bad: boolean }) {
  if (bad) throw new Error("cannot draw this");
  return <p>All drawn</p>;
}

describe("ErrorBoundary", () => {
  beforeEach(() => {
    // React logs the caught error to the console; the test expects it.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows a plain line and a Reload button when a screen cannot draw, not a blank page", () => {
    render(
      <ErrorBoundary resetKey="#/a">
        <Broken bad />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("could not be shown");
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("moving to another page gives the screen a fresh try", () => {
    const { rerender } = render(
      <ErrorBoundary resetKey="#/a">
        <Broken bad />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    rerender(
      <ErrorBoundary resetKey="#/b">
        <Broken bad={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("All drawn")).toBeInTheDocument();
  });
});
