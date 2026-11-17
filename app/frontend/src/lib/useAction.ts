import { useCallback, useRef, useState } from "react";
import { ApiError, errorMessage } from "../api/client";

interface ActionOptions<A extends unknown[]> {
  /** The words to show when the call fails and the server sent none; a function gets the arguments of the
   * run, for a screen whose one runner serves several buttons. */
  fallback?: string | ((...args: A) => string);
  /** Hears the words too, for a screen that shows them somewhere else (a toast) or does more with the failure. */
  onError?: (message: string, err: unknown) => void;
}

interface Action<A extends unknown[], R> {
  /** Runs `fn`; resolves to what it returned, or `undefined` when it failed (the failure is in `error`). */
  run: (...args: A) => Promise<R | undefined>;
  busy: boolean;
  /** The words for the last failure; cleared when the next run starts. */
  error: string | null;
  /** Whether the last failure was the server's 409: what the screen holds is out of date. */
  conflict: boolean;
  /** For a check the screen makes itself before running (a name left empty), so it shows in the same place. */
  setError: (message: string | null) => void;
}

/** A button's work: marks the screen busy while `fn` runs, and turns a failure into words instead of an
 * unhandled rejection. This is the one home of the `setBusy(true)` / `try` / `catch` / `finally` block. Put what
 * should happen only on success at the end of `fn`. */
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>, options: ActionOptions<A> = {}): Action<A, R> {
  const [busy, setBusy] = useState(false);
  const [error, setErrorText] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const setError = useCallback((message: string | null) => {
    setErrorText(message);
    setConflict(false);
  }, []);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const run = useCallback(async (...args: A): Promise<R | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fnRef.current(...args);
    } catch (err) {
      const { fallback = "That didn't go through.", onError } = optionsRef.current;
      const message = errorMessage(err, typeof fallback === "function" ? fallback(...args) : fallback);
      setErrorText(message);
      setConflict(err instanceof ApiError && err.status === 409);
      onError?.(message, err);
      return undefined;
    } finally {
      setBusy(false);
    }
  }, [setError]);

  return { run, busy, error, conflict, setError };
}
