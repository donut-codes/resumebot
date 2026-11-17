import type { Ask } from "../../api/types/profile";
import { DECIDER_WORDS, EMPLOYER_START, factName } from "./profileSpec";

/** An employer fact in plain words, for the line that says the company record answered the ask. */
const FACT_WORDS: Record<string, string> = {
  employees: "headcount",
  years_since_founded: "year founded",
  ownership: "ownership",
  ai_native: "AI-native",
  industry: "industry",
  local: "local",
  work_style: "work style",
  years_since_layoff: "layoffs",
  layoffs_in_2_years: "layoffs in the last 2 years",
};

/** Whether the app decides an ask from the employer's record: it holds a fact to test (and says it is the app's, or is a
 * reader's ask the app answers once the record has the fact). */
export const byEmployerRecord = (ask: Ask): boolean => ask.fact !== null && ask.fact !== undefined;

/** Whether this is an ask the owner can choose who decides for: a plain one. A pay tier and a posting's age are edit-only, an
 * ask decided from the requirement classes keeps working as it is stored (the page makes no new one: grading the requirements
 * is its own setting), and an ask counted for each item and the built-in ask are not in a row of their own choosing. */
export const canChooseWhoDecides = (ask: Ask): boolean =>
  !ask.per_item && !ask.derived && !ask.from_classes && (ask.amount === null || ask.amount === undefined) && (ask.days === null || ask.days === undefined);

/** Who answers an ask or a statement, in one quiet line under its sentence:
 * "Answered by: Claude, from the job posting" with a text button "Answer from the company record instead", which makes it an
 * ask the app decides from the employer's record (a fact, a comparison and a number, the same three boxes a rule about the
 * employer has on Hard pass, shown in the ask's own line), and, once it is, "Answered by: the company record (fact)" alone: the
 * server refuses to save a line the reader judges (store.refuse_reader_lines), so there is no way back to the job posting here.
 * Changing who decides changes
 * how the ask behaves, so it is the owner's edit, made on the page with their own number: nothing converts an ask by itself, and
 * the number starts blank, which decides nothing until it is typed. A draft until the page's one Save. */
export default function AskDecidedBy({ ask, name, onChange }: { ask: Ask; name: string; onChange: (next: Ask) => void }) {
  if (!canChooseWhoDecides(ask)) return null;
  const byRecord = byEmployerRecord(ask);
  return (
    <p className="fi-ss-decider">
      {byRecord ? `${DECIDER_WORDS.record} (${FACT_WORDS[ask.fact ?? ""] ?? factName(ask.fact ?? "").toLowerCase()})` : DECIDER_WORDS.claude}{" "}
      {!byRecord && (
        <button type="button" className="fi-link" aria-label={`${DECIDER_WORDS.toApp}: ${name}`} onClick={() => onChange({ ...ask, checked_by: "app", ...EMPLOYER_START })}>
          {DECIDER_WORDS.toApp}
        </button>
      )}
    </p>
  );
}
