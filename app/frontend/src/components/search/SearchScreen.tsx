// Search Settings (the screens rebuild spec, screen 3): tell the search where to look, what to look for, what to
// rule out and how to rank, and read how it works. The rail holds the six parts; each part's list holds its items and
// the pane edits the open one, each field saving itself (About has no list: the pane takes its place). Everything comes
// from the search store, read once a visit and kept fresh by the live stream; nothing here calls the API.

import { useEffect, useState } from "react";
import { useRefreshKey, useRoute } from "../../lib/appContext";
import { PROFILE_TABS, routeFor, type ProfileTab } from "../../lib/route";
import { SAID_TOAST_MS } from "../../lib/useDraftPage";
import { NARROW, useMedia } from "../../lib/useMedia";
import { useSearch, useSearchStore } from "../../state/searchStore";
import { Rail, RailEntry, RailGroup } from "../focus/parts";
import { useToast } from "../toast/ToastProvider";
import AlsoPart from "./AlsoPart";
import AboutPart from "./AboutPart";
import HardPassPart from "./HardPassPart";
import LookForPart from "./LookForPart";
import PrioritizationPart from "./PrioritizationPart";
import WherePart from "./WherePart";

export const PART_LABEL: Record<ProfileTab, string> = {
  where: "Where to look",
  criteria: "What to look for",
  gates: "Hard pass",
  scoring: "Prioritization",
  also: "Also sent to agents",
  about: "About",
};

/** What every part is handed: the item open in its pane (null: none picked), how to open one, and whether the window is a
 * phone's (where the pane replaces the list, and nothing opens by itself). */
export interface PartProps {
  open: string | null;
  onOpen: (key: string | null) => void;
  narrow: boolean;
}

export default function SearchScreen() {
  const store = useSearchStore();
  const { route, go } = useRoute();
  const narrow = useMedia(NARROW);
  const { showToast } = useToast();
  const part = route.profilePart;
  const status = useSearch((s) => s.status);
  const error = useSearch((s) => s.error);
  const said = useSearch((s) => s.said);
  const needsYou = useSearch((s) => (s.sites?.sites ?? []).filter((x) => x.on && x.state === "needs_you").length);
  // The item open in each part. A target role named in the address (#/profile/criteria?role=<id>) opens it.
  const [open, setOpen] = useState<Partial<Record<ProfileTab, string | null>>>(() => (route.profileRole ? { criteria: `role:${route.profileRole}` } : {}));

  useEffect(() => store.start(), [store]);
  // Clicking the tab you are on reads it again.
  const refreshKey = useRefreshKey();
  useEffect(() => {
    if (refreshKey > 0) void store.reload();
  }, [refreshKey, store]);
  // What a save changed, said once.
  useEffect(() => {
    if (!said) return;
    showToast(said, SAID_TOAST_MS);
    store.dismissSaid();
  }, [said, showToast, store]);

  const onOpen = (key: string | null) => {
    store.quiet();
    setOpen((o) => ({ ...o, [part]: key }));
    if (part === "criteria" && route.profileRole) go(routeFor("profile", { profilePart: "criteria" }));
  };
  const pick = (p: ProfileTab) => {
    store.quiet();
    go(routeFor("profile", { profilePart: p }));
  };

  const roleIds = useSearch((s) => (s.profile?.criteria.target_roles ?? []).map((r) => r.id).join(" "));
  const picked = open[part] ?? null;
  // A target role the address named that is not there opens nothing.
  const shown = picked?.startsWith("role:") && status === "ready" && !roleIds.split(" ").includes(picked.slice(5)) ? null : picked;
  const props: PartProps = { open: shown, onOpen, narrow };
  const wide = part === "about" || part === "also";

  return (
    <div className={`fi fi-ss${wide ? " is-wide" : ""}${shown && !wide ? " is-on-role" : ""}`}>
      <Rail label="Search Settings">
        <RailGroup>
          {PROFILE_TABS.map((p) => (
            <RailEntry
              key={p}
              label={PART_LABEL[p]}
              count={p === "where" && needsYou > 0 ? needsYou : null}
              yours={p === "where" && needsYou > 0}
              current={p === part}
              onPick={() => pick(p)}
            />
          ))}
        </RailGroup>
      </Rail>
      {part === "where" ? (
        <WherePart {...props} />
      ) : part === "about" ? (
        <AboutPart />
      ) : status === "error" ? (
        <section className="fi-list" aria-label={PART_LABEL[part]}>
          <p className="fi-alert" role="alert">
            {error}{" "}
            <button type="button" className="fi-link" onClick={() => void store.reload()}>
              Try again
            </button>
          </p>
        </section>
      ) : status !== "ready" ? (
        <section className="fi-list" aria-label={PART_LABEL[part]}>
          <p className="fi-empty">Loading…</p>
        </section>
      ) : part === "also" ? (
        <AlsoPart />
      ) : part === "criteria" ? (
        <LookForPart {...props} />
      ) : part === "gates" ? (
        <HardPassPart {...props} />
      ) : (
        <PrioritizationPart {...props} />
      )}
    </div>
  );
}
