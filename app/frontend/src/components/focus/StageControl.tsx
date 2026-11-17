import { useLayoutEffect, useRef, useState } from "react";
import { STAGE_STEPS } from "./model";

interface Props {
  status: string;
  busy: boolean;
  onMove: (to: string) => void;
}

/** One click puts the role in a stage. The marker slides to the stage it is in. */
export default function StageControl({ status, busy, onMove }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const [thumb, setThumb] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  useLayoutEffect(() => {
    const place = () => {
      const on = box.current?.querySelector<HTMLElement>('[aria-pressed="true"]');
      setThumb(on ? { left: on.offsetLeft, top: on.offsetTop, width: on.offsetWidth, height: on.offsetHeight } : null);
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [status]);
  return (
    <div className="fi-seg" role="group" aria-label="Stage" ref={box}>
      <span className="fi-seg-thumb" style={thumb ? { insetInlineStart: thumb.left, insetBlockStart: thumb.top, inlineSize: thumb.width, blockSize: thumb.height } : { inlineSize: 0, opacity: 0 }} />
      {STAGE_STEPS.map((s) => (
        <button key={s.status} type="button" aria-pressed={s.status === status} disabled={busy} onClick={() => s.status !== status && onMove(s.status)}>
          {s.short}
        </button>
      ))}
    </div>
  );
}
