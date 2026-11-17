import { api, ApiError } from "./client";
import { stubFetch } from "../test-utils";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("api client error handling", () => {
  it("surfaces the backend's detail message on a 4xx", async () => {
    stubFetch(() => Promise.resolve(jsonResponse({ detail: "A search is already running." }, 409)));
    await expect(api.me()).rejects.toMatchObject({ message: "A search is already running.", status: 409 });
  });

  it("joins FastAPI's validation-error array into one message", async () => {
    stubFetch(() =>
      Promise.resolve(jsonResponse({ detail: [{ msg: "field required" }, { msg: "value error" }] }, 422)));
    await expect(api.inbox()).rejects.toThrow("field required; value error");
  });

  it("gives a readable message when the network request itself fails", async () => {
    stubFetch(() => Promise.reject(new Error("offline")));
    let error: unknown;
    try {
      await api.inbox();
    } catch (e) {
      error = e;
    }
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toMatch(/can't reach the server/i);
    expect((error as ApiError).status).toBe(0);
  });

  it("falls back to a status-based message when the body isn't JSON", async () => {
    stubFetch(() => Promise.resolve(new Response("not json", { status: 404 })));
    await expect(api.inbox()).rejects.toMatchObject({ message: "Not found.", status: 404 });
  });

  it("returns parsed JSON on success", async () => {
    stubFetch(() =>
      Promise.resolve(jsonResponse({ version: "1.0.0" })),
    );
    const me = await api.me();
    expect(me.version).toBe("1.0.0");
  });
});

describe("the calls the screens make", () => {
  function record() {
    const seen: { url: string; method: string; body: unknown }[] = [];
    stubFetch((url: string, init?: RequestInit) => {
      seen.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined });
      return Promise.resolve(jsonResponse({}));
    });
    return seen;
  }

  it("What to look for asks whether an agent is free, reads a posting for a role and follows the reading", async () => {
    const seen = record();
    await api.agentReady();
    await api.parseExample("A posting.", "lead");
    await api.parseExample("Another.");
    await api.parseJob("job 1");
    expect(seen.map((s) => [s.method, s.url])).toEqual([
      ["GET", "/api/v1/agent/ready"],
      ["POST", "/api/v1/profile/parse"],
      ["POST", "/api/v1/profile/parse"],
      ["GET", "/api/v1/profile/parse/job%201"],
    ]);
    expect(seen[1].body).toEqual({ text: "A posting.", role_id: "lead" });
    expect(seen[2].body).toEqual({ text: "Another." }); // a role the server doesn't know yet is left out
  });

  it("a save of What to look for judges nothing again, and the page has no client call to judge again or to match roles again", () => {
    expect("rejudge" in api).toBe(false);
    expect("criteriaImpact" in api).toBe(false);
    expect("matchRoles" in api).toBe(false);
  });

  it("the Inbox badge reads /inbox", async () => {
    const seen = record();
    await api.inbox();
    expect(seen[0].url).toBe("/api/v1/inbox");
  });

  it("the agents are read at /agents, and Pause is a PATCH", async () => {
    const seen = record();
    await api.listAgents();
    await api.getAgent("a1");
    await api.editAgent("a1", { paused: true });
    expect(seen[0].url).toBe("/api/v1/agents");
    expect(seen[1].url).toBe("/api/v1/agents/a1");
    expect(seen[2]).toMatchObject({ url: "/api/v1/agents/a1", method: "PATCH", body: { paused: true } });
  });

  it("the trail passes its filter, cursor and limit, and leaves out what is not set", async () => {
    const seen = record();
    await api.agentTrail("a1", { filter: "needs_look", before: "2026-10-01T00:00:00Z", limit: 20 });
    await api.agentTrail("a1");
    expect(seen[0].url).toBe("/api/v1/agents/a1/trail?filter=needs_look&before=2026-10-01T00%3A00%3A00Z&limit=20");
    expect(seen[1].url).toBe("/api/v1/agents/a1/trail");
  });

  it("How they work reads the overview, a step's example, what was sent and a run", async () => {
    const seen = record();
    await api.harness();
    await api.harnessExample("find roles");
    await api.harnessTask("t1");
    await api.processRun("i1");
    expect(seen.map((s) => s.url)).toEqual([
      "/api/v1/harness",
      "/api/v1/harness/steps/find%20roles/example",
      "/api/v1/harness/tasks/t1",
      "/api/v1/harness/runs/i1",
    ]);
  });
});
