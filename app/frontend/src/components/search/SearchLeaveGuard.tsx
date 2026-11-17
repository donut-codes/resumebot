// Search Settings' fields save themselves, and a save of Hard pass is answered only when its re-gate ends. An edit made
// while that is going waits in this page, and a page closed or reloaded then loses it without a word. So while the store has a save on its way or an edit waiting, the browser asks before the page is left.
// Moving to another address loses nothing (the store lives above the screens and keeps sending), so nothing else asks.

import { usePageLeaveGuard } from "../../lib/unsavedGuard";
import { useSearch } from "../../state/searchStore";

export default function SearchLeaveGuard() {
  usePageLeaveGuard(useSearch((s) => s.pending));
  return null;
}
