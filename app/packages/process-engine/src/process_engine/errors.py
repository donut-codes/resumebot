"""Plain errors the process engine raises. Services raise these, routes translate them (never HTTPException)."""


class ProcessLoadError(ValueError):
    """A process file can't be loaded. `problems` holds every plain-words problem found, each naming the
    element (or flow) it is about, so an author fixes the file in one pass."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


class ConditionError(ValueError):
    """A condition can't be read (parse) or checked against the data model (analysis)."""


class EngineError(RuntimeError):
    """The engine was asked something it can't do: an unknown instance, a step that isn't waiting, a start
    value the data model doesn't declare."""


class NotWaiting(EngineError):
    """An answer came for a step nothing is waiting at: it was already answered (a double click, an agent reporting
    twice, a late answer from an earlier lap of a loop) or the instance has ended. Nothing was changed. Not a failure
    of the process, so a live run is never held for it (`live.attempt` lets it through)."""


class Stranded(EngineError):
    """An instance's own file can't be loaded any more (a release removed a card or instruction file it names, or its
    text was never kept). The engine moves such an instance onto the current file (`Engine.move_stranded`)."""


class RunOver(Exception):
    """Raised by an action to say the run it belongs to has nothing left to do: what the run was for was settled
    elsewhere before the run got to this step (a run moved onto a new file by `Engine.move_stranded` meets a plan the
    owner already declined). It isn't a failure: the engine ends the run as cancelled, with `message` as the reason, and
    runs nothing after the step. The action changes nothing before it raises this."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ActionError(Exception):
    """Raised by an action to say a named thing went wrong. An error boundary event on the step whose
    `errorRef` is `code` (or any error boundary with no code) catches it and routes the process on; with no
    boundary to catch it the instance stops as failed, with this recorded."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")
