import { useEffect, useRef } from "react";
import { msUntilOwnerMidnight, ownerToday } from "./dates";

/** Calls `onNewDay` when the owner's calendar day changes: at their midnight, and when the tab becomes visible again
 * (a timer sleeps with the machine or a hidden tab). */
export function useOwnerNewDay(onNewDay: () => void): void {
  const latest = useRef(onNewDay);
  useEffect(() => {
    latest.current = onNewDay;
  });
  useEffect(() => {
    let day = ownerToday();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = () => {
      const now = ownerToday();
      if (now !== day) {
        day = now;
        latest.current();
      }
    };
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        check();
        arm();
      }, msUntilOwnerMidnight() + 500);
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        check();
        arm();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    arm();
    return () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
}
