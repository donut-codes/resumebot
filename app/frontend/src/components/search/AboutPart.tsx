// About (the screens rebuild spec, screen 3; the owner, 2026-10-24: kept, and rewritten to explain how the search
// works). The same for every user of the product: it names the steps and the settings, never anyone's values. It follows
// the code as built (backend app/jobsearch: boards_night, sift, gating, role_from_posting, pipeline, rank, analysis,
// scoring, favorites, apply_go); when a step changes there, this page changes with it.

import type { ReactNode } from "react";
import { routeFor, serializeRoute, type ProfileTab } from "../../lib/route";
import { PaneHead, Sheet } from "../focus/parts";

const part = (p: ProfileTab, words: string) => <a href={serializeRoute(routeFor("profile", { profilePart: p }))}>{words}</a>;
const missions = (words: string) => <a href={serializeRoute(routeFor("missions"))}>{words}</a>;

/** One step of the search: what happens, and the settings on this screen it reads. */
function Step({ n, title, children, reads }: { n: number; title: string; children: ReactNode; reads?: ReactNode }) {
  return (
    <Sheet
      title={
        <>
          <span className="fi-ss-step" aria-hidden="true">
            {n}
          </span>
          {title}
        </>
      }
    >
      <div className="fi-ss-about">
        {children}
        {reads && <p className="fi-ss-reads">{reads}</p>}
      </div>
    </Sheet>
  );
}

export default function AboutPart() {
  return (
    <section className="fi-pane" aria-label="About">
      <div className="fi-pane-in">
        <PaneHead title="How the search works" meta={["From a posting on a job board to a role ready for you to apply to"]} />
        <p className="fi-body">
          resumebot never applies or fills in a form for you. It finds roles, judges them against what you set
          here, and puts the best in front of you. Nothing is thrown away: every posting is kept, and one that is stopped
          says which rule stopped it.
        </p>

        <Step n={1} title="Where roles come from" reads={<>Set on {part("where", "Where to look")}: the sites, the job systems, and watching every employer with a role in play.</>}>
          <p>
            Every night the app reads, in its own code, not by an agent, the public job listings of the employers you
            watch, through the job systems they post on (an applicant tracking system and the like), and the job boards and sites
            you switched on. It reads politely: it honours each site&rsquo;s rules for robots, asks about once a second, and
            stops when it is refused. A site a server cannot read can be read in your own browser instead.
          </p>
          <p>
            A link you paste on the Pipeline joins the same way: it waits in Added links until its page is read. Every
            posting goes into one log with the day it was first and last seen, and one its board no longer lists is closed.
            A site you avoid is never read, and a posting that links to one is stopped.
          </p>
        </Step>

        <Step n={2} title="The hard passes" reads={<>Set on {part("gates", "Hard pass")}: for every role, or for one target role on its own.</>}>
          <p>
            Before any agent sees a posting, code checks it against your hard passes: the employers on your hard pass list,
            the sites you avoid, where the work is and how it is done, the pay where the posting states it, how old the
            posting is, and the facts on record about the employer. A posting that fails one is stopped, with that rule as
            its reason. What a posting does not say never stops it: an unknown passes.
          </p>
          <p>
            Each part of a posting has an On switch: off, nothing in it stops anything. Saving Hard pass checks every posting and role again at once and says what moved; a role you
            applied to, or moved by hand, is never moved. Every posting that passes becomes a role on the Pipeline.
          </p>
        </Step>

        <Step n={3} title="Your target roles" reads={<>Set on {part("criteria", "What to look for")}: each target role&rsquo;s name and example titles.</>}>
          <p>
            A role is not judged on its title alone. Its title is read together with the rest of the posting in the first
            look, and a role with only a title and a few lines is not ranked until its description has been read. Your
            target roles&rsquo; example titles tell the first look what you are after.
          </p>
          <p>
            Check my roles, on each target role, checks, as a dry run, which of the roles you pursued,
            or of one posting you paste, your target roles would catch, and which fit none of them. It changes nothing.
          </p>
        </Step>

        <Step n={4} title="First look and rank" reads={<>Reads {part("criteria", "What to look for")} (the kind of employer and each target role) and {part("gates", "Hard pass")}.</>}>
          <p>
            Each role&rsquo;s posting is cleaned (the boilerplate taken out), and the hard passes on the facts it states are
            checked again. Then a small, fast agent reads every cleaned posting against a short profile
            made from your settings: the kind of employer you want, each target role with its titles and its words, and
            every hard pass that is on. It scores each role from 0 to 100, with two lines of why. A posting that clearly
            breaks a hard pass scores 0. The rank only orders roles: it never closes one.
          </p>
          <p>These roles are First Impression on the Pipeline.</p>
        </Step>

        <Step n={5} title="Line-by-line read" reads={<>Reads {part("scoring", "Prioritization")}, the class rules on {part("gates", "Hard pass")}, and your master resume on My Resume.</>}>
          <p>
            A larger agent then reads the best-ranked roles in full, line by line, from the top of First
            Impression. It rates each required and preferred item Proven, Transferable or Unproven against your master
            resume, answers each ask on Prioritization and each class rule, works out the pay, fills in what it
            learns about the employer, and says which of your target roles the role fits, so that role&rsquo;s own hard
            passes and asks apply.
          </p>
          <p>
            Code then adds up the fit: the points the role earned as a percent of the most it could earn. There is no partial
            credit. A met ask earns all its points; one that is not met, or that cannot be told, earns nothing. With
            &ldquo;Let the app grade the requirements&rdquo; on, the app scores each rated item by your numbers itself. The
            hard passes that needed this research are checked again, and a role that fails one is closed.
          </p>
        </Step>

        <Step n={6} title="The queue and Ready to Launch" reads={<>The match weight is the first item of {part("scoring", "Prioritization")}. How many roles each stage holds: {missions("Roles in Second Impression")} and {missions("Roles in Ready to Launch")}, on Agent Settings (blank: 15 each).</>}>
          <p>
            First Impression, Second Impression and Ready to Launch are one list, and every role in it has one number. A role
            not ranked yet is 0. Once the first look has ranked it, its number is that rank as written. Once the line-by-line read has
            read it, its number is that read&rsquo;s score, which weighs &ldquo;Can I do it&rdquo; (the share of its graded
            requirements that are Proven) against &ldquo;Does it fit&rdquo; (the share of your other asks&rsquo; points it
            earned), by the match weight; a newer read replaces the rank even when it is lower. A role in its first hours
            gains points and a posting that is old loses some: the number on a card already has both in it, and the list is
            sorted by that number.
          </p>
          <p>
            Ready to Launch is the top read roles in that list, as many as you set. Second Impression is the next read
            roles, as many as you set: they are on deck. First Impression is every other role in the list, the roles not
            read yet and the read roles below the rest. A role near the top that is not read yet waits for its read, and
            the line-by-line read takes the best ranked first. A role at an employer you applied to within the days you set takes no place.
            Roles you moved there yourself, starred or opened with Launch stay where they are.
          </p>
          <p>
            Roles still being got ready (Followup needed, Landing Zone and Cleansed Data) have no number yet. The Pipeline
            keeps them apart, as Work in progress, from the ranked list.
          </p>
          <p>
            No role sits in Ready to Launch until its Second Impression has read and graded its posting. A role with no
            read, or a read that graded nothing, leaves for the stage it belongs in and comes back once it is read if it
            still ranks. If you move a role there before it is read, it is read first, as a star is, and then it takes its
            place. Go opens only roles that are read.
          </p>
          <p>
            When a role comes into Ready to Launch, whichever way it got there, the app checks at once that its posting is
            still open and compares the employer&rsquo;s own page with the posting it holds. The result shows on the role. A
            posting that is gone leaves Ready to Launch with its reason; a page the app cannot read is looked at in your
            browser.
          </p>
          <p>
            Nothing is written for a role in Ready to Launch until you press Go (or Build now on one role). Go checks that each
            posting is still open, builds its resume and cover letter, has them read again, and opens each posting in a tab
            for you to apply. You apply; the app never does.
          </p>
        </Step>

        <Step n={7} title="Favorites">
          <p>
            Star any role on the Pipeline. A starred role goes to Ready to Launch at once and keeps its place while it is
            starred, counted among the day&rsquo;s roles. If the line-by-line read has not read it yet, it goes first in line, and its resume
            and cover letter are built as soon as it is read. Hard passes still apply to it. Unstar it and it competes on its
            score again.
          </p>
        </Step>

        <Sheet title="Where each setting takes effect">
          <dl className="fi-ss-where">
            <div>
              <dt>{part("where", "Where to look")}</dt>
              <dd>What the nightly read covers, from the next read. Adding or removing a site to avoid checks every posting again at once.</dd>
            </div>
            <div>
              <dt>{part("criteria", "What to look for")}</dt>
              <dd>
                What the first look ranks against, and which target role the read matches a role to. Saving it changes what later reads
                look for; it judges nothing again by itself.
              </dd>
            </div>
            <div>
              <dt>{part("gates", "Hard pass")}</dt>
              <dd>
                Code&rsquo;s checks, then the first look, and the class rules the read answers. Saving it checks
                every posting and role again at once. How the ask is worked out sets each role&rsquo;s estimated ask, which the
                lowest pay can be compared with.
              </dd>
            </div>
            <div>
              <dt>{part("scoring", "Prioritization")}</dt>
              <dd>
                The asks the read answers and the fit code adds up. Saving it works every score out again at once, without a new read;
                the match weight puts the queue in a new order.
              </dd>
            </div>
          </dl>
        </Sheet>
      </div>
    </section>
  );
}
