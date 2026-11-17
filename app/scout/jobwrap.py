"""Runs a command in a Windows Job Object that ends everything in it when this process ends (docs/PRD.md path 52.6).

    py -3 jobwrap.py -- <command> [args...]

A lane's Claude session starts the DevTools MCP server, git and whatever else its tools start. Ending the session's process
ended only that process (`taskkill /T` walks the tree it can see, and a process that was started and went away leaves its
children where no walk finds them). A Job Object holds every process started under it, whoever starts it, so when this
wrapper ends, however it ends (the lane kills it, a crash, Task Manager), the system ends all of them
(`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`). Breakaway is not allowed, so no child leaves the job. The command's own standard
input, output and error are this process's, and its exit code is returned.

If the job cannot be made or joined, the command runs without one and says so on standard error: the lane's `taskkill /T`
is still the fallback. Not Windows: the command is simply run.
"""

import subprocess
import sys

KILL_ON_JOB_CLOSE = 0x2000
EXTENDED_LIMIT_INFORMATION = 9


def put_this_process_in_a_job() -> str:
    """Make a job that is killed when its last handle closes, and join it. "" when done, else why not."""
    if sys.platform != "win32":
        return "not Windows"
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return f"CreateJobObject failed ({ctypes.get_last_error()})"
    info = Extended()
    info.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
        return f"SetInformationJobObject failed ({ctypes.get_last_error()})"
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        return f"AssignProcessToJobObject failed ({ctypes.get_last_error()})"
    return ""  # the handle stays open for as long as this process lives: its closing is what ends the job


def main(argv: list[str]) -> int:
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        sys.stderr.write("usage: jobwrap.py -- <command> [args...]\n")
        return 2
    command = argv[argv.index("--") + 1:]
    why = put_this_process_in_a_job()
    if why and why != "not Windows":
        sys.stderr.write(f"jobwrap: running without a job object ({why})\n")
    try:
        return subprocess.Popen(command).wait()
    except OSError as exc:
        sys.stderr.write(f"jobwrap: could not start {command[0]}: {exc}\n")
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
