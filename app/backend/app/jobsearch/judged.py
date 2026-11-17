"""Who judged a posting (docs/PRD.md 31.1 and 31.4). Pure: no database, no model, nothing to flush. The filter
(`sift.py`), the retired title check (`titles.py`), the readers' queue (`readers.py`), the owner's "read it anyway"
(`stopped.py`) and the save of What to look for (`profile/store.py`) all read these words, so they live in one place.

The rule: no posting is stopped or read first on the classifier. Every posting that passes the code gates becomes a role
(`pipeline/from_posting.py`); the title check is retired, and the classifier only ranks the postings, the likeliest
first.
The words of the title check's stops stay, because the postings it stopped are kept with their reason."""

#: Where a posting that passed the gates goes: `ROLES`, to become a role at once (`pipeline/from_posting.py`; the title
#: pass is retired), `READER` for a reader (the owner asked). `TITLES` is the retired title check's: postings it had
#: left waiting are taken as `ROLES` are.
ROLES, TITLES, READER = "roles", "titles", "reader"
#: The step that judged a posting last (`JobPosting.judged_by`): a gate, the title check (a tab said no, or yes), or the
#: owner (they asked for a posting to be read anyway). Until 2026-10-13 the classifier was a step too (it trimmed the
#: very unlikely); a closed posting it stopped still says so, and no open one does.
GATE, TITLE_CHECK, YOU = "gate", "titles", "you"

#: What the title check's stop says: the words a stop is shown with on the Gated stage.
NOT_YOUR_KIND = "not your kind of job"
