import { Component, type ErrorInfo, type ReactNode } from "react";
import ErrorBanner from "./ErrorBanner";

interface Props {
  children: ReactNode;
  /** When this changes (the address you are on), a screen that broke gets a fresh try. */
  resetKey: string;
}

interface State {
  failed: boolean;
}

/** Catches a screen that cannot draw what it was given (a half-written packet, say), so one bad record shows a plain
 *  line and a Reload button instead of a blank page. Moving to another page clears it. */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("A screen could not be drawn", error, info.componentStack);
  }

  componentDidUpdate(prev: Props) {
    if (this.state.failed && prev.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <ErrorBanner style={{ margin: "var(--fi-s9) 0" }}>
        <p>Something on this page could not be shown. Reloading may fix it; another page still works.</p>
        <p>
          <button type="button" className="fi-btn" onClick={() => window.location.reload()}>
            Reload
          </button>
        </p>
      </ErrorBanner>
    );
  }
}
