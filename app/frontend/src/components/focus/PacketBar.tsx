import type { PacketProgress } from "../../api/types/pipeline";

/** A packet on its way: how far it is, as a thin bar with its step in words. */
export default function PacketBar({ progress, words }: { progress: PacketProgress; words?: string }) {
  const pct = Math.round(Math.min(1, Math.max(0, progress.fraction)) * 100);
  return (
    <span className="fi-packet">
      <span className="fi-packet-words">{words ?? progress.label}</span>
      <span className="fi-packet-track" role="progressbar" aria-label="Packet" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-valuetext={`${progress.done} of ${progress.of} steps`}>
        <span className="fi-packet-fill" style={{ inlineSize: `${pct}%` }} />
      </span>
    </span>
  );
}
