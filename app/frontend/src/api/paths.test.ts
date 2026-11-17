// Every call `client.ts` makes goes to a route the backend serves. The backend writes its paths down in
// `api-paths.json` (its own test, tests/core/test_api_paths.py, fails when the app's routes and that file differ, and when
// this copy and the backend's differ), so a screen can no longer ask for a route that was renamed or removed while the
// test servers in the screens' tests go on answering it.
import clientSource from "./client.ts?raw";
import servedPaths from "./api-paths.json";

const SERVED = servedPaths as Record<string, string[]>;
const PREFIX = "/api/v1";
const METHODS: Record<string, string> = { get: "GET", post: "POST", put: "PUT", patch: "PATCH", del: "DELETE" };

interface Call {
  method: string;
  path: string;
}

/** Past the generics that follow a helper's name (`get<{ a: B[] }>(`): the index of the `(`, or -1. */
function afterGenerics(source: string, from: number): number {
  let depth = 1;
  let i = from;
  for (; i < source.length && depth > 0; i++) {
    const ch = source[i];
    if (ch === "<") depth++;
    else if (ch === ">" && source[i - 1] !== "=") depth--;
  }
  return source[i] === "(" ? i : -1;
}

/** The string that starts at `source[at]` (a quote or a backtick): the text with each `${...}` made `{}` and a query
 * string (`${qs(...)}`) left off. Null when the first argument is no string. */
function pathAt(source: string, at: number): string | null {
  const quote = source[at];
  if (quote !== '"' && quote !== "'" && quote !== "`") return null;
  let path = "";
  for (let i = at + 1; i < source.length && source[i] !== quote; i++) {
    if (quote === "`" && source[i] === "$" && source[i + 1] === "{") {
      let depth = 1;
      let j = i + 2;
      for (; j < source.length && depth > 0; j++) {
        if (source[j] === "{") depth++;
        else if (source[j] === "}") depth--;
      }
      const expression = source.slice(i + 2, j - 1);
      if (!/\bqs\(|\bqs\b|trailQs/.test(expression)) path += "{}";
      i = j - 1;
    } else {
      path += source[i];
    }
  }
  return path.split("?")[0];
}

/** Every call to a method helper (`get`, `post`, `put`, `patch`, `del`) in `source` whose first argument is a path. */
export function callsIn(source: string): Call[] {
  const calls: Call[] = [];
  for (const m of source.matchAll(/(?<![\w.])(get|post|put|patch|del)</g)) {
    const open = afterGenerics(source, (m.index ?? 0) + m[0].length);
    if (open < 0) continue;
    let at = open + 1;
    while (/\s/.test(source[at])) at++;
    const path = pathAt(source, at);
    if (path !== null) calls.push({ method: METHODS[m[1]], path });
  }
  return calls;
}

/** Whether the backend serves `method` at `path`: a served segment in braces takes any segment. */
function served(call: Call): boolean {
  const asked = call.path.split("/");
  return Object.entries(SERVED).some(([route, methods]) => {
    const segments = route.slice(PREFIX.length).split("/");
    return (
      methods.includes(call.method) &&
      segments.length === asked.length &&
      segments.every((s, i) => /^\{.*\}$/.test(s) || s === asked[i])
    );
  });
}

describe("the calls client.ts makes", () => {
  const calls = callsIn(clientSource);

  it("are found: a changed way of writing them must not leave this test checking nothing", () => {
    expect(calls.length).toBeGreaterThan(60);
    expect(calls).toContainEqual({ method: "POST", path: "/tasks/{}/skip" });
    expect(calls).toContainEqual({ method: "GET", path: "/board" });
    expect(calls).toContainEqual({ method: "PATCH", path: "/missions/numbers" });
  });

  it("all go to a route the backend serves, with the method it serves there", () => {
    const missing = calls.filter((c) => !served(c)).map((c) => `${c.method} ${PREFIX}${c.path}`);
    expect(missing).toEqual([]);
  });

  it("are understood the way the backend's paths are written (a refused call is named, not passed)", () => {
    expect(served({ method: "GET", path: "/board" })).toBe(true);
    expect(served({ method: "DELETE", path: "/board" })).toBe(false);
    expect(served({ method: "GET", path: "/not-a-route" })).toBe(false);
    expect(served({ method: "POST", path: "/tasks/{}/skip" })).toBe(true);
    expect(served({ method: "POST", path: "/tasks/{}/skip/extra" })).toBe(false);
  });
});

describe("reading client.ts", () => {
  it("takes the path of each helper call, with its parameters as {} and its query string off", () => {
    const source = [
      'get<Me>("/me")',
      "post<{ ok: boolean; fn: () => void }>(`/roles/${id}/move`, { to })",
      "get<Board>(`/board${qs({ column, role })}`)",
      "get<X>(\n    `/a/${encodeURIComponent(id)}/b/${kind}`)",
      "function get<T>(path: string): Promise<T> {",
      "obj.get<Y>('/not-this')",
    ].join("\n");
    expect(callsIn(source)).toEqual([
      { method: "GET", path: "/me" },
      { method: "POST", path: "/roles/{}/move" },
      { method: "GET", path: "/board" },
      { method: "GET", path: "/a/{}/b/{}" },
    ]);
  });
});
