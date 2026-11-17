import { blankRoute, ME_PAGES, PROFILE_TABS,parseRoute, routeFor, serializeRoute } from "./route";

describe("parseRoute", () => {
  it("empty and unknown addresses are the board, the front page; the roles keep their address", () => {
    expect(parseRoute("")).toEqual(blankRoute("board"));
    expect(parseRoute("#")).toEqual(blankRoute("board"));
    expect(parseRoute("#/nonsense/here")).toEqual(blankRoute("board"));
    expect(parseRoute("#/board")).toEqual(blankRoute("board"));
    expect(blankRoute()).toEqual(blankRoute("board"));
    // The screens that no longer exist land on the front page too.
    expect(parseRoute("#/home")).toEqual(blankRoute("board"));
    expect(parseRoute("#/goals/g1")).toEqual(blankRoute("board"));
  });

  it("an address that no longer exists is an unknown one: the front page, or the first part of its page", () => {
    // #/dashboard, #/pipeline, #/tasks and #/where were pages of their own once; the rest were parts of pages that stay.
    for (const hash of ["#/dashboard", "#/pipeline", "#/pipeline?role=builders", "#/tasks", "#/tasks?x=1", "#/where"]) expect(parseRoute(hash)).toEqual(blankRoute("board"));
    expect(parseRoute("#/profile/settings")).toEqual(blankRoute("profile"));
    expect(parseRoute("#/profile?role=builders")).toEqual(blankRoute("profile"));
    expect(parseRoute("#/inbox/list")).toEqual(blankRoute("inbox"));
    expect(parseRoute("#/me/about")).toEqual(blankRoute("me"));
    expect(parseRoute("#/me/writing")).toEqual(blankRoute("me"));
  });

  it("the profile's parts have addresses that round-trip; Where to look is the first, so is an unknown part", () => {
    const gates = routeFor("profile", { profilePart: "gates" });
    // Opening Profile shows the first tab, Where to look.
    expect(blankRoute("profile").profilePart).toBe("where");
    expect(parseRoute("#/profile")).toEqual(blankRoute("profile"));
    expect(parseRoute("#/profile").profilePart).toBe("where");
    expect(parseRoute("#/profile/gates")).toEqual(gates);
    expect(parseRoute("#/profile/nonsense").profilePart).toBe("where");
    expect(serializeRoute(gates)).toBe("#/profile/gates");
    expect(serializeRoute(blankRoute("profile"))).toBe("#/profile");
    // What to look for is a part like the rest: it has its own address now.
    expect(serializeRoute(routeFor("profile", { profilePart: "criteria" }))).toBe("#/profile/criteria");
    expect(parseRoute("#/profile/criteria")).toEqual(routeFor("profile", { profilePart: "criteria" }));
    for (const part of PROFILE_TABS) {
      const r = routeFor("profile", { profilePart: part });
      expect(parseRoute(serializeRoute(r))).toEqual(r);
    }
  });

  it("What to look for opens on a target role's tab by its address, and only that part does", () => {
    const builders = routeFor("profile", { profilePart: "criteria", profileRole: "builders" });
    expect(serializeRoute(builders)).toBe("#/profile/criteria?role=builders");
    expect(parseRoute("#/profile/criteria?role=builders")).toEqual(builders);
    expect(serializeRoute(routeFor("profile", { profilePart: "criteria", profileRole: "a b/c" }))).toBe("#/profile/criteria?role=a%20b%2Fc");
    expect(parseRoute("#/profile/criteria?role=a%20b%2Fc").profileRole).toBe("a b/c");
    expect(parseRoute("#/profile").profileRole).toBeNull();
    expect(parseRoute("#/profile/criteria").profileRole).toBeNull();
    // With no part named, #/profile is Where to look, whatever role the address carries.
    expect(parseRoute("#/profile?role=")).toEqual(blankRoute("profile"));
    expect(parseRoute("#/profile?role=").profilePart).toBe("where");
    expect(parseRoute("#/profile?role=builders")).toEqual(blankRoute("profile"));
    // A named part is the part, whatever else is in the address.
    expect(parseRoute("#/profile/nonsense?role=builders")).toEqual(blankRoute("profile"));
    // Another part has no role to open: the address does not carry one, and reading one off leaves it out.
    expect(parseRoute("#/profile/gates?role=builders")).toEqual(routeFor("profile", { profilePart: "gates" }));
    expect(parseRoute("#/profile/where?role=builders")).toEqual(blankRoute("profile"));
    expect(serializeRoute(routeFor("profile", { profilePart: "gates", profileRole: "builders" }))).toBe("#/profile/gates");
    expect(serializeRoute(routeFor("profile", { profileRole: "builders" }))).toBe("#/profile");
    // The Pipeline's narrowing is its own: the same word, a different page.
    expect(parseRoute("#/board?role=builders").profileRole).toBeNull();
    expect(parseRoute("#/board?role=builders").targetRole).toBe("builders");
  });

  it("the Settings tab is gone: no Profile address makes it", () => {
    // In the order the tabs are shown: Where to look, What to look for, Hard pass, Prioritization, Also sent to agents, About (the ids are the documents').
    expect(PROFILE_TABS).toEqual(["where", "criteria", "gates", "scoring", "also", "about"]);
    expect(parseRoute("#/profile/about")).toEqual(routeFor("profile", { profilePart: "about" }));
    expect(parseRoute("#/profile/scoring").profilePart).toBe("scoring");
  });

  it("a mission opens beside the list with its own address", () => {
    expect(parseRoute("#/missions")).toEqual(blankRoute("missions"));
    const r = parseRoute("#/missions/m1");
    expect(r).toEqual(routeFor("missions", { missionId: "m1" }));
    expect(serializeRoute(r)).toBe("#/missions/m1");
  });

  it("an employer opens beside the list with its own address that round-trips", () => {
    expect(parseRoute("#/employers")).toEqual(blankRoute("employers"));
    const r = parseRoute("#/employers/e%201");
    expect(r).toEqual(routeFor("employers", { employerId: "e 1" }));
    expect(serializeRoute(r)).toBe("#/employers/e%201");
    expect(serializeRoute(routeFor("board", { employerId: "e1" }))).toBe("#/board");
  });

  it("where to look is the Profile's first tab: #/profile is it", () => {
    expect(serializeRoute(routeFor("profile", { profilePart: "where" }))).toBe("#/profile");
    expect(parseRoute("#/profile")).toEqual(routeFor("profile", { profilePart: "where" }));
    expect(parseRoute("#/profile/where")).toEqual(routeFor("profile", { profilePart: "where" }));
  });

  it("a role on the board has its own address that round-trips", () => {
    const role = routeFor("board", { roleId: "r 1" });
    expect(parseRoute("#/board/r%201")).toEqual(role);
    expect(serializeRoute(role)).toBe("#/board/r%201");
    expect(parseRoute("#/board").roleId).toBeNull();
  });

  it("the board can be narrowed to one target role: #/board?role=<id>, with or without a role open", () => {
    const narrowed = routeFor("board", { targetRole: "ai governance" });
    expect(parseRoute("#/board?role=ai%20governance")).toEqual(narrowed);
    expect(serializeRoute(narrowed)).toBe("#/board?role=ai%20governance");

    const open = routeFor("board", { roleId: "r1", targetRole: "builders" });
    expect(serializeRoute(open)).toBe("#/board/r1?role=builders");
    expect(parseRoute("#/board/r1?role=builders")).toEqual(open);

    // Nothing narrowed: no query, and an empty or missing one is every role.
    expect(parseRoute("#/board?role=").targetRole).toBeNull();
    expect(parseRoute("#/board").targetRole).toBeNull();
    expect(serializeRoute(routeFor("board", { roleId: "r1" }))).toBe("#/board/r1");
    // Only the board has one: another tab drops it.
    expect(serializeRoute(routeFor("inbox", { targetRole: "builders" }))).toBe("#/inbox");
    expect(parseRoute("#/inbox?role=builders").targetRole).toBeNull();
  });

  it("the board can open on a stage: #/board?role=<id>&stage=<stage>, with or without a role; a word that is no stage is none", () => {
    const gated = routeFor("board", { targetRole: "builders", boardStage: "gated_out" });
    expect(serializeRoute(gated)).toBe("#/board?role=builders&stage=gated_out");
    expect(parseRoute("#/board?role=builders&stage=gated_out")).toEqual(gated);
    const applied = routeFor("board", { boardStage: "applied" });
    expect(serializeRoute(applied)).toBe("#/board?stage=applied");
    expect(parseRoute("#/board?stage=applied")).toEqual(applied);
    expect(parseRoute("#/board/r1?role=builders&stage=second_impression")).toEqual(
      routeFor("board", { roleId: "r1", targetRole: "builders", boardStage: "second_impression" }),
    );
    // "ended" is a stage too (the three ended statuses); a word of before the statuses were renamed ("found"), a made-up word, an empty one and none are every stage as it was left.
    expect(parseRoute("#/board?stage=ended").boardStage).toBe("ended");
    for (const bad of ["#/board?stage=found", "#/board?stage=nonsense", "#/board?stage=", "#/board"]) expect(parseRoute(bad).boardStage).toBeNull();
    // Only the board has one.
    expect(serializeRoute(routeFor("inbox", { boardStage: "applied" }))).toBe("#/inbox");
    expect(parseRoute("#/inbox?stage=applied").boardStage).toBeNull();
  });

  it("the Me page's parts have addresses that round-trip; the resume is the first, Find Gaps the second, then the three pages of writing rules", () => {
    expect(ME_PAGES).toEqual(["resume", "find-gaps", "story-library", "resume-rules", "letter-rules", "document-rules"]);
    expect(parseRoute("#/me")).toEqual(blankRoute("me"));
    expect(parseRoute("#/me/nonsense").mePart).toBe("resume");
    expect(serializeRoute(blankRoute("me"))).toBe("#/me");
    for (const page of ["resume-rules", "letter-rules", "document-rules"] as const) {
      const route = routeFor("me", { mePart: page });
      expect(parseRoute(`#/me/${page}`)).toEqual(route);
      expect(serializeRoute(route)).toBe(`#/me/${page}`);
    }
  });

  it("Import your resume opens on a reading named in the address, #/me?import=<job id>, on the master resume only", () => {
    const route = routeFor("me", { importJob: "job 1" });
    expect(serializeRoute(route)).toBe("#/me?import=job%201");
    expect(parseRoute("#/me?import=job%201")).toEqual(route);
    expect(parseRoute("#/me").importJob).toBeNull();
    expect(parseRoute("#/me?import=").importJob).toBeNull();
    expect(parseRoute("#/me/resume-rules?import=x").importJob).toBeNull();
    expect(parseRoute("#/board?import=x").importJob).toBeNull();
    expect(serializeRoute(routeFor("me", { mePart: "resume-rules", importJob: "x" }))).toBe("#/me/resume-rules");
  });

  it("Notifications keeps the address #/inbox", () => {
    expect(parseRoute("#/inbox")).toEqual(blankRoute("inbox"));
  });

  it("the old addresses keep working under the new names: #/me, #/profile, #/inbox, #/agents, #/missions", () => {
    expect(parseRoute("#/me").tab).toBe("me");
    expect(parseRoute("#/profile").tab).toBe("profile");
    expect(parseRoute("#/inbox").tab).toBe("inbox");
    expect(parseRoute("#/agents").tab).toBe("agents");
    expect(parseRoute("#/missions").tab).toBe("missions");
  });

  it("the front page is the Pipeline: an empty address and an unknown one open it", () => {
    for (const hash of ["", "#", "#/", "#/nonsense", "#/nonsense/deeper"]) expect(parseRoute(hash)).toEqual(blankRoute("board"));
    expect(blankRoute().tab).toBe("board");
  });

  it("Notifications' parts have addresses that round-trip; an unknown part is the first", () => {
    const activity = routeFor("inbox", { inboxSection: "activity" });
    expect(parseRoute("#/inbox/activity")).toEqual(activity);
    expect(serializeRoute(activity)).toBe("#/inbox/activity");
    const decisions = routeFor("inbox", { inboxSection: "decisions" });
    const notes = routeFor("inbox", { inboxSection: "notifications" });
    expect(parseRoute("#/inbox/decisions")).toEqual(decisions);
    expect(parseRoute("#/inbox/notifications")).toEqual(notes);
    expect(parseRoute("#/inbox/needs")).toEqual(blankRoute("inbox"));
    expect(parseRoute("#/inbox/nonsense").inboxSection).toBe("needs");
    expect(serializeRoute(decisions)).toBe("#/inbox/decisions");
    expect(serializeRoute(notes)).toBe("#/inbox/notifications");
    expect(serializeRoute(blankRoute("inbox"))).toBe("#/inbox");
    expect(parseRoute(serializeRoute(decisions))).toEqual(decisions);
    // a part means nothing on another tab
    expect(parseRoute("#/employers/decisions").inboxSection).toBe("needs");
    expect(serializeRoute(routeFor("agents", { inboxSection: "decisions" }))).toBe("#/agents");
  });

  it("agents and their tab; an unknown tab is dropped", () => {
    expect(parseRoute("#/agents")).toEqual(blankRoute("agents"));
    expect(parseRoute("#/agents/c1")).toEqual(routeFor("agents", { agentId: "c1" }));
    expect(parseRoute("#/agents/c1/settings")).toEqual(routeFor("agents", { agentId: "c1", agentTab: "settings" }));
    expect(parseRoute("#/agents/c1/activity")).toEqual(routeFor("agents", { agentId: "c1", agentTab: "activity" }));
    expect(parseRoute("#/agents/c1/skills").agentTab).toBeNull();
    expect(parseRoute("#/agents/c1/nope").agentTab).toBeNull();
  });

  it("How they work, alone or opened on what was sent for one search", () => {
    expect(parseRoute("#/agents/how")).toEqual(routeFor("agents", { agentsHow: true }));
    expect(parseRoute("#/agents/how?sent=t1")).toEqual(routeFor("agents", { agentsHow: true, sentTaskId: "t1" }));
    // "sent" means nothing anywhere else.
    expect(parseRoute("#/agents/c1?sent=t1").sentTaskId).toBeNull();
    expect(serializeRoute(routeFor("agents", { agentsHow: true, sentTaskId: "t 1" }))).toBe("#/agents/how?sent=t%201");
  });

  it("How they work, opened on a running process", () => {
    const live = routeFor("agents", { agentsHow: true, liveRunId: "r1" });
    expect(parseRoute("#/agents/how?live=r1")).toEqual(live);
    expect(serializeRoute(live)).toBe("#/agents/how?live=r1");
    // "live" means nothing anywhere else.
    expect(parseRoute("#/agents/c1?live=r1").liveRunId).toBeNull();
    expect(parseRoute("#/board?live=r1").liveRunId).toBeNull();
  });

  it("How they work's process tabs have addresses that round-trip", () => {
    const sub = routeFor("agents", { agentsHow: true, howPath: ["jobsearch_round", "jobsearch_screen_role"] });
    expect(parseRoute("#/agents/how/jobsearch_round/jobsearch_screen_role")).toEqual(sub);
    expect(serializeRoute(sub)).toBe("#/agents/how/jobsearch_round/jobsearch_screen_role");
    expect(parseRoute("#/agents/how/jobsearch_round").howPath).toEqual(["jobsearch_round"]);
    expect(parseRoute("#/agents/how").howPath).toEqual([]);
    expect(parseRoute(serializeRoute(sub))).toEqual(sub);
    // with one search's payload, and a path that isn't How they work is ignored
    const sent = routeFor("agents", { agentsHow: true, howPath: ["jobsearch_round"], sentTaskId: "t1" });
    expect(serializeRoute(sent)).toBe("#/agents/how/jobsearch_round?sent=t1");
    expect(parseRoute("#/agents/how/jobsearch_round?sent=t1")).toEqual(sent);
    expect(parseRoute("#/agents/c1/settings").howPath).toEqual([]);
  });

  it("survives a bad escape", () => {
    expect(parseRoute("#/agents/%E0%A4%A").agentId).toBeNull();
  });
});

describe("serializeRoute", () => {
  it("writes each tab's front page", () => {
    expect(serializeRoute(blankRoute("board"))).toBe("#/board");
    expect(serializeRoute(blankRoute("inbox"))).toBe("#/inbox");
    expect(serializeRoute(blankRoute("missions"))).toBe("#/missions");
    expect(serializeRoute(blankRoute("agents"))).toBe("#/agents");
  });

  it("escapes ids", () => {
    expect(serializeRoute(routeFor("agents", { agentId: "a/b c" }))).toBe("#/agents/a%2Fb%20c");
  });

  it("round-trips every shape", () => {
    const routes = [
      blankRoute("board"),
      routeFor("board", { targetRole: "builders" }),
      routeFor("board", { roleId: "r 1", targetRole: "a/b c" }),
      routeFor("board", { targetRole: "builders", boardStage: "gated_out" }),
      routeFor("board", { boardStage: "ended" }),
      blankRoute("profile"),
      routeFor("profile", { profilePart: "criteria" }),
      routeFor("profile", { profilePart: "criteria", profileRole: "a/b c" }),
      routeFor("profile", { profilePart: "scoring" }),
      blankRoute("inbox"),
      routeFor("inbox", { inboxSection: "activity" }),
      blankRoute("missions"),
      routeFor("missions", { missionId: "m 1" }),
      blankRoute("agents"),
      routeFor("agents", { agentId: "c1", agentTab: "settings" }),
      routeFor("agents", { agentId: "c1" }),
      routeFor("agents", { agentsHow: true }),
      routeFor("agents", { agentsHow: true, sentTaskId: "t1" }),
      routeFor("agents", { agentsHow: true, liveRunId: "r1" }),
      routeFor("agents", { agentsHow: true, howPath: ["a", "b c"], liveRunId: "r1" }),
    ];
    for (const r of routes) expect(parseRoute(serializeRoute(r))).toEqual(r);
  });
});
