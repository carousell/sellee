"""A server that does its real work in a child process — the `npx` -> `node` shape.

`default_command` runs the server as `npx @playwright/mcp`, and npx is a launcher: the process the
daemon holds is the wrapper, and the server that actually talks to Chrome is its child. Signalling
only the wrapper leaves that child alive, still connected. This stands in for that shape so the
kill path can be tested without npx.

argv: <pidfile> <fake server script> <script json>
"""

import os
import subprocess
import sys

pidfile, server, script = sys.argv[1], sys.argv[2], sys.argv[3]

# The part that outlives a naive terminate() of the process the client is holding.
child = subprocess.Popen(  # noqa: S603 — argv is built here, never a shell string
    [sys.executable, "-c", "import time; time.sleep(120)"]
)
with open(pidfile, "w") as handle:
    handle.write(str(child.pid))
    handle.flush()
    os.fsync(handle.fileno())

# Become the real server, so the client's handshake and tool calls work as they always do.
os.execv(sys.executable, [sys.executable, server, script])
