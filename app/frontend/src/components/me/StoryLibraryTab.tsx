import { api } from "../../api/client";
import type { StoryBlock, StoryLine } from "../../api/types/me";
import { useRefreshKey } from "../../lib/appContext";
import { dateInWords } from "../../lib/dates";
import { useResource } from "../../lib/useResource";
import { MeAlert, MeLoading } from "./MeSection";

function Held({ held, why, children }: { held: boolean; why?: string; children: string }) {
  return (
    <li data-held={held ? "true" : undefined}>
      {children}
      {held && why ? <span className="fi-me-held"> (Held: {why})</span> : null}
    </li>
  );
}

const line = (l: StoryLine) => (
  <Held key={l.id} held={l.held} why={l.why_held}>
    {l.text}
  </Held>
);

function status(version: number, builtAt: string | null, building: boolean, current: boolean): string {
  if (building) return "Building…";
  if (!current) return `Version ${version}, built from an earlier master resume or writing rules.`;
  const when = builtAt ? ` on ${dateInWords(builtAt)}` : "";
  return `Version ${version} built${when}.`;
}

/** The story library, read-only: each role's scope line and lines, the achievements, and the stories with their blocks. */
export default function StoryLibraryTab() {
  const refreshKey = useRefreshKey();
  const lib = useResource(() => api.storyLibrary(), [refreshKey], {
    fallback: "Couldn't load the story library.",
  });
  const d = lib.data;
  if (!d) return lib.error ? <MeAlert>{lib.error}</MeAlert> : <MeLoading>Loading the story library&hellip;</MeLoading>;
  if (d.version === null) {
    return <p className="fi-me-quiet">{d.building ? "Building…" : "No story library has been built yet."}</p>;
  }
  return (
    <div className="fi-me-story">
      <p className="fi-me-quiet">{status(d.version, d.built_at, d.building, d.current)}</p>
      {d.roles.map((r) => (
        <section key={r.role} className="fi-sheet fi-me-story-part" aria-label={`${r.title}, ${r.employer}`}>
          <h3>
            {r.title}, {r.employer}
          </h3>
          <ul>
            {r.scope && <Held held={r.scope.held} why={r.scope.why_held}>{r.scope.text}</Held>}
            {r.lines.map(line)}
          </ul>
        </section>
      ))}
      {d.achievements.length > 0 && (
        <section className="fi-sheet fi-me-story-part" aria-label="Achievements">
          <h3>Achievements</h3>
          <ul>{d.achievements.map(line)}</ul>
        </section>
      )}
      {d.stories.map((s) => (
        <section key={s.id} className="fi-sheet fi-me-story-part" aria-label={s.title}>
          <h3>{s.title}</h3>
          <ul>
            {s.blocks.map((b: StoryBlock) => (
              <Held key={b.id} held={b.held} why={b.why_held}>
                {b.text}
              </Held>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
