import { useRef, type ReactNode } from "react";
import type { PrepAnswer, PrepCite, PrepReport, PrepReview, PrepSource } from "../../api/types/pipeline";
import { dateTimeInWords } from "../../lib/dates";
import { formatDollars } from "../../lib/format";
import { safeHref } from "../../lib/links";

const ROUND_WORDS: Record<string, string> = {
  recruiter_screen: "Recruiter screen",
  hiring_manager: "Hiring manager",
  panel: "Panel",
  executive: "Executive",
};
const roundWords = (r: string) => ROUND_WORDS[r] ?? r;

const SECTIONS = [
  ["quick", "Quick Sheet"],
  ["gaps", "Open gaps"],
  ["need", "Core need"],
  ["good", "What good looks like"],
  ["stage", "Stage read"],
  ["staples", "Staples"],
  ["questions", "Questions and stories"],
  ["problem", "Problem to solve"],
  ["ask", "Questions to ask"],
  ["reference", "Reference"],
  ["review", "Red-team read"],
] as const;

/** A fact's source: its page, and the day it was checked. */
function Source({ s }: { s: PrepSource }) {
  const href = safeHref(s.url);
  return (
    <span className="fi-rep-source">
      {href ? (
        <a href={href} target="_blank" rel="noreferrer">{s.title || s.url}</a>
      ) : (
        <span>{s.title || s.url}</span>
      )}{" "}
      <span className="fi-rep-quiet">checked {s.checked_on}</span>
    </span>
  );
}

/** The master lines or stories a claim rests on, small and quiet under it. */
function Cites({ cites }: { cites?: PrepCite[] }) {
  if (!cites || cites.length === 0) return null;
  return (
    <p className="fi-rep-cites">
      From your {cites.length === 1 ? "line" : "lines"}: {cites.map((c) => c.text).join(" · ")}
    </p>
  );
}

function Answer({ a }: { a: PrepAnswer }) {
  return (
    <div className="fi-rep-answer">
      <p>
        <b>{a.kind === "story" ? "Story" : "Judgement"}{a.title ? `: ${a.title}` : ""}</b>
      </p>
      <p>{a.text}</p>
      {a.result && <p><b>Result</b> {a.result}</p>}
      <Cites cites={a.cites} />
    </div>
  );
}

function Labelled({ label, children }: { label: string; children: ReactNode }) {
  return (
    <p className="fi-rep-line">
      <b>{label}</b> <span>{children}</span>
    </p>
  );
}

interface Props {
  report: PrepReport;
  review: PrepReview | null;
}

/** The Coach's report, one column: the Quick Sheet first and open, the rest in the report's order, each in its own fold,
 * with an index that opens the one it names. */
export default function InterviewReport({ report, review }: Props) {
  const root = useRef<HTMLElement>(null);
  const q = report.quick_sheet;
  const pay = report.staples.pay;
  const jump = (id: string) => {
    const el = root.current?.querySelector<HTMLDetailsElement>(`details[data-section="${id}"]`);
    if (!el) return;
    el.open = true;
    el.scrollIntoView?.({ block: "start", behavior: "smooth" });
  };
  const sections = SECTIONS.filter(([id]) => (id === "problem" ? !!report.problem : id === "review" ? !!review : true));
  const fold = (id: string, children: ReactNode) => {
    const title = SECTIONS.find(([key]) => key === id)![1];
    return (
      <details className="fi-rep-sec" data-section={id} open={id === "quick" ? true : undefined}>
        <summary><h4>{title}</h4></summary>
        <div className="fi-rep-body">{children}</div>
      </details>
    );
  };
  const rounds = ["recruiter_screen", "hiring_manager", "panel", "executive"].filter((r) => report.ask.some((a) => a.round === r));

  return (
    <section className="fi-rep" aria-label="Interview prep report" ref={root}>
      <nav className="fi-rep-index" aria-label="On this page">
        <span className="fi-rep-quiet">On this page</span>
        {sections.map(([id, title]) => (
          <button key={id} type="button" className="fi-chip" onClick={() => jump(id)}>{title}</button>
        ))}
      </nav>

      {fold("quick", (
        <>
          <Labelled label="When">{dateTimeInWords(q.when_at)}, {q.round_label}</Labelled>
          {q.people.length > 0 && <Labelled label="Who">{q.people.map((p) => (p.title ? `${p.name}, ${p.title}` : p.name)).join("; ")}</Labelled>}
          <Labelled label="Core need">{q.core_need}</Labelled>
          <Labelled label="Opener">{q.opener}</Labelled>
          {q.pay.expectation && <Labelled label="Pay">{q.pay.expectation}</Labelled>}
          {q.pay.market && (
            <Labelled label="Market range">
              {formatDollars(q.pay.market.low)} to {formatDollars(q.pay.market.high)} ({q.pay.market.basis})
            </Labelled>
          )}
          {q.first_choice.length > 0 && (
            <>
              <h5>Questions to ask, first choice</h5>
              <ul className="fi-plain">
                {q.first_choice.map((f, i) => <li key={i}>{f.question} <span className="fi-rep-quiet">({roundWords(f.round)})</span></li>)}
              </ul>
            </>
          )}
          {q.story_map.length > 0 && (
            <>
              <h5>Which story answers which question</h5>
              <ul className="fi-plain">
                {q.story_map.map((m, i) => <li key={i}>{m.question} <span className="fi-rep-quiet">{"→"} {m.story}</span></li>)}
              </ul>
            </>
          )}
        </>
      ))}

      {fold("gaps", (
        report.open_gaps.length === 0 ? (
          <p>Nothing you still need to do before the interview.</p>
        ) : (
          <ul className="fi-plain">
            {report.open_gaps.map((g, i) => (
              <li key={i}>{g.text} <b>Do:</b> {g.action}</li>
            ))}
          </ul>
        )
      ))}

      {fold("need", (
        <>
          <p>{report.core_need.text}</p>
          {report.core_need.from_title_only && <p className="fi-rep-quiet">From the title only: the posting&rsquo;s text was missing.</p>}
        </>
      ))}

      {fold("good", (
        <ul className="fi-plain">
          {report.good_looks_like.map((g, i) => (
            <li key={i}>{g.text}{g.source && <> <Source s={g.source} /></>}</li>
          ))}
        </ul>
      ))}

      {fold("stage", (
        <>
          <p>{report.stage_read.screens_for}</p>
          {report.stage_read.interviewers.map((p, i) => (
            <div className="fi-rep-card" role="group" aria-label={`Interviewer: ${p.name}`} key={i}>
              <p><b>{p.name}</b></p>
              <p>{p.found && p.background ? p.background : "Nothing found on public pages."}</p>
              {p.screens_for && <Labelled label="Screens for">{p.screens_for}</Labelled>}
              {p.connection && (
                <>
                  <Labelled label="Your connection">{p.connection.text}</Labelled>
                  <Cites cites={p.connection.cites} />
                </>
              )}
              <Labelled label="Don't">{p.dont}</Labelled>
              {p.questions && p.questions.length > 0 && (
                <>
                  <p className="fi-rep-quiet">Likely to ask</p>
                  <ul className="fi-plain">{p.questions.map((x, j) => <li key={j}>{x}</li>)}</ul>
                </>
              )}
            </div>
          ))}
        </>
      ))}

      {fold("staples", (
        <>
          <h5>Opener</h5>
          {report.staples.opener.map((c, i) => (
            <div key={i}>
              <p>{c.text}</p>
              <Cites cites={c.cites} />
            </div>
          ))}
          <h5>Why here</h5>
          {report.staples.why_here.map((c, i) => (
            <div key={i}>
              <p>{c.text}{c.source && <> <Source s={c.source} /></>}</p>
              <Cites cites={c.cites} />
            </div>
          ))}
          <h5>Why leaving</h5>
          {report.staples.why_leaving.ask_owner ? (
            <p className="fi-rep-ask">You write this: {report.staples.why_leaving.ask_owner}</p>
          ) : (
            <div>
              <p>{report.staples.why_leaving.text}</p>
              <Cites cites={report.staples.why_leaving.cites} />
            </div>
          )}
          <h5>Pay</h5>
          {pay.expectation && <p>{pay.expectation}</p>}
          {pay.assumptions && <p className="fi-rep-quiet">{pay.assumptions}</p>}
          {pay.market && (
            <p>
              Market range {formatDollars(pay.market.low)} to {formatDollars(pay.market.high)} ({pay.market.basis}).{" "}
              {pay.market.source && <Source s={pay.market.source} />}
            </p>
          )}
        </>
      ))}

      {fold("questions", (
        report.questions.map((x, i) => (
          <div className="fi-rep-card" key={i}>
            <p><b>{x.question}</b>{x.asked_by && <span className="fi-rep-quiet"> (likely from {x.asked_by})</span>}</p>
            {x.method && <Labelled label="What they are really asking">{x.method}</Labelled>}
            {x.answer ? <Answer a={x.answer} /> : <p className="fi-rep-ask">No story yet: {x.gap}</p>}
          </div>
        ))
      ))}

      {report.problem && fold("problem", (
        <>
          <p className="fi-rep-quiet">{report.problem.kind === "approach" ? "An approach to the work" : "How you would run your first engagement"}</p>
          <p>{report.problem.approach}</p>
          {report.problem.discovery_questions.length > 0 && (
            <>
              <h5>What you would need answered</h5>
              <ul className="fi-plain">{report.problem.discovery_questions.map((d, i) => <li key={i}>{d}</li>)}</ul>
            </>
          )}
        </>
      ))}

      {fold("ask", (
        rounds.map((r) => (
          <div key={r}>
            <h5>{roundWords(r)}</h5>
            {report.ask
              .filter((a) => a.round === r)
              .sort((a, b) => a.priority - b.priority)
              .map((a, i) => (
                <div className="fi-rep-card" key={i}>
                  <p><b>{a.question}</b> <span className="fi-rep-quiet">{a.priority === 1 ? "First choice" : "Second choice"}</span></p>
                  <Labelled label="What it tells you">{a.diagnoses}</Labelled>
                  <Labelled label="A good answer">{a.good_answer}</Labelled>
                  <Labelled label="A bad answer">{a.bad_answer}</Labelled>
                  <Labelled label="The follow-up">{a.follow_up}</Labelled>
                </div>
              ))}
          </div>
        ))
      ))}

      {fold("reference", (
        <ul className="fi-plain">
          {report.reference.map((r, i) => <li key={i}>{r.text} <Source s={r.source} /></li>)}
        </ul>
      ))}

      {review && fold("review", (
        <>
          <p>{review.summary}</p>
          {review.findings.map((f, i) => (
            <div className="fi-rep-card" key={i}>
              <p><b>{f.where}</b></p>
              <Labelled label="The problem">{f.problem}</Labelled>
              <Labelled label="The fix">{f.fix}</Labelled>
            </div>
          ))}
        </>
      ))}
    </section>
  );
}
