import type { EndedReason } from "../../api/types/pipeline";
import { Menu } from "./parts";

interface Props {
  reasons: EndedReason[];
  busy: boolean;
  onClose: (reason: string) => void;
}

/** Close asks why: the role is kept in Closed with the reason picked. */
export default function CloseMenu({ reasons, busy, onClose }: Props) {
  return <Menu label="Close" heading="Why close it?" items={reasons.map((r) => ({ key: r.reason, label: r.label }))} onPick={onClose} disabled={busy} />;
}
