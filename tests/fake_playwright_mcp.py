"""A scripted stdio JSON-RPC server standing in for Playwright MCP.

Run as a subprocess by the browser-client tests, so the real transport is exercised end to end: a
process, two pipes, line-delimited JSON-RPC, the initialize handshake, and responses framed the way
Playwright MCP frames them (markdown `### Result` / `### Error` sections inside one text block).

The script is driven by a JSON file named in argv, so a test scripts a whole session — including the
failure shapes a fake that only ever succeeded would hide: a dead process, an error result, a
malformed frame.

    {"tools": {"browser_evaluate": {"result": [{"text": "hi", "side": "in"}]},
               "browser_click": {"error": "no element matches"},
               "browser_navigate": {"text": "### Page\\nabout:blank"}},
     "on_start": "die" | null,
     "malformed": ["browser_snapshot"]}
"""

from __future__ import annotations

import json
import sys


def _framed(entry: dict) -> dict:
    """One MCP tool result, framed as Playwright MCP frames it."""
    if "text" in entry:
        return {"content": [{"type": "text", "text": entry["text"]}], "isError": False}
    if "error" in entry:
        return {
            "content": [{"type": "text", "text": f"### Error\n{entry['error']}"}],
            "isError": True,
        }
    body = json.dumps(entry.get("result"), indent=2)
    text = f"### Result\n{body}\n### Ran Playwright code\n```js\nawait page.evaluate(...);\n```"
    return {"content": [{"type": "text", "text": text}], "isError": False}


def main(argv) -> int:
    script = json.loads(open(argv[1]).read()) if len(argv) > 1 else {}
    # Every tool call is appended here so a test can assert on what the client actually sent without
    # the client having to keep a record for the test's benefit.
    calls_log = argv[1] + ".calls" if len(argv) > 1 else None
    if script.get("on_start") == "die":
        return 1
    calls_seen: dict = {}
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method, msg_id = message.get("method"), message.get("id")
        if msg_id is None:
            continue  # a notification (notifications/initialized) gets no reply
        if method == "initialize":
            if script.get("handshake") == "silent":
                # Alive, reading its pipe, and never answering — a slow npx fetch, or a server that
                # starts and never completes startup. The distinction that matters to the client is
                # that the process does NOT exit, so its own restart guard would otherwise see a
                # live process and decline to try again for the rest of the daemon's life.
                continue
            reply = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "FakePlaywright", "version": "1"},
            }
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            calls_seen[name] = calls_seen.get(name, 0) + 1
            if calls_log:
                with open(calls_log, "a") as handle:
                    entry = {"tool": name, "arguments": params.get("arguments")}
                    handle.write(json.dumps(entry) + "\n")
            if name in script.get("malformed", []):
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
                continue
            if name in script.get("die_on", []):
                return 2  # the server exits mid-call
            entry = (script.get("tools") or {}).get(name)
            if entry is None:
                reply = _framed({"error": f"unknown tool {name}"})
            else:
                # A per-call list lets a test script a first answer and a different second one.
                if isinstance(entry, list):
                    index = min(calls_seen[name], len(entry)) - 1
                    entry = entry[index]
                reply = _framed(entry)
        else:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"no method {method}"},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            continue
        # An unrelated notification before the reply: a real server interleaves them, and the client
        # has to skip anything that is not the reply it is waiting for.
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}) + "\n")
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": reply}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
