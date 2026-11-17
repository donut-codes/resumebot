"""The classifier (docs/PRD.md 29.5 and 29.6): a small, explainable model that learns, from one owner's own roles, what
this owner moves forward, and gives a posting the probability that it is that kind of role. It is multinomial naive
Bayes over word unigrams and bigrams of the title, the department and the first words of the text, in plain Python
(no dependencies). It is trained in memory when the night's filter runs and kept for as long as what it is made from
(the owner's roles and the postings it learns them against) stays the same; nothing about it is stored.

What it learns from (the owner's roles and no one else's):
  - positives, weight 2: a role the owner moved forward (Needs Drafted, Ready to Apply, Applied, Active Discussion, or
    ended after it had reached Applied, which a role the employer turned down did even when no history says so, as an
    imported one has none: docs/PRD.md 31.7);
  - positives, weight 2: a posting the filter stopped and the owner asked to read anyway (docs/PRD.md 30.2);
  - negatives, weight 1: a role the owner passed on (Ended, "I passed") and a role the title check stopped as not the
    owner's kind of job. These are what the owner or the title check said about the kind of job. A score teaches
    nothing: it ranks and never stops (docs/PRD.md 39.10), and it is worked out after research, from the answers and
    the employer's facts (39.20), so a role that merely has a high or a low one has said nothing about the owner's kind
    of job. A role a place, pay, employer or age gate stopped says nothing about
    the kind of job, so it teaches nothing (`kind_of_job_stop`); nor does one whose posting came down;
  - background: a sample of the postings the app has read that never became roles, weighted so the two sides count
    equally, without the ones a gate other than the kind of job stopped (they would teach "wrong place", not "wrong
    kind of job"). An owner's roles are mostly already-chosen (a tracker keeps what passed), so their own negatives are
    few; learning them against the run of postings is what lets the model tell "like this owner's roles" from "a
    posting".
With fewer positives than `Settings.classifier_min_examples` there is no model (the cold start): `train` gives None
and nothing is rated.

It judges nothing and stops nothing: it ranks (docs/PRD.md 31.1). The probability is the model's lean toward this
owner's history, tempered so that a long text doesn't push it to 0 or 1."""

import hashlib
import math
import re
import string
import struct
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import judged, off_turn, stages
from app.jobsearch.models import JobPosting, Opportunity, StageMove
from app.jobsearch.postings.coop import Cooperate
from app.jobsearch.profile import store as profile_store
from app.jobsearch.states import StopGate

#: How much of the text the model reads (the title and department are read whole).
WORDS_READ = 200
#: A word counts at most this many times in one document (the title is said twice, so its words weigh double).
MAX_TF = 3
#: Weights of the kinds of example.
MOVED_FORWARD_WEIGHT, NEGATIVE_WEIGHT = 2.0, 1.0
#: The most postings sampled as background (the same sample each time: by a hash of the posting's id).
BACKGROUND_MAX = 4000
#: Laplace smoothing.
ALPHA = 1.0
#: The log-odds are clipped here so a probability never reads exactly 0 or 1.
MAX_LOG_ODDS = 12.0

#: Stages a role has to be in (or to have reached before it ended) to count as moved forward.
FORWARD = (stages.READY_TO_LAUNCH, stages.APPLIED, stages.ACTIVE_DISCUSSION)
#: An ended role counts as moved forward when its history shows it reached one of these.
REACHED = (stages.APPLIED, stages.ACTIVE_DISCUSSION)


def reached(owner: str) -> Select:
    """The ids of the owner's roles whose history shows they reached Applied or Active Discussion: a stage move into
    either, or out of either. The move out matters for a role the tracker import brought in already Applied, which has
    no move into it: when a rule or the owner ends it, the move out is the only history that says it got that far. The
    one definition of "reached"; the classifier, the fits-none check, the role tab's numbers and the title pass's
    examples all read it."""
    return select(StageMove.role_id).where(
        StageMove.owner == owner, or_(StageMove.to_status.in_(REACHED), StageMove.from_status.in_(REACHED)))


async def reached_set(session: AsyncSession, owner: str) -> set:
    """`reached`, as a set of role ids."""
    return set(await session.scalars(reached(owner)))

#: The gates whose stop is about the kind of job, not the place, pay, employer or age (docs/PRD.md 31.7): the title
#: pass's "not your kind of job". The role says which gate stopped it (`Opportunity.screened_out_by`); the sentence is
#: only what the owner reads.
KIND_OF_JOB_GATES = (StopGate.KIND,)


def kind_of_job_stop(gate: str | None) -> bool:
    """Whether the gate that stopped a role is about the kind of job. A stop for the place, pay, an employer or a site,
    the posting's age, or the classifier's own low rating is not: no lesson about the kind of job comes from it."""
    return gate in KIND_OF_JOB_GATES


STOP_WORDS = frozenset("""
a about above after again all also am an and any are as at be because been before being below between both but by can
could did do does doing down during each few for from further had has have having he her here hers him his how i if in
into is it its itself just me more most my no nor not now of off on once only or other our out over own same she should
so some such than that the their them then there these they this those through to too under until up us very was we
were what when where which while who whom why will with would you your yours
""".split())

#: Where a run of words ends: a run of marks, or a dash with a space each side. A word is a letter, then letters,
#: digits, `+` and `#`, and at most one `'` or `&` piece.
_BREAK = r"[.!?;:,\n\r|/()\[\]{}–—]+|\s-\s"
_WORD = r"[a-z][a-z0-9+#]*(?:['&][a-z0-9]+)?"
#: A word (group 1) or a break (group 1 empty), in the order they stand in the text; anything else is skipped.
_TOKEN = re.compile(f"({_WORD})|{_BREAK}")
#: What a word may not be: a stop word, or one character (the only one-character word is a letter).
_DROPPED = STOP_WORDS | frozenset(string.ascii_lowercase)


def text_of(title: str | None, department: str | None, body: str | None) -> str:
    """One document for the model: the title (said twice), the department, the first `WORDS_READ` words of the text."""
    t = (title or "").strip()
    first = " ".join((body or "").split(None, WORDS_READ)[:WORDS_READ])  # the words past the last one read stay unsplit
    return ". ".join(part for part in (t, t, (department or "").strip(), first) if part)


def features(text: str) -> Counter:
    """The words and word pairs of a text, lowercased, stop words dropped (a pair never spans a comma, a stop or a
    sentence), each counted up to `MAX_TF` times. One pass over the text: the words and the breaks come out of one
    search, in the order they stand, and a run of words ends at a break or at a dropped word. The features come out in
    the order they first appear (each run's words, then its pairs), so a sum over them is the same sum every time."""
    found: list[str] = []
    run: list[str] = []
    tokens = _TOKEN.findall((text or "").lower())
    tokens.append("")  # ends the last run
    for word in tokens:
        if word and word not in _DROPPED:
            run.append(word)
        elif run:
            found += run
            if len(run) > 1:
                found += [f"{a} {b}" for a, b in zip(run, run[1:], strict=False)]
            run = []
    counts = Counter(found)
    for feature, n in counts.items():
        if n > MAX_TF:
            counts[feature] = MAX_TF
    return counts


@dataclass
class Model:
    """What was learned: per feature, the weighted counts in the positive and the negative class."""

    positive: Counter = field(default_factory=Counter)
    negative: Counter = field(default_factory=Counter)
    positive_total: float = 0.0
    negative_total: float = 0.0
    positive_weight: float = 0.0
    negative_weight: float = 0.0
    positives: int = 0
    negatives: int = 0
    vocabulary: int = 0  # how many features in all, counted once by build (each prediction needs it, per feature)
    #: How much each feature the model knows says "positive": worked out the first time the model rates a text, kept
    #: after that, and forgotten when the model learns something (`_learn`). Not part of what was learned.
    _log_ratios: dict[str, float] | None = field(default=None, init=False, repr=False, compare=False)

    def _vocabulary(self) -> int:
        return self.vocabulary or len(set(self.positive) | set(self.negative)) or 1

    def _ratios(self) -> dict[str, float]:
        """How much each feature says "positive": the log of its smoothed rate in the positive class over its rate in
        the negative class. A feature the model never saw is not in it. Worked out once for the model (it is the same
        for every text), where each text used to work out each of its features' again."""
        ratios = self._log_ratios
        if ratios is None:
            v = self._vocabulary()
            positive_all, negative_all = self.positive_total + ALPHA * v, self.negative_total + ALPHA * v
            positive, negative = self.positive, self.negative
            ratios = {feature: math.log(((positive.get(feature, 0.0) + ALPHA) / positive_all)
                                        / ((negative.get(feature, 0.0) + ALPHA) / negative_all))
                      for feature in positive.keys() | negative.keys()}
            self._log_ratios = ratios
        return ratios

    def _evidence(self, text: str) -> list[tuple[str, float]]:
        """Each known feature of the text with how much it adds to the log-odds (its rate, times its count)."""
        ratios = self._ratios()
        out = []
        for feature, count in features(text).items():
            llr = ratios.get(feature)
            if llr is not None:
                out.append((feature, llr * count))
        return out

    def _probability(self, evidence: list[tuple[str, float]]) -> float:
        prior = math.log(max(self.positive_weight, 1e-9) / max(self.negative_weight, 1e-9))
        tempered = sum(e for _, e in evidence) / math.sqrt(len(evidence)) if evidence else 0.0
        odds = max(-MAX_LOG_ODDS, min(MAX_LOG_ODDS, prior + tempered))
        return 1 / (1 + math.exp(-odds))

    @staticmethod
    def _reasons(evidence: list[tuple[str, float]], top: int) -> list[str]:
        picked: list[str] = []
        used: set[str] = set()
        for feature, contribution in sorted(evidence, key=lambda fe: (-fe[1], fe[0])):
            if contribution <= 0 or len(picked) >= top:
                break
            words = set(feature.split())
            if words & used:
                continue
            picked.append(feature)
            used |= words
        return picked

    def predict(self, text: str) -> float:
        """The probability, 0 to 1, that a posting is like the owner's moved-forward roles. The features' evidence is
        divided by the root of how many there are, so a long text counts as a little more than a short one, not as
        much more as its length."""
        return self._probability(self._evidence(text))

    def why(self, text: str, top: int = 3) -> list[str]:
        """The words or phrases that push the probability up most, strongest first, none inside another's words."""
        return self._reasons(self._evidence(text), top)

    def rate(self, text: str, top: int = 3) -> tuple[float, list[str]]:
        """`predict` and `why` together, from one reading of the text (the filter wants both for every posting)."""
        evidence = self._evidence(text)
        return self._probability(evidence), self._reasons(evidence, top)

    def summary(self) -> dict:
        """A line for a log: how many examples, how many words."""
        return {"positives": self.positives, "negatives": self.negatives, "features": self._vocabulary()}


def _learn(model: Model, text: str, positive: bool, weight: float) -> None:
    model._log_ratios = None  # it knows something new: each feature's log-ratio is worked out again when next used
    counts = features(text)
    if positive:
        model.positive.update({f: n * weight for f, n in counts.items()})
        model.positive_total += weight * sum(counts.values())
        model.positive_weight += weight
        model.positives += 1
    else:
        model.negative.update({f: n * weight for f, n in counts.items()})
        model.negative_total += weight * sum(counts.values())
        model.negative_weight += weight
        model.negatives += 1


def build(examples: list[tuple[str, bool, float]]) -> Model:
    """A model from (text, is_positive, weight) examples."""
    model = Model()
    for text, positive, weight in examples:
        _learn(model, text, positive, weight)
    model.vocabulary = len(set(model.positive) | set(model.negative)) or 1
    return model


async def build_giving_the_loop_back(examples: list[tuple[str, bool, float]]) -> Model:
    """`build`, with the event loop given back between examples (`Cooperate`): reading every text is seconds of it, and
    no other task, a transaction's next statement among them, runs meanwhile."""
    model = Model()
    coop = Cooperate()
    for text, positive, weight in examples:
        await coop()
        _learn(model, text, positive, weight)
    model.vocabulary = len(set(model.positive) | set(model.negative)) or 1
    return model


def posting_text(posting: JobPosting) -> str:
    return text_of(posting.title, posting.department, posting.text)


#: What a labelled role stands for: the owner pursued it, or the owner or a reader passed on it as the kind of job.
PURSUED, PASSED_ON = "pursued", "passed_on"


@dataclass(frozen=True, slots=True)
class RoleFacts:
    """What `lesson` reads of a role, as `examples` reads it for each of the owner's roles: four columns, a tenth
    of the cost of a role row."""

    id: uuid.UUID
    status: str
    screened_out_by: str | None
    ended_reason: str | None


def lesson(role: Opportunity | RoleFacts, reached: set) -> tuple[str, bool, float] | None:
    """(what the role stands for, is_positive, weight) for a role the model learns from, or None when it says nothing
    about the owner's kind of job: a role a place, pay, employer or age gate stopped, one the owner has not moved or
    ended, one whose posting went (docs/PRD.md 31.7). A fit, high or low, says nothing (39.10)."""
    if role.status == stages.GATED_OUT:
        return (PASSED_ON, False, NEGATIVE_WEIGHT) if kind_of_job_stop(role.screened_out_by) else None
    ended = role.status in stages.ENDED
    # An employer saying no is the end of an application: the owner pursued it, whatever history was kept (a role
    # imported from the tracker has none).
    if role.status in FORWARD or (ended and (role.id in reached or role.ended_reason == stages.THEY_SAID_NO)):
        return PURSUED, True, MOVED_FORWARD_WEIGHT
    if ended and role.ended_reason in stages.PASSED_ON_REASONS:
        return PASSED_ON, False, NEGATIVE_WEIGHT
    return None


def label(role: Opportunity | RoleFacts, reached: set) -> tuple[bool, float] | None:
    """(is_positive, weight) for a role the model learns from, or None when the role says nothing yet (`lesson`)."""
    learned = lesson(role, reached)
    return learned[1:] if learned else None


#: How many roles one statement of `examples` asks for by id (a statement has a parameter for each), and how many it
#: reads at a time of the owner's roles (a page of them is a few milliseconds of the loop).
ROLES_AT_ONCE, ROLES_PER_PAGE = 500, 5000


async def examples(session: AsyncSession, owner: str) -> list[tuple[str, bool, float]]:
    """The owner's roles as labelled examples (see the module's note).

    Which roles the model learns from is asked of four columns of every role (`lesson`, the one definition); the title,
    the description and the first posting (the one first seen, then by id) are read only for those. The text of a role's
    posting is the one thing the model reads that is large, and most roles teach it nothing."""
    coop = Cooperate()
    gone_far = await reached_set(session, owner)
    labelled = []
    after = None
    while True:  # a page of the owner's roles at a time, the loop given back between them
        query = select(Opportunity.id, Opportunity.status, Opportunity.screened_out_by, Opportunity.ended_reason).where(
            Opportunity.owner == owner).order_by(Opportunity.id).limit(ROLES_PER_PAGE)
        page = (await session.execute(query if after is None else query.where(Opportunity.id > after))).all()
        for row in page:
            await coop()
            role = RoleFacts(*row)
            marked = label(role, gone_far)
            if marked is not None:
                labelled.append((role.id, marked))
        if len(page) < ROLES_PER_PAGE:
            break
        after = page[-1].id
    named: dict[uuid.UUID, tuple[str, str | None]] = {}
    first: dict[uuid.UUID, tuple[str | None, str | None]] = {}
    for at in range(0, len(labelled), ROLES_AT_ONCE):
        ids = [role_id for role_id, _ in labelled[at:at + ROLES_AT_ONCE]]
        for role_id, title, description in await session.execute(select(
                Opportunity.id, Opportunity.title, Opportunity.description).where(Opportunity.id.in_(ids))):
            named[role_id] = (title, description)
        # The first posting of each role: the one first seen, then by id (a window, as `distinct on` is Postgres's).
        ranked = select(
            JobPosting.opportunity_id, JobPosting.department, JobPosting.text,
            func.row_number().over(partition_by=JobPosting.opportunity_id,
                                   order_by=(JobPosting.first_seen, JobPosting.id)).label("place"),
        ).where(JobPosting.owner == owner, JobPosting.opportunity_id.in_(ids)).subquery()
        for role_id, department, text in await session.execute(
                select(ranked.c.opportunity_id, ranked.c.department, ranked.c.text).where(ranked.c.place == 1)):
            if role_id is not None:
                first[role_id] = (department, text)
        await coop()
    out = []
    for role_id, marked in labelled:  # each text splits a whole body: the loop is given back between them
        await coop()
        if role_id not in named:
            continue  # gone since the roles were read
        title, description = named[role_id]
        department, text = first.get(role_id, (None, None))
        out.append((text_of(title, department, description or text), *marked))
    # A posting the filter stopped and the owner asked to read anyway (docs/PRD.md 30.2): their correction, counted as
    # a role they moved forward.
    for posting in await session.scalars(select(JobPosting).where(
            JobPosting.owner == owner, JobPosting.owner_override.is_(True))):
        await coop()
        out.append((posting_text(posting), True, MOVED_FORWARD_WEIGHT))
    return out


async def background(session: AsyncSession, owner: str, positive_weight: float, negative_weight: float
                     ) -> list[tuple[str, bool, float]]:
    """Postings that never became roles, as negatives whose weights together make the negative side as heavy as the
    positive one (none when the owner's own negatives already outweigh it). No posting counts for more than a role the
    owner turned down: with few postings read, the ones about to be judged must not outweigh the owner's history."""
    room = positive_weight - negative_weight
    if room <= 0:
        return []
    coop = Cooperate()
    # The sample is the ones whose id's md5 sorts first: the same ones each time, whatever else changes. SQLite has no
    # md5, so the ids are read (small) and ordered here, and only the chosen postings' texts are read.
    ids = await off_turn.scalars_in_slices(
        session, select(JobPosting.id).where(
            JobPosting.owner == owner, JobPosting.opportunity_id.is_(None), JobPosting.owner_override.is_(False),
            # Not the ones a gate stopped for the place, pay, an employer or the age: they say nothing of the kind.
            or_(JobPosting.filtered_by.is_(None), JobPosting.judged_by == judged.TITLE_CHECK)))
    keyed = []
    for posting_id in ids:
        await coop()
        keyed.append((hashlib.md5(str(posting_id).encode()).hexdigest(), posting_id))
    chosen = [posting_id for _, posting_id in sorted(keyed)[:BACKGROUND_MAX]]
    read: dict[uuid.UUID, tuple[str | None, str | None, str | None]] = {}
    for at in range(0, len(chosen), ROLES_AT_ONCE):
        for posting_id, title, department, text in await session.execute(
                select(JobPosting.id, JobPosting.title, JobPosting.department, JobPosting.text)
                .where(JobPosting.id.in_(chosen[at:at + ROLES_AT_ONCE]))):
            read[posting_id] = (title, department, text)
    rows = [read[posting_id] for posting_id in chosen if posting_id in read]  # one gone since the ids were read: out
    if not rows:
        return []
    each = min(NEGATIVE_WEIGHT, room / len(rows))
    out = []
    for title, department, text in rows:  # up to `BACKGROUND_MAX` texts, each split whole: the loop is given back
        await coop()
        out.append((text_of(title, department, text), False, each))
    return out


#: owner -> (the roles' state when it was trained, what it was made from (`_read_by_the_model`), the model or None)
_CACHE: dict[str, tuple[tuple, bytes, Model | None]] = {}


def clear_cache() -> None:
    _CACHE.clear()


async def _state(session: AsyncSession, owner: str, minimum: int) -> tuple:
    count, newest = (await session.execute(select(func.count(Opportunity.id), func.max(Opportunity.analysed_at))
                                           .where(Opportunity.owner == owner))).one()
    # `count(*)`, not `count(id)`: the same number (the id is the primary key, never null), and the index by owner and
    # time answers it without a visit to the table's rows.
    moved = (await session.execute(select(func.count(), func.max(StageMove.at)).where(StageMove.owner == owner))).one()
    postings = await session.scalar(select(func.count(JobPosting.id)).where(JobPosting.owner == owner))
    overrides = await session.scalar(select(func.count(JobPosting.id)).where(
        JobPosting.owner == owner, JobPosting.owner_override.is_(True)))
    # Why roles ended counts too: telling the app why one ended changes what it learns (docs/PRD.md 31.7).
    ended = await session.scalar(select(func.count(Opportunity.ended_reason)).where(Opportunity.owner == owner))
    passed = await session.scalar(select(func.count(Opportunity.id)).where(
        Opportunity.owner == owner, Opportunity.ended_reason.in_(stages.PASSED_ON_REASONS)))
    return count, newest, *moved, postings, overrides, ended, passed, minimum


def _read_by_the_model(learned: list[tuple[str, bool, float]], minimum: int) -> bytes:
    """What the model is made from, as a short digest: every example in order (its text, whether it is a positive, its
    weight) and the fewest positives it needs. Two makings from the same digest give the same model, to the last digit,
    so the one made already is the one a new making would give."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(struct.pack("<qq", minimum, len(learned)))
    for text, positive, weight in learned:
        words = text.encode("utf-8", "surrogatepass")
        digest.update(struct.pack("<?dq", positive, weight, len(words)))
        digest.update(words)
    return digest.digest()


async def train(session: AsyncSession, owner: str) -> Model | None:
    """The owner's model, or None when they have fewer positive examples than `classifier_min_examples`. Kept in memory
    while what it is made from stays the same.

    The state of the roles and postings (`_state`) is the cheap look: while it is as it was, nothing is read. When it
    has moved the examples are read again, and the model is made again only if they are not the ones it was made from.
    The state moves for many things the model does not read (a role the owner has not moved or ended, a posting the
    filter stopped), and each used to cost a making: reading many texts to learn the same words."""
    settings = (await profile_store.current(session, owner)).settings
    minimum = settings.classifier_min_examples
    state = await _state(session, owner, minimum)
    cached = _CACHE.get(owner)
    if cached and cached[0] == state:
        return cached[2]
    labelled = await examples(session, owner)
    pos = sum(w for _t, positive, w in labelled if positive)
    neg = sum(w for _t, positive, w in labelled if not positive)
    learned = [*labelled, *await background(session, owner, pos, neg)]
    read = _read_by_the_model(learned, minimum)
    if cached and cached[1] == read:
        model = cached[2]
    else:
        model = await build_giving_the_loop_back(learned)
        if model.positives < minimum or not model.negatives:
            model = None
    _CACHE[owner] = (state, read, model)
    return model


async def role_probabilities(session: AsyncSession, owner: str, role_ids: list[uuid.UUID]) -> dict[uuid.UUID, float]:
    """The classifier's probability for each role's posting, by role id: the likeliest of its postings when it has more
    than one. A role whose posting the classifier never rated is left out, as is one with no posting (an unknown ask
    then counts 0.5, and a list of roles puts it after the rated ones)."""
    if not role_ids:
        return {}
    rows = (await session.execute(
        select(JobPosting.opportunity_id, func.max(JobPosting.fit_prob)).where(
            JobPosting.owner == owner, JobPosting.opportunity_id.in_(role_ids), JobPosting.fit_prob.is_not(None))
        .group_by(JobPosting.opportunity_id))).all()
    return {role_id: prob for role_id, prob in rows if role_id is not None and prob is not None}
