import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { errorMessage } from "../api/client";
import type { LiveArea } from "../api/types/pipeline";
import { useLiveChange } from "./useLiveChange";

interface ResourceOptions<T> {
  /** The words to show when the load fails and the server sent none. Left out, a failure is not shown (the
   * screen keeps what it last had) and only `onError` hears of it. */
  fallback?: string;
  /** Load again when the server says this area changed, when the live stream opens again after a drop and on `resync`.
   * No timer: the stream tells. */
  liveArea?: LiveArea;
  /** With `liveArea`: a `changed` that names ids counts only if it names this one. */
  liveId?: string;
  /** Runs after each load that is still current, with `live()` to ask, later, whether it still is. */
  onData?: (data: T, live: () => boolean) => void;
  onError?: (err: unknown) => void;
  /** `false` holds the load back (nothing to fetch yet); it runs when it turns `true`. */
  enabled?: boolean;
  /** When `deps` change, clear what is on screen (and any error) until the new answer lands, instead of keeping
   * the old one: for a page or a file that is a different thing, not a refresh of the same one. */
  reset?: boolean;
}

interface Resource<T> {
  /** `null` until the first load lands. */
  data: T | null;
  /** The words for the last failed load; cleared by the next load that works. */
  error: string | null;
  /** Load again now, keeping what is on screen until the answer comes. */
  reload: () => void;
  setData: Dispatch<SetStateAction<T | null>>;
  setError: Dispatch<SetStateAction<string | null>>;
}

/** Load something for a screen: once when it opens and again whenever `deps` change, optionally reading again when the
 * server says an area changed (`liveArea`). An answer that arrives after a newer load started, after `deps` changed
 * or after the screen closed is dropped. `load` is read fresh each time, so an inline closure is fine; list in `deps`
 * what should make it load again. This is the one home of the `let cancelled = false` fetch effect and of
 * the `reload()` + `useEffect(reload)` + live-refresh trio. */
export function useResource<T>(load: () => Promise<T>, deps: readonly unknown[], options: ResourceOptions<T> = {}): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  const optionsRef = useRef(options);
  optionsRef.current = options;
  // Which load is the latest; bumping it makes every answer still on its way stale.
  const latest = useRef(0);

  const reload = useCallback(() => {
    const mine = ++latest.current;
    const live = () => latest.current === mine;
    let pending: Promise<T>;
    try {
      pending = Promise.resolve(loadRef.current());
    } catch (err) {
      pending = Promise.reject(err);
    }
    pending.then(
      (result) => {
        if (!live()) return;
        setError(null);
        setData(result);
        optionsRef.current.onData?.(result, live);
      },
      (err) => {
        if (!live()) return;
        const { fallback, onError } = optionsRef.current;
        if (fallback !== undefined) setError(errorMessage(err, fallback));
        onError?.(err);
      },
    );
  }, []);

  const enabled = options.enabled !== false;
  useEffect(() => {
    if (!enabled) return undefined;
    if (optionsRef.current.reset) {
      setData(null);
      setError(null);
    }
    reload();
    return () => {
      latest.current++;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);

  useLiveChange(options.liveArea, reload, { id: options.liveId, enabled });

  return { data, error, reload, setData, setError };
}
