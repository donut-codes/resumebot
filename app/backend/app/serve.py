"""`resumebot serve`: the app on this machine."""

import argparse

#: The address the app listens on: this machine only. The Host and Origin guard (app/http_guard.py) stays, so a
#: page in the user's browser cannot call it by a name that points at loopback. A power user who wants another address
#: runs uvicorn directly.
HOST = "127.0.0.1"
#: One port for the page, the API and the doors.
DEFAULT_PORT = 8430


def main(argv: list[str]) -> int:
    """run the app on this machine (loopback only)"""
    parser = argparse.ArgumentParser(prog="resumebot serve", description=main.__doc__)
    port_help = f"the port to listen on (default {DEFAULT_PORT})"
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=port_help)
    args = parser.parse_args(argv)
    import uvicorn  # not at the top: `resumebot --version` and the commands that are not a server never load it

    from app.log_setup import uvicorn_log_config

    uvicorn.run("app.jobsearch.main:app", host=HOST, port=args.port, log_config=uvicorn_log_config())
    return 0
