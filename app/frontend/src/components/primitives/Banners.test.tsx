import { render, screen } from "@testing-library/react";
import EmptyState from "./EmptyState";
import ErrorBanner from "./ErrorBanner";
import Loading from "./Loading";

describe("ErrorBanner", () => {
  it("is a block alert by default", () => {
    render(<ErrorBanner>Couldn&rsquo;t load that.</ErrorBanner>);
    const el = screen.getByRole("alert");
    expect(el.tagName).toBe("DIV");
    expect(el).toHaveClass("error-banner");
    expect(el).toHaveTextContent("Couldn’t load that.");
  });

  it("can be a paragraph or an inline note without the alert role, with a style", () => {
    const { container } = render(
      <>
        <ErrorBanner as="p">one</ErrorBanner>
        <ErrorBanner as="span" alert={false} style={{ margin: 4 }}>two</ErrorBanner>
      </>,
    );
    expect(container.querySelector("p.error-banner")).toHaveAttribute("role", "alert");
    const span = container.querySelector("span.error-banner");
    expect(span).not.toHaveAttribute("role");
    expect(span).toHaveStyle({ margin: "4px" });
  });
});

describe("Loading", () => {
  it("says Loading… unless told what is loading, and can be a paragraph", () => {
    const { container } = render(
      <>
        <Loading />
        <Loading as="p">Loading your roles…</Loading>
      </>,
    );
    expect(container.querySelector("div.loading")).toHaveTextContent("Loading…");
    expect(container.querySelector("p.loading")).toHaveTextContent("Loading your roles…");
  });
});

describe("EmptyState", () => {
  it("is the regular or the small empty line", () => {
    const { container } = render(
      <>
        <EmptyState>Nothing yet.</EmptyState>
        <EmptyState small>Nothing open.</EmptyState>
      </>,
    );
    expect(container.querySelector("p.empty-state")).toHaveTextContent("Nothing yet.");
    expect(container.querySelector("p.empty-state-small")).toHaveTextContent("Nothing open.");
  });
});
