#!/usr/bin/env python3
"""granola webhook receiver — event-driven trigger for the granola-mirror pipeline.

Granola pushes note.generated / note.edited / note.access_granted / note.regenerated
events (Standard Webhooks spec: HMAC-SHA256 over "{webhook-id}.{webhook-timestamp}.{body}")
to https://<hooks host>/granola, which a public tunnel or reverse proxy routes to this listener on
127.0.0.1:8097. Payloads carry no content — just note_id — so a verified event
simply kicks refresh.sh (pull → transcripts → note → commit) for the whole mirror,
debounced: a pending marker plus a single-flight runner coalesce event bursts into
one run. refresh.sh now self-locks the whole pipeline (~/.locks/granola-pipeline), so
the runner no longer wraps it in a caller-side flock; its own single-flight lock just
coalesces bursts. The hourly cron stays as the fallback sweep for missed deliveries.

Hardening: the server is threaded with a per-request timeout (a slow client can't wedge
delivery), an oversized or non-numeric Content-Length is refused before the body is read,
the mirror path is required — from the GRANOLA_MIRROR environment variable or the
~/.config/granola/env constants file, with no personal default in code — and it reaches
the runner as an argv element, never interpolated into the shell string.

The signing secret lives at ~/.config/granola/webhook-secret (0600; written once at
endpoint registration — Granola returns it only in the registration response). The
receiver refuses to start without it: an unverifiable endpoint would accept forged
posts, and silently-off verification is worse than a loud failed unit.

Registration (one-time, needs the public route live for delivery, not for the call):
  curl -X POST https://public-api.granola.ai/v1/webhook-endpoints \
    -H "Authorization: Bearer $(cat ~/.config/granola/api-key)" \
    -H "Content-Type: application/json" \
    -d '{"url": "https://<hooks host>/granola", "scopes": ["personal", "public"]}'
"""
import base64
import hashlib
import hmac
import http.server
import json
import os
import subprocess
import sys
import time

PORT = 8097
PATH = "/granola"
SECRET_FILE = os.path.expanduser("~/.config/granola/webhook-secret")
STATE = os.path.expanduser("~/.local/state")
PENDING = os.path.join(STATE, "granola-webhook-pending")
EVENTS = os.path.join(STATE, "granola-webhook-events.jsonl")
LOCKS = os.path.expanduser("~/.locks")
ENV_FILE = os.path.expanduser("~/.config/granola/env")


def _env_file_value(key, path=ENV_FILE):
    """Read one KEY=VALUE from the per-deployment constants file (optional `export `
    prefix and #-comments tolerated; sourceable from shell). Missing file/key -> None."""
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                line = line.removeprefix("export ")
                k, sep, v = line.partition("=")
                if sep and k.strip() == key:
                    return v.strip().strip("'\"") or None
    except OSError:
        pass
    return None


# No personal default in code: a deployer who configured neither the environment nor
# ~/.config/granola/env must get a refusal, not a silently-materialised empty mirror at
# someone else's path (see __main__).
MIRROR = os.environ.get("GRANOLA_MIRROR") or _env_file_value("GRANOLA_MIRROR")
REFRESH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "refresh.sh")
TOLERANCE_S = 300
MAX_BODY = 64 * 1024        # a signed Granola event is a few hundred bytes; anything larger is abuse
REQUEST_TIMEOUT_S = 15      # bound how long one request may hold a worker (slowloris)
HANDLED = {"note.generated", "note.edited", "note.access_granted", "note.regenerated"}

# Coalesce a burst of events into one pipeline run: the marker records "work arrived", the
# runner lock keeps a single consumer, and the while-loop re-runs if more events landed
# during a run. refresh.sh self-locks the whole pipeline, so this no longer wraps it in a
# caller-side flock. REFRESH and MIRROR arrive as "$1"/"$2" (argv), never interpolated, so a
# hostile mirror path cannot break out of the shell string; only the HOME-derived constants
# below are interpolated.
RUNNER = f"""
exec 9>"{LOCKS}/granola-webhook-runner"; flock -n 9 || exit 0
sleep 10
while [ -e "{PENDING}" ]; do
  rm -f "{PENDING}"
  "$1" --commit "$2" 2>&1 | logger -t granola-refresh
done
"""


def _keys():
    with open(SECRET_FILE) as fh:              # closed explicitly: this runs per POST, threaded
        raw = fh.read().strip()
    keys = []
    # Standard Webhooks secrets are "whsec_" + base64; the HMAC key is the decoded
    # bytes. Keep the raw string as a fallback in case Granola signs with it as-is.
    if raw.startswith("whsec_"):
        try:
            keys.append(base64.b64decode(raw[6:]))
        except Exception:
            pass
    keys.append(raw.encode())
    return keys


def _sig_ok(headers, body):
    wid = headers.get("webhook-id", "")
    ts = headers.get("webhook-timestamp", "")
    sigs = headers.get("webhook-signature", "")
    if not (wid and ts and sigs):
        return False
    try:
        if abs(time.time() - int(ts)) > TOLERANCE_S:
            return False
    except ValueError:
        return False
    msg = f"{wid}.{ts}.".encode() + body
    for key in _keys():
        want = base64.b64encode(hmac.new(key, msg, hashlib.sha256).digest()).decode()
        # header holds space-separated "v1,<base64>" entries
        for part in sigs.split():
            got = part.split(",", 1)[-1]
            if hmac.compare_digest(got, want):
                return True
    return False


def _kick():
    open(PENDING, "w").close()
    # bash -c SCRIPT NAME REFRESH MIRROR -> $0=_, $1=REFRESH, $2=MIRROR (argv, not interpolated)
    subprocess.Popen(["bash", "-c", RUNNER, "_", REFRESH, MIRROR], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT_S     # a slow client's socket read times out instead of hanging

    def _respond(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # health probe for tunnel-routing checks; no auth, no effect
        if self.path == PATH:
            self._respond(200, b"granola-webhook ok\n")
        else:
            self._respond(404)

    def do_POST(self):
        if self.path != PATH:
            self._respond(404)
            return
        # Bound the read before touching the body: a non-numeric length is a malformed
        # request (400), an oversized one is refused unread (413) so a huge Content-Length
        # can't make a worker allocate against it.
        raw_len = self.headers.get("Content-Length")
        try:
            length = int(raw_len) if raw_len is not None else 0
        except ValueError:
            self._respond(400)
            return
        if length < 0 or length > MAX_BODY:
            self._respond(413)
            return
        body = self.rfile.read(length)
        if not _sig_ok(self.headers, body):
            self._respond(401)
            return
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400)
            return
        etype = event.get("event_type", "")
        with open(EVENTS, "a") as fh:
            fh.write(json.dumps({"received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "event_type": etype,
                                 "note_id": event.get("note_id", ""),
                                 "event_id": event.get("event_id", "")}) + "\n")
        if etype in HANDLED:
            _kick()
        self._respond(204)

    def log_message(self, fmt, *args):  # journald via systemd captures stderr
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    if not os.path.isfile(SECRET_FILE):
        sys.exit(f"granola-webhook: no signing secret at {SECRET_FILE} — register the "
                 "endpoint first (see module docstring); refusing to run unverifiable")
    # Three facts, no default: what's missing, why it matters, how to fix. Downstream tools
    # mkdir what they're given, so a wrong/absent GRANOLA_MIRROR would materialise an empty
    # mirror at the wrong path and quietly commit it.
    if not MIRROR or not os.path.isdir(MIRROR):
        sys.exit("granola-webhook: GRANOLA_MIRROR is unset or not a directory "
                 f"({MIRROR!r}) — a kicked pipeline would create an empty mirror at the "
                 "wrong place; set GRANOLA_MIRROR in the environment or in "
                 f"{ENV_FILE} and restart")
    os.makedirs(STATE, exist_ok=True)
    os.makedirs(LOCKS, exist_ok=True)
    # Threaded so one slow request can't block delivery of the next; each request is bounded
    # by Handler.timeout.
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
