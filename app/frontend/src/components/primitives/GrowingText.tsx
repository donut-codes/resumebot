import { useCallback, useEffect, useLayoutEffect, useRef, type TextareaHTMLAttributes } from "react";

/** A text box that grows with its text, so what was written is read whole instead of behind a scrollbar. `oneLine`
 * keeps it one line of words (Enter adds no line break, and a pasted break becomes a space) while still wrapping. It is
 * fitted again when its width changes and when the page's face has loaded, so a line that fits once the face is drawn
 * does not keep the room a wider fallback face asked for. */
export default function GrowingText({ value, onText, oneLine = false, minRows = 1, className = "", ...rest }: {
  value: string;
  onText: (text: string) => void;
  oneLine?: boolean;
  minRows?: number;
} & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange" | "rows">) {
  const box = useRef<HTMLTextAreaElement>(null);
  const fit = useCallback(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
  }, []);
  useLayoutEffect(fit, [value, fit]);
  useEffect(() => {
    const el = box.current;
    if (!el) return undefined;
    let width = el.offsetWidth;
    const watch =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => {
            // Only a change of width: the box's own growing changes its height, which must not fit it again.
            if (el.offsetWidth === width) return;
            width = el.offsetWidth;
            fit();
          });
    watch?.observe(el);
    void document.fonts?.ready.then(fit);
    return () => watch?.disconnect();
  }, [fit]);
  return (
    <textarea
      {...rest}
      ref={box}
      rows={minRows}
      className={`growing-text ${className}`.trim()}
      value={value}
      onKeyDown={(e) => {
        if (oneLine && e.key === "Enter") e.preventDefault();
        rest.onKeyDown?.(e);
      }}
      onChange={(e) => onText(oneLine ? e.target.value.replace(/\s*\n\s*/g, " ") : e.target.value)}
    />
  );
}
