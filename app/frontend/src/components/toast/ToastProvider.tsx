import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";

interface ToastItem {
  id: number;
  message: string;
}

interface ToastContextValue {
  /** Shows a toast for ~5s (or `ms`), then auto-dismisses. */
  showToast: (message: string, ms?: number) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const AUTO_DISMISS_MS = 5000;

/** Bottom-left toasts (the ui spec; the mock's `.toast`): aria-live
 * polite, auto-dismiss, no "Undo" in release 1. */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(0);

  const showToast = useCallback((message: string, ms: number = AUTO_DISMISS_MS) => {
    const id = nextId.current++;
    setToasts((t) => [...t, { id, message }]);
    setTimeout(() => {
      setToasts((t) => t.filter((x) => x.id !== id));
    }, ms);
  }, []);

  const value = useMemo(() => ({ showToast }), [showToast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-stack" aria-live="polite" role="status">
        {toasts.map((t) => (
          <div className="toast" key={t.id}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    // A screen used outside <ToastProvider> (e.g. a unit test rendering it
    // in isolation) still works -- it just can't show a toast.
    return { showToast: () => {} };
  }
  return ctx;
}
