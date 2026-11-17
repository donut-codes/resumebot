import { act, renderHook } from "@testing-library/react";
import { ApiError, errorMessage } from "../api/client";
import { useAction } from "./useAction";

describe("errorMessage", () => {
  it("gives the server's words for an ApiError and the fallback for anything else", () => {
    expect(errorMessage(new ApiError("That role isn't in your tracker.", 403), "Couldn't do that.")).toBe(
      "That role isn't in your tracker.",
    );
    expect(errorMessage(new Error("boom"), "Couldn't do that.")).toBe("Couldn't do that.");
    expect(errorMessage("nope", "Couldn't do that.")).toBe("Couldn't do that.");
  });
});

describe("useAction", () => {
  it("is busy while the call runs and hands back what it returned", async () => {
    let finish!: (v: number) => void;
    const fn = vi.fn((_n: number) => new Promise<number>((res) => (finish = res)));
    const { result } = renderHook(() => useAction(fn));
    expect(result.current.busy).toBe(false);
    let answer: Promise<number | undefined> | undefined;
    act(() => {
      answer = result.current.run(4);
    });
    expect(result.current.busy).toBe(true);
    await act(async () => {
      finish(9);
      await answer;
    });
    expect(result.current.busy).toBe(false);
    expect(await answer).toBe(9);
    expect(fn).toHaveBeenCalledWith(4);
    expect(result.current.error).toBeNull();
  });

  it("turns a failure into words, tells onError, and clears them on the next run", async () => {
    const onError = vi.fn();
    let fail = true;
    const fn = () => (fail ? Promise.reject(new ApiError("Not allowed.", 403)) : Promise.resolve("ok"));
    const { result } = renderHook(() => useAction(fn, { fallback: "Couldn't save that.", onError }));
    let out: string | undefined = "unset";
    await act(async () => {
      out = await result.current.run();
    });
    expect(out).toBeUndefined();
    expect(result.current.error).toBe("Not allowed.");
    expect(result.current.busy).toBe(false);
    expect(onError).toHaveBeenCalledWith("Not allowed.", expect.any(ApiError));

    fail = false;
    await act(async () => {
      await result.current.run();
    });
    expect(result.current.error).toBeNull();
  });

  it("lets one runner serve several buttons, each with its own fallback", async () => {
    const { result } = renderHook(() =>
      useAction((_words: string) => Promise.reject(new Error("boom")), { fallback: (words) => words }),
    );
    await act(async () => {
      await result.current.run("Couldn't rename that.");
    });
    expect(result.current.error).toBe("Couldn't rename that.");
  });

  it("uses the fallback for a failure that is not an ApiError", async () => {
    const { result } = renderHook(() => useAction(() => Promise.reject(new Error("boom")), { fallback: "Couldn't save that." }));
    await act(async () => {
      await result.current.run();
    });
    expect(result.current.error).toBe("Couldn't save that.");
  });
});
