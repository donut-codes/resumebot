<!-- Used by: the job-search app's scout agent in its analyst role, as the shape of one `jobs.analysis_submit` call (handed over in the `jobs.analysis` reply).
     Loaded: by jobs.analysis (backend/app/jobsearch/analysis.py) from the open research's step; named by playbooks/jobsearch/analyse_role.bpmn.
     Placeholders: none. Context keys: none. -->
role: "the role id from jobs.analysis"
title: "The job's title as the posting writes it; only when the task's role carries name_the_role (a job the owner added by its link), with company; leave out otherwise"
company: "The employer's own name as the posting writes it; only with title, never a job board's; leave out otherwise"
target_role: "The id of the one target role this job matches, exactly as what_to_look_for gives it, or none; only when the task carries match_the_role (a job the owner added by its link); leave out otherwise"
employer:                                 # only what you found; leave out what you couldn't
  website: "https://..."
  careers_url: "https://... the page listing all its jobs, on its job system"
  reviews_url: "https://... its page on an employer review site, as a web search result showed it; never opened; leave out when none showed"
  headquarters: "City, ST"
  offices: ["City, ST", "City, ST"]
  work_style: "hybrid"                    # remote, hybrid, onsite, mixed or unknown
  employees: 1200
  founded: 1998
  industry: "Plain words"
  sectors: ["Sector", "Sector"]
  ownership: "private"                    # public, private, pe, vc, nonprofit, government or unknown
  ai_native: false                        # only when one of the owner's asks or gates reads it
  events:                                 # layoffs, acquisitions, funding, leadership changes
    - {kind: "layoff", date: "2025-03", note: "What happened", source: "https://..."}
  sources:                                # where each fact came from, by field
    employees: "https://..."
    founded: "https://..."
pay:
  band: [140000, 170000]                  # posted for this role and place; one posted figure: [200000, 200000]; leave out when none is posted
  band_source: "Where the band was posted"
  market_value: 155000                    # the 50th percentile for this title, level and place; leave out when the posting states a two-figure band
  market_sources: ["Source: figure (sample size)"]
  spread: 0.2                             # only when no band is posted: (H - L) / P to model one
  siblings: []                            # leave empty: the app builds the employer's other bands (the task's sibling_bands)
  note: "Anything the posting or the research says about where hires land."
location: "City, ST"                      # REQUIRED in the first submission when the task carries send_the_place (or when the task's role has no location or names no city): where the posting states the work is (each place it lists); leave out only when the role's place is decided
work_mode: remote                         # remote | hybrid | onsite; REQUIRED with location when the task carries send_the_place or the role has no work mode: read it from the posting; leave out otherwise
place_not_stated: false                   # true only when the task carries send_the_place (or the role's place is undecided) and neither the posting nor the employer's page says where the work is: then send this in place of location and work_mode
required:                                 # with the task's requirement_lines: one item for every numbered required line (line, class, from, why; no item, the app uses the line's words), none skipped, added or answered twice. Without them: every item in the posting's required section
  - {item: "What the posting requires", class: "P", from: ["master-line-id"], why: "How that line shows it"}   # P Proven, T Transferable (cite the bridge), U Unproven (no from)
  - {item: "A main duty, when the posting states no qualifications", class: "T", from: ["master-line-id"], why: "...", from_duties: true}   # only then; never on a stated qualification
  - {line: 1, class: "P", from: ["master-line-id"], why: "How that line shows it"}   # a numbered line answered: P, T, U as below
  - {line: 2, class: "N", why: "Why a resume can't measure it (willingness to travel, work authorisation...)"}   # N: not measurable, a reason, no from; in no count
preferred:                                # with requirement_lines: one item for every numbered preferred line, the same way (line, class, ...). Without them: only what the posting's own words list as preferred, classed the same way; never inferred; leave out when it has none
  - {item: "What the posting prefers", class: "T", from: ["master-line-id"], why: "How that line carries over"}
  - {line: 7, class: "T", from: ["master-line-id"], why: "How that line carries over"}
responsibilities:                         # one item per duty bullet, however many, in the posting's words, lightly cleaned, never merged or invented; skip only boilerplate; classed the same way; leave out the duties sent as required with from_duties
  - {group: "The heading the posting lists it under", item: "A duty the posting lists", class: "P", from: ["master-line-id"], why: "How that line shows it"}   # group: leave out when the duty has no heading
fit_breakdown:                            # leave out to keep the reader's answers; send all of them to re-judge
  asks:
    - {id: "ask-a", answer: "met", evidence: "What in the posting or the record shows it"}
    - {id: "ask-b", answer: "not", evidence: "Why it isn't so"}
    - {id: "ask-c", answer: "unknown", evidence: ""}
    - {id: "ask-d", answer: "met", evidence: "...", count: 2}   # count only for an ask counted for each item
    - {id: "stmt-c", answer: "unknown", evidence: ""}   # a statement of a first-match row: answered like an ask, by its own id; answer every statement of the row, each on its own
    - {id: "stmt-d", answer: "met", evidence: "What in the posting or the record shows it"}   # a statement of an any-of row: answered like an ask, by its own id
notes: "Two or three lines: what the role really is, and what the research turned up."
for_owner: ["Anything the owner should decide: a hard requirement, a doubt about the employer."]
