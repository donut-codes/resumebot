import { api } from "../api/client";
import { useRefreshKey } from "./appContext";
import { useResource } from "./useResource";

/** Whether an agent is there to take a reading, or why not (peak hours, none has checked in, it is paused): asked where a
 * reading can be started (Import your resume, Paste a job posting, Check my roles, Find gaps), and again when the server
 * says it changed (`changed{ready}`), when the stream opens again and on `resync`. An agent busy with other work is there:
 * what is started waits its turn and says so. `enabled: false` holds the question back. */
export function useAgentReady(enabled = true) {
  const refreshKey = useRefreshKey();
  return useResource(() => api.agentReady(), [refreshKey], { liveArea: "ready", enabled });
}
