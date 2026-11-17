import { useId, useState } from "react";
import type { Packet, PacketResume } from "../../api/types/pipeline";
import PacketBar from "./PacketBar";

const STATUS_WORDS: Record<string, string> = {
  writing: "Being written",
  ready: "Ready",
  approved: "Ready",
  held: "Held",
  replaced: "Replaced",
  failed: "Not written",
};

interface Props {
  packets: Packet[];
  busy: boolean;
  /** Claude's peak hours: what starts Claude work is off. */
  blocked: boolean;
  onAsk: (feedback: string) => void;
  onSaveFiles: (packetId: string) => void;
}

function Resume({ r }: { r: PacketResume }) {
  return (
    <article className="fi-doc" aria-label="The tailored resume">
      {r.name && <h4 className="fi-doc-name">{r.name}</h4>}
      {r.contact && <p className="fi-doc-quiet">{r.contact.join(" | ")}</p>}
      {r.headline && <p className="fi-doc-lead">{r.headline}</p>}
      {r.summary && (
        <section>
          <h5>Summary</h5>
          <p>{r.summary}</p>
        </section>
      )}
      {r.competencies && r.competencies.length > 0 && (
        <section>
          <h5>Core competencies</h5>
          {r.competencies.map((c) => (
            <p key={c.area}>
              <b>{c.area}:</b> {c.items.join(" · ")}
            </p>
          ))}
        </section>
      )}
      {r.achievements && r.achievements.length > 0 && (
        <section>
          <h5>Selected achievements</h5>
          <ul>
            {r.achievements.map((a, i) => (
              <li key={i}>{a.text}</li>
            ))}
          </ul>
        </section>
      )}
      {r.roles && r.roles.length > 0 && (
        <section>
          <h5>Experience</h5>
          {r.roles.map((role, i) => (
            <div key={`${role.role}-${i}`} className="fi-doc-role">
              <p>
                <b>{role.title}</b>
                {[role.employer, role.location].filter(Boolean).length > 0 && <>, {[role.employer, role.location].filter(Boolean).join(", ")}</>}
                {(role.start || role.end) && <span className="fi-doc-quiet"> | {[role.start, role.end].filter(Boolean).join(" – ")}</span>}
              </p>
              {role.intro && <p>{role.intro}</p>}
              <ul>
                {(role.bullets ?? []).map((b, j) => (
                  <li key={j}>{b.text}</li>
                ))}
              </ul>
            </div>
          ))}
        </section>
      )}
      {r.education && r.education.length > 0 && (
        <section>
          <h5>Education</h5>
          {r.education.map((e) => (
            <p key={e.degree}>
              <b>{e.degree}</b>
              {e.school && `, ${e.school}`}
            </p>
          ))}
        </section>
      )}
    </article>
  );
}

/** The role's packet: its versions, the tailored resume and letter to read and download, its checks and reasons, and
 * asking the writer for it or for changes. */
export default function PacketPart({ packets, busy, blocked, onAsk, onSaveFiles }: Props) {
  const [picked, setPicked] = useState<string | null>(null);
  const [feedback, setFeedback] = useState("");
  const [doc, setDoc] = useState<"resume" | "letter">("resume");
  const feedbackId = useId();
  const shown = packets.find((p) => p.id === picked) ?? packets[0];
  const offBecause = blocked ? "Claude's peak hours: this waits until they end" : undefined;

  if (!shown) {
    return (
      <section className="fi-sec" aria-label="Packet">
        <h3>Packet</h3>
        <div className="fi-card fi-packet-none">
          <p>Nothing yet. Launch in Browser builds it, or build it now.</p>
          <button type="button" className="fi-btn" disabled={busy || blocked} title={offBecause} onClick={() => onAsk("")}>
            {busy ? "Starting…" : "Build now"}
          </button>
        </div>
      </section>
    );
  }

  const letter = shown.letter;
  const hasLetter = !!letter.paragraphs?.length;
  const failed = shown.checks.filter((c) => c.blocking && !c.ok);
  const done = shown.status !== "writing" && shown.status !== "failed";
  const r = shown.reasoning;

  return (
    <section className="fi-sec" aria-label="Packet">
      <h3>
        Packet <span>Version {shown.version}: {STATUS_WORDS[shown.status] ?? shown.status}</span>
      </h3>
      {packets.length > 1 && (
        <div className="fi-chips" role="group" aria-label="Versions">
          {packets.map((p) => (
            <button key={p.id} type="button" className="fi-chip" aria-pressed={p.id === shown.id} onClick={() => setPicked(p.id)}>
              Version {p.version}
            </button>
          ))}
        </div>
      )}

      {shown.status === "writing" && (
        <div className="fi-card">{shown.progress ? <PacketBar progress={shown.progress} /> : <p>The writer is on version {shown.version}.</p>}</div>
      )}
      {shown.status === "failed" && <div className="fi-card"><p>Version {shown.version} wasn&rsquo;t written. Build it again below.</p></div>}

      {done && (
        <>
          <div className="fi-downloads">
            <a className="fi-btn" href={`/api/v1/packets/${shown.id}/resume.pdf`}>Resume (.pdf)</a>
            <a className="fi-btn" href={`/api/v1/packets/${shown.id}/resume.docx`}>Resume (.docx)</a>
            {hasLetter && <a className="fi-btn" href={`/api/v1/packets/${shown.id}/letter.pdf`}>Letter (.pdf)</a>}
            {hasLetter && <a className="fi-btn" href={`/api/v1/packets/${shown.id}/letter.docx`}>Letter (.docx)</a>}
          </div>
          {shown.files_problem && (
            <p className="fi-alert">
              The files couldn&rsquo;t be saved to your packet folder: {shown.files_problem}{" "}
              <button type="button" className="fi-btn" disabled={busy} onClick={() => onSaveFiles(shown.id)}>Save files again</button>
            </p>
          )}
          {failed.length > 0 && (
            <p className="fi-alert">Held: the checks found problems the writer couldn&rsquo;t fix. Ask for changes below, or move the role on yourself.</p>
          )}
          <div className="fi-seg fi-doc-switch" role="group" aria-label="Document">
            <button type="button" aria-pressed={doc === "resume"} onClick={() => setDoc("resume")}>Resume</button>
            <button type="button" aria-pressed={doc === "letter"} onClick={() => setDoc("letter")}>Cover letter</button>
          </div>
          <div className="fi-card fi-doc-sheet">
            {doc === "resume" ? (
              <Resume r={shown.resume} />
            ) : (
              <article className="fi-doc" aria-label="The cover letter">
                {hasLetter ? letter.paragraphs!.map((p, i) => <p key={i}>{p}</p>) : <p className="fi-doc-quiet">{letter.none || "This packet has no letter."}</p>}
              </article>
            )}
          </div>
          {shown.checks.length > 0 && (
            <details className="fi-more-box">
              <summary>Checks: {shown.checks.filter((c) => c.ok).length} of {shown.checks.length} passed</summary>
              <ul className="fi-plain">
                {shown.checks.map((c) => (
                  <li key={c.check}>
                    <b className={c.ok ? "is-ok" : c.blocking ? "is-bad" : "is-note"}>{c.ok ? "Passed" : c.blocking ? "Failed" : "Note"}</b> {c.check}
                    {c.detail && <span className="fi-doc-quiet"> {c.detail}</span>}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {(r.problem || r.headline || r.chosen?.length || r.left_out?.length || r.letter || r.for_owner?.length) && (
            <details className="fi-more-box">
              <summary>Why it says what it says</summary>
              {r.problem && <p><b>The problem it answers:</b> {r.problem}</p>}
              {r.headline && <p><b>The headline:</b> {r.headline}</p>}
              {r.chosen && r.chosen.length > 0 && (
                <>
                  <h5>Chosen</h5>
                  <ul className="fi-plain">{r.chosen.map((c, i) => <li key={i}>{c.why}</li>)}</ul>
                </>
              )}
              {r.left_out && r.left_out.length > 0 && (
                <>
                  <h5>Left out</h5>
                  <ul className="fi-plain">{r.left_out.map((c, i) => <li key={i}>{c.why}</li>)}</ul>
                </>
              )}
              {r.letter && <p><b>The letter:</b> {r.letter}</p>}
              {r.for_owner && r.for_owner.length > 0 && (
                <>
                  <h5>For you to decide</h5>
                  <ul className="fi-plain">{r.for_owner.map((x, i) => <li key={i}>{x}</li>)}</ul>
                </>
              )}
            </details>
          )}
        </>
      )}

      {shown.status !== "writing" && (
        <form
          className="fi-ask"
          onSubmit={(e) => {
            e.preventDefault();
            if (shown.status === "failed") onAsk("");
            else if (feedback.trim()) {
              onAsk(feedback.trim());
              setFeedback("");
            }
          }}
        >
          {shown.status === "failed" ? (
            <button type="submit" className="fi-btn" disabled={busy || blocked} title={offBecause}>Build it again</button>
          ) : (
            <>
              <label htmlFor={feedbackId}>Ask for changes (the writer makes a new version; this one is kept)</label>
              <textarea id={feedbackId} rows={3} value={feedback} onChange={(e) => setFeedback(e.target.value)} />
              <button type="submit" className="fi-btn" disabled={!feedback.trim() || busy || blocked} title={offBecause}>Ask for a new version</button>
            </>
          )}
        </form>
      )}
    </section>
  );
}
