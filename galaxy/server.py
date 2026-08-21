#!/usr/bin/env python3
"""Serve viewer/ on 127.0.0.1:4700. Standard library only.

Bound to loopback deliberately and not configurably. This box has a public IP
and no authentication in front of this page; binding 0.0.0.0 would publish the
full text of every note to the internet. Reach it over an SSH tunnel:

    ssh -N -L 4700:127.0.0.1:4700 jarvis

There is no --host flag on purpose. A flag is an invitation, and the only
reason anyone would reach for it is the thing that must not happen.
"""

from __future__ import annotations

import sys
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

HOST = "127.0.0.1"
PORT = 4700
VIEWER = Path(__file__).resolve().parent / "viewer"


class Handler(SimpleHTTPRequestHandler):
    """Static files out of one directory, never cached."""

    def end_headers(self) -> None:
        # graph-data.js is rewritten by build.py whenever the vault changes.
        # A cached copy means a reload shows yesterday's galaxy and looks like
        # the scanner is broken.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    if not VIEWER.is_dir():
        print(f"No viewer directory at {VIEWER}", file=sys.stderr)
        return 1
    if not (VIEWER / "graph-data.js").is_file():
        # Not fatal — the page loads and says so — but this is the actual cause
        # of an empty galaxy, and saying it here saves a debugging session.
        print("warning: viewer/graph-data.js missing — run build.py first", file=sys.stderr)

    # directory= confines the handler to viewer/; SimpleHTTPRequestHandler also
    # normalises away any ".." in the request path.
    handler = partial(Handler, directory=str(VIEWER))
    server = HTTPServer((HOST, PORT), handler)

    print(f"Serving {VIEWER} on http://{HOST}:{PORT} (loopback only)")
    print(f"From your Mac:  ssh -N -L {PORT}:127.0.0.1:{PORT} jarvis")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
