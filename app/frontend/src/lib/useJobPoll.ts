import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, errorMessage } from "../api/client";
import type { LiveArea } from "../api/types/pipeline";
import { useLiveChange } from "./useLiveChange";

/** How many answers in a row may fail before the person is told. */
const MAX_MISSES = 3;

/** What every job answers with: where it is, and why it failed. */
export interface JobAnswer {
  state: string;
  error?: string | null;
}

/** The sentences a job tells when it goes wrong, in the words of what it is ("reading", "check"). */
export interface JobWords {
  /** The job ended in failure and the server gave no reason. */
  failed: string;
  /** The server no longer knows the job. */
  lost: string;
  /** The answers stopped coming. */
  touch: string;
}

/** A job started on the server that takes a while (a posting read, a resume read, a check of the roles): once `begin` has
 * the job's id, it is read once, then again each time the server says its `area` changed, when the stream opens again and
 * on `resync`. It stops when the job is done or failed, or the panel closes. The seconds it has been going tick, so a slow
 * job still shows it is working: that is a clock, not a check. `onDone` hears the finished answer; it may return a
 * sentence to refuse it as a failure. */
export function useJobPoll<T extends JobAnswer>(
  load: (id: string) => Promise<T>,
  words: JobWords,
  onDone: ((answer: T) => string | void) | undefined,
  area: LiveArea,
) {
  const [job, setJob] = useState<{ id: string; startedAt: number } | null>(null);
  const [status, setStatus] = useState<T | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const latest = useRef({ load, words, onDone });
  latest.current = { load, words, onDone };
  // Reads the job now; set by the effect below while a job is going.
  const askNow = useRef<() => void>(() => {});

  /** Starts asking after a job just sent to the server. */
  const begin = useCallback((id: string) => {
    const at = Date.now();
    setStatus(null);
    setFailure(null);
    setNow(at);
    setJob({ id, startedAt: at });
  }, []);

  /** Forgets the job, for another try. */
  const reset = useCallback(() => {
    setJob(null);
    setStatus(null);
    setFailure(null);
  }, []);

  useEffect(() => {
    if (!job) return undefined;
    let live = true;
    let ended = false;
    let misses = 0;
    let seq = 0;
    const ask = async () => {
      if (ended) return;
      const mine = ++seq;
      try {
        const next = await latest.current.load(job.id);
        // An answer that lands after a newer read began, or after the panel closed, is dropped.
        if (!live || mine !== seq) return;
        misses = 0;
        setStatus(next);
        if (next.state === "done") {
          ended = true;
          const refused = latest.current.onDone?.(next);
          if (refused) setFailure(refused);
          return;
        }
        if (next.state === "failed") {
          ended = true;
          setFailure(next.error || latest.current.words.failed);
          return;
        }
      } catch (err) {
        if (!live || mine !== seq) return;
        if (err instanceof ApiError && err.status === 404) {
          ended = true;
          setFailure(latest.current.words.lost);
          return;
        }
        misses += 1;
        if (misses >= MAX_MISSES) {
          ended = true;
          setFailure(errorMessage(err, latest.current.words.touch));
          return;
        }
      }
    };
    askNow.current = () => void ask();
    void ask();
    return () => {
      live = false;
      askNow.current = () => {};
    };
  }, [job]);

  // The server's word that the area changed (and the stream coming back, and `resync`) is the cue to read.
  useLiveChange(area, () => askNow.current(), { enabled: job !== null && failure === null && status?.state !== "done" });

  // A clock, not a check: it counts the seconds the job has been going so a slow job shows it is working. It asks the server nothing.
  const ticking = job !== null && failure === null && status?.state !== "done";
  useEffect(() => {
    if (!ticking) return undefined;
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [ticking]);

  return {
    status,
    failure,
    /** Sent and not failed: the panel shows the job going. (A finished job still counts until the caller closes it.) */
    working: job !== null && failure === null,
    /** How long it has been going. */
    seconds: job ? (now - job.startedAt) / 1000 : 0,
    begin,
    reset,
  };
}
