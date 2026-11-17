import type { CSSProperties, ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** The element it renders as; most screens use a block, a few sit inside a line of text. */
  as?: "div" | "p" | "span";
  /** Announced to a screen reader as an alert (the default); a few inline notes are not. */
  alert?: boolean;
  style?: CSSProperties;
}

/** The one error line every screen uses: `.error-banner`, announced as an alert. */
export default function ErrorBanner({ children, as: Tag = "div", alert = true, style }: Props) {
  return (
    <Tag className="error-banner" role={alert ? "alert" : undefined} style={style}>
      {children}
    </Tag>
  );
}
