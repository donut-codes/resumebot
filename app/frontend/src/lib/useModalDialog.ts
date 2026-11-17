import { useEffect, useRef, type SyntheticEvent } from "react";

/** What a popup over the page needs: opened as a modal where the browser can, held open otherwise, closed again when the
 * panel goes. Escape asks to close (`onClose`) instead of closing it itself. Spread `dialogProps` on the `<dialog>`. */
export function useModalDialog(onClose: () => void) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    if (typeof el.showModal === "function" && !el.open) el.showModal();
    else el.setAttribute("open", "");
    return () => {
      if (typeof el.close === "function" && el.open) el.close();
    };
  }, []);
  return {
    ref,
    onCancel: (e: SyntheticEvent) => {
      e.preventDefault();
      onClose();
    },
  };
}
