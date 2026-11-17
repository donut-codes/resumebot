import { fireEvent, screen, waitFor } from "@testing-library/react";
import type { ParsedRole, ParseJob } from "../../api/types/profile";
import { fakeLive, pollTimers } from "../../test-live";
import { render } from "../../test-utils";
import ExamplePanel from "./ExamplePanel";

type Job = ParseJob<Partial<ParsedRole>>;

describe("Paste a job posting", () => {
  let live: ReturnType<typeof fakeLive>;
  beforeEach(() => {
    live = fakeLive();
  });
  afterEach(() => live.restore());

  it("reads the job when it starts and again on each changed message of jobs, and sets no timer", async () => {
    const timers = pollTimers();
    const answers: Job[] = [
      { state: "waiting", step: "Waiting for an agent" },
      { state: "reading", step: "Reading the posting" },
      { state: "done", step: "Done", fields: { name: "Projects Lead" } },
    ];
    let asked = 0;
    const load = vi.fn(async () => answers[Math.min(asked++, answers.length - 1)]);
    const onFilled = vi.fn();
    render(<ExamplePanel roleId="r1" onFilled={onFilled} onClose={vi.fn()} start={async () => ({ id: "j1" })} load={load} />);
    fireEvent.change(screen.getByLabelText("The job posting"), { target: { value: "A posting." } });
    fireEvent.click(screen.getByRole("button", { name: "Read it" }));
    expect(await screen.findByText("Waiting for an agent", { selector: "[role=status]" })).toBeInTheDocument();
    expect(load).toHaveBeenCalledTimes(1);
    await live.changed("gaps");
    expect(load).toHaveBeenCalledTimes(1);
    await live.changed("jobs");
    expect(await screen.findByText("Reading the posting", { selector: "[role=status]" })).toBeInTheDocument();
    await live.changed("jobs");
    await waitFor(() => expect(onFilled).toHaveBeenCalledWith({ name: "Projects Lead" }));
    expect(load).toHaveBeenCalledTimes(3);
    expect(timers.seen()).toEqual([]);
    timers.stop();
  });
});
