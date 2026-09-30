"""Shared sandbox for the granola-mirror suites.

The real scripts (`notes.sh`, `refresh.sh`) run against a throwaway workspace with the
shape of a live deployment: a mirror dir, `meetings/notes/`, `workflows/meetings/`, and a
redirected HOME so `~/.local/state` and `~/.locks` are sandboxed. `claude` and `curl` are
stubbed on PATH — nothing reaches a model or a phone. The `claude` stub records every
invocation (and its argv) and emits a fixed note body, so "did a generation happen" and
"with which flags" are both assertable, and no test spends a rate-limit token.

Modelled on the notion-mirror refresh suite: a clone pointed at a workspace it does
not live inside, driven end to end through the paths the real cron uses.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

# The repository root (the real scripts run in place so readlink -f resolves the skill).
GM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES_SH = os.path.join(GM, "notes.sh")
REFRESH_SH = os.path.join(GM, "refresh.sh")
MIGRATE = os.path.join(GM, "migrate-banners.py")

# A claude stub: records the call + its argv, emits a body per CLAUDE_STUB_MODE. `ok` -> a
# heading-led body (a success); `fail` -> non-zero; `empty` -> nothing; `nonhash` -> a body
# that doesn't start with '#'. notes.sh treats the last three as failed generations. A single
# meeting can be failed selectively (for the "one wedged while others succeed" case) by
# putting the literal GRANOLA_FAIL_TOKEN in its mirror content — the stub sees it on stdin.
CLAUDE_STUB = r'''#!/usr/bin/env bash
printf '%s\n' "called" >> "$CLAUDE_CALLS"
for a in "$@"; do printf '%s\0' "$a"; done >> "$CLAUDE_ARGV"
printf '\036' >> "$CLAUDE_ARGV"
input="$(cat)"
printf '%s\036' "$input" >> "$CLAUDE_INPUTS"
case "${CLAUDE_STUB_MODE:-ok}" in
  fail)    printf 'boom: %s\n' "${CLAUDE_STUB_ERR:-stub failure}" >&2; exit 1 ;;
  empty)   exit 0 ;;
  nonhash) printf 'not a heading\n'; exit 0 ;;
esac
if printf '%s' "$input" | grep -q 'GRANOLA_FAIL_TOKEN'; then
  printf 'boom: this meeting is wedged\n' >&2; exit 1
fi
printf '# Meeting note\n\nBody paragraph.\n'
# The glossary tail notes.sh asks for: `none` by default, CLAUDE_STUB_GLOSSARY's rows when
# set, and no marker at all under CLAUDE_STUB_MODE=nomarker.
[ "${CLAUDE_STUB_MODE:-ok}" = nomarker ] && exit 0
printf '\n<!-- glossary-additions -->\n%s\n' "${CLAUDE_STUB_GLOSSARY:-none}"
'''

# Stubs for the network fetchers refresh.sh calls by SELF_DIR-relative path — copied into the
# clone the refresh sandbox assembles. No network; exit codes are env-driven so the OAuth
# (rc 3) and fetch-failure paths are reachable.
GRANOLA_STUB = '''#!/usr/bin/env bash
cf=""
while [ $# -gt 0 ]; do case "$1" in --changed-file) cf="$2"; shift 2;; *) shift;; esac; done
[ -n "$cf" ] && : > "$cf"
exit "${GRANOLA_SYNC_RC:-0}"
'''
TRANSCRIPTS_STUB = '#!/usr/bin/env bash\nexit "${TRANSCRIPTS_RC:-0}"\n'
DIGEST_STUB = '#!/usr/bin/env bash\nprintf \'digest stub called\\n\'\nexit "${DIGEST_RC:-0}"\n'
GEMINI_NOTES_STUB = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GEMINI_NOTES_CALLS"
printf 'gemini-notes stub called: %s\\n' "$*"
if [ -n "${GEMINI_NOTES_STDERR:-}" ]; then
  printf '%s\\n' "$GEMINI_NOTES_STDERR" >&2
fi
exit "${GEMINI_NOTES_RC:-0}"
'''

CURL_STUB = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SANDBOX_NTFY_LOG"
exit 0
'''


GEMINI_NOTES = os.path.join(GM, "gemini-notes")
GRANOLA = os.path.join(GM, "granola")

# An rclone stub for gemini-notes, serving files from RCLONE_STUB_DIR: `backend query` prints
# query.json (or fails with RCLONE_QUERY_ERR on stderr when that is set), `backend copyid`
# copies docs/<id>.md to the destination (or fails when <id> is in RCLONE_FAIL_IDS), and
# `config dump` prints config.json. Every call's argv is appended to RCLONE_CALLS as JSON.
RCLONE_STUB = r'''#!/usr/bin/env python3
import json, os, shutil, sys
d = os.environ["RCLONE_STUB_DIR"]
with open(os.environ["RCLONE_CALLS"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:2] == ["backend", "query"]:
    if os.environ.get("RCLONE_QUERY_ERR"):
        sys.stderr.write(os.environ["RCLONE_QUERY_ERR"] + "\nsecond line\n"); sys.exit(1)
    sys.stdout.write(open(os.path.join(d, "query.json")).read())
elif args[:2] == ["backend", "copyid"]:
    doc, dest = args[3], args[4]
    if doc in os.environ.get("RCLONE_FAIL_IDS", "").split():
        sys.stderr.write('2026/09/30 16:01:58 NOTICE: Failed to backend: command "copyid" failed: '
                         'couldn\'t find id: googleapi: Error 404: File not found: %s., notFound\n' % doc)
        sys.exit(1)
    shutil.copy(os.path.join(d, "docs", doc + ".md"), dest)
elif args[:2] == ["config", "dump"]:
    sys.stdout.write(open(os.path.join(d, "config.json")).read())
else:
    sys.stderr.write("rclone stub: unexpected call %r\n" % args); sys.exit(9)
'''

# Python's startup hook, put on PYTHONPATH for a script under test: it replaces
# urllib.request.urlopen with a router over HTTP_STUB_ROUTES, so a script that calls
# urllib.request.urlopen reaches no network. A route key is "METHOD URL-without-query",
# plus " <pageToken>" for a later page; its value is {"status": N, "body": <json>}, or
# {"timeout": true} for a server that accepts the request and never answers. An unrouted
# request answers 404. A route may set `incomplete_read: true` to raise
# http.client.IncompleteRead, or `http_error_body_incomplete_read: true` to raise it when
# reading an HTTP error body. Every request is appended to HTTP_STUB_CALLS as JSON.
SITECUSTOMIZE = r'''
import http.client, io, json, os, urllib.error, urllib.parse, urllib.request

def _urlopen(req, data=None, timeout=None, **kw):
    if isinstance(req, str):
        req = urllib.request.Request(req, data=data)
    u = urllib.parse.urlsplit(req.full_url)
    q = dict(urllib.parse.parse_qsl(u.query))
    body = req.data.decode() if req.data else None
    with open(os.environ["HTTP_STUB_CALLS"], "a") as f:
        f.write(json.dumps({"method": req.get_method(), "url": req.full_url, "body": body,
                            "headers": dict(req.header_items())}) + "\n")
    key = "%s %s://%s%s" % (req.get_method(), u.scheme, u.netloc, u.path)
    if q.get("pageToken"):
        key += " " + q["pageToken"]
    with open(os.environ["HTTP_STUB_ROUTES"]) as f:
        route = json.load(f).get(key, {"status": 404, "body": {"error": "no stub route: " + key}})
    if route.get("timeout"):
        raise TimeoutError("timed out")
    if route.get("incomplete_read"):
        raise http.client.IncompleteRead(b"partial", 10)
    if route.get("http_error_body_incomplete_read"):
        class IncompleteBody:
            def read(self, *args, **kwargs):
                raise http.client.IncompleteRead(b"partial", 10)
            def close(self):
                pass
        raise urllib.error.HTTPError(req.full_url, route["status"], "stub", {}, IncompleteBody())
    raw = json.dumps(route["body"]).encode()
    if route["status"] >= 400:
        raise urllib.error.HTTPError(req.full_url, route["status"], "stub", {}, io.BytesIO(raw))
    resp = io.BytesIO(raw)
    resp.status = route["status"]
    return resp

if os.environ.get("HTTP_STUB_ROUTES"):
    urllib.request.urlopen = _urlopen
'''


def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class GranolaSandbox(unittest.TestCase):
    """A throwaway workspace + redirected HOME; the real notes.sh/refresh.sh run against it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = os.path.join(self.tmp, "ws")              # the workspace (mirror's parent)
        self.mirror = os.path.join(self.ws, "granola")      # the mirror dir
        self.notes = os.path.join(self.ws, "meetings", "notes")
        self.wf = os.path.join(self.ws, "workflows", "meetings")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".local", "state")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.mirror, self.notes, self.wf, self.state, self.bin,
                  os.path.join(self.home, ".locks")):
            os.makedirs(d, exist_ok=True)
        write_exec(os.path.join(self.bin, "claude"), CLAUDE_STUB)
        write_exec(os.path.join(self.bin, "curl"), CURL_STUB)
        self.claude_calls = os.path.join(self.tmp, "claude-calls")
        self.claude_argv = os.path.join(self.tmp, "claude-argv")
        self.claude_inputs = os.path.join(self.tmp, "claude-inputs")
        self.ntfy_log = os.path.join(self.tmp, "ntfy")
        self.gemini_notes_calls = os.path.join(self.tmp, "gemini-notes-calls")

    # --- env / invocation -------------------------------------------------
    def env(self, **over):
        e = dict(os.environ,
                 HOME=self.home,
                 PATH=self.bin + os.pathsep + os.environ["PATH"],
                 CLAUDE_CALLS=self.claude_calls,
                 CLAUDE_ARGV=self.claude_argv,
                 CLAUDE_INPUTS=self.claude_inputs,
                 SANDBOX_NTFY_LOG=self.ntfy_log,
                 GEMINI_NOTES_CALLS=self.gemini_notes_calls)
        e.pop("GRANOLA_LOCK_HELD", None)
        e.pop("GEMINI_RCLONE_REMOTE", None)
        e.update(over)
        return e

    def notes_sh(self, *args, **env):
        return subprocess.run(("bash", NOTES_SH) + args, env=self.env(**env),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def refresh_sh(self, *args, **env):
        return subprocess.run(("bash", REFRESH_SH) + args, env=self.env(**env),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def build_refresh_clone(self):
        """Assemble a clone whose refresh.sh resolves stub fetchers + the real notes.sh by
        SELF_DIR, so a full pipeline run touches no network. Returns the clone's refresh.sh."""
        clone = os.path.join(self.tmp, "clone")
        skills = os.path.join(clone, "skills", "meetings")
        os.makedirs(skills, exist_ok=True)
        shutil.copy(REFRESH_SH, os.path.join(clone, "refresh.sh"))
        shutil.copy(NOTES_SH, os.path.join(clone, "notes.sh"))
        shutil.copy(os.path.join(GM, "skills", "meetings", "SKILL.md"),
                    os.path.join(skills, "SKILL.md"))
        write_exec(os.path.join(clone, "granola"), GRANOLA_STUB)
        write_exec(os.path.join(clone, "granola-transcripts"), TRANSCRIPTS_STUB)
        write_exec(os.path.join(clone, "gemini-notes"), GEMINI_NOTES_STUB)
        self.refresh = os.path.join(clone, "refresh.sh")
        return self.refresh

    def enable_digest_stub(self):
        write_exec(os.path.join(self.bin, "granola-digest"), DIGEST_STUB)

    def gemini_notes_calls_made(self):
        if not os.path.exists(self.gemini_notes_calls):
            return []
        with open(self.gemini_notes_calls) as f:
            return f.read().splitlines()

    def run_refresh(self, *args, **env):
        return subprocess.run(("bash", self.refresh) + args, env=self.env(**env),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def seed_floor(self, date="2000-01-01"):
        with open(os.path.join(self.state, "granola-notes-since"), "w") as f:
            f.write(date)

    def hash_note(self, note_path):
        r = subprocess.run(("bash", NOTES_SH, "--hash", note_path), env=self.env(),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        return r.stdout.strip()

    # --- fixtures ---------------------------------------------------------
    def mirror_file(self, name, updated="2026-08-19T10:00:00Z", summary="A real summary.",
                    transcript="Alice: hello there.  Bob: hi Alice.", tmark="__match__",
                    header_line=True, event=None):
        """Write one mirror .md. tmark '__match__' stamps the transcript with `updated`
        (coherent); None omits the marker (grandfathered); any string stamps that literal.
        summary=None uses granola's no-summary placeholder (unsettled). transcript=None omits
        the section. header_line=False drops the `granola updated_at` comment (unstamped)."""
        path = os.path.join(self.mirror, name)
        lines = []
        if header_line:
            lines.append("<!-- granola updated_at: %s -->" % updated)
        lines += ["# %s" % name.rsplit(".", 1)[0], "",
                  "- **Date:** %s" % updated[:10]]
        if event:
            lines.append("- **Calendar event:** %s" % event)
        lines += ["", summary if summary is not None else "_(no summary)_"]
        if transcript is not None:
            lines += ["", "## Transcript", ""]
            if tmark is not None:
                stamp = updated if tmark == "__match__" else tmark
                lines.append("<!-- transcript for updated_at: %s -->" % stamp)
                lines.append("")
            lines.append(transcript)
        with open(path, "w") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        return path

    def gemini_file(self, name, modified="2026-09-30T12:00:00.000Z", event="evt123",
                    transcript=True, meet_lines=None, topic_heading=None):
        """Writes one Gemini mirror file under <workspace>/meetings/gemini/ as gemini-notes
        writes it, for suites that read the mirror. `name` is the basename (`<date>-<slug>-gem_<doc id>.md`); event=None omits the
        Calendar event line; transcript=False gives a notes-only doc (no turns);
        meet_lines, a list of `[HH:MM:SS] **Name:** text` lines, appends a Meet section
        (an empty list also writes the Transcript-tab ending heading with no turns, matching
        an empty capture)."""
        doc_id = name.rsplit("-gem_", 1)[1][:-3]
        lines = ["<!-- gemini modified: %s -->" % modified, "# Team sync", "",
                 "- **Date:** %s" % name[:10]]
        if event:
            lines.append("- **Calendar event:** %s" % event)
        lines += ["- **Google Doc:** https://docs.google.com/document/d/%s/edit" % doc_id, "",
                  "## ", "", "Gemini's short AI summary.", "", "## **Team sync**", ""]
        if topic_heading:
            lines += ["## **%s**" % topic_heading, ""]
        lines += ["### **Summary**", "", "Alice and Bob agreed on the plan.", "",
                  "### **Next steps**", "", "* [Bob Example] Send the draft.", ""]
        if transcript:
            lines += ["## **Team sync \\- Transcript**", "", "### **00:00:05** {#00:00:05}", "",
                      "**Alice Example:** Hello Bob.", "", "**Bob Example:** Hi Alice.", "",
                      "### **Transcription ended after 00:01:00**", ""]
        elif meet_lines == []:
            lines += ["## **Team sync \\- Transcript**", "",
                      "### **Transcription ended after 00:00:11**", ""]
        text = "\n".join(lines).rstrip() + "\n"
        if meet_lines is not None:
            section = ["## Meet transcript", "", "<!-- meet transcript: conferenceRecords/rec1/transcripts/t1 -->", ""]
            text += "\n" + "\n".join(section + meet_lines).rstrip() + "\n"
        gemini = os.path.join(self.ws, "meetings", "gemini")
        os.makedirs(gemini, exist_ok=True)
        path = os.path.join(gemini, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def note_path(self, mirror_name):
        return os.path.join(self.notes, mirror_name.rsplit(".", 1)[0] + ".note.md")

    def write_note(self, mirror_name, banner, body="# Note\n\nHand body.\n"):
        p = self.note_path(mirror_name)
        with open(p, "w") as f:
            f.write(banner.rstrip("\n") + "\n\n" + body)
        return p

    def read(self, path):
        with open(path) as f:
            return f.read()

    # --- observations -----------------------------------------------------
    def claude_call_count(self):
        if not os.path.exists(self.claude_calls):
            return 0
        with open(self.claude_calls) as f:
            return len([l for l in f if l.strip()])

    def last_claude_argv(self):
        """argv of the last claude call, as a list (each arg null-terminated, record sep \\x1e).
        Empty args (like the value of --tools "") are preserved; only the trailing terminator
        artifact is dropped."""
        if not os.path.exists(self.claude_argv):
            return None
        with open(self.claude_argv, "rb") as f:
            raw = f.read()
        recs = [r for r in raw.split(b"\x1e") if r]
        if not recs:
            return None
        parts = recs[-1].split(b"\x00")
        if parts and parts[-1] == b"":      # every arg is followed by \0 -> trailing empty
            parts = parts[:-1]
        return [a.decode() for a in parts]

    def last_claude_input(self):
        """Stdin for the last stub call, for checking the capture block the model receives."""
        if not os.path.exists(self.claude_inputs):
            return None
        with open(self.claude_inputs, "rb") as f:
            inputs = f.read().split(b"\x1e")
        if not inputs:
            return None
        return inputs[-2].decode() if inputs[-1] == b"" else inputs[-1].decode()

    def run_summary(self):
        p = os.path.join(self.state, "granola-notes-run.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def wedge(self, mirror_name):
        base = mirror_name.rsplit(".", 1)[0]
        p = os.path.join(self.state, "granola-note-wedge", base + ".json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def held_marker(self, mirror_name):
        base = mirror_name.rsplit(".", 1)[0]
        return os.path.exists(os.path.join(self.state, "granola-note-held-" + base))

    def ntfy_calls(self):
        if not os.path.exists(self.ntfy_log):
            return []
        with open(self.ntfy_log) as f:
            return [l for l in f.read().splitlines() if l.strip()]


class GeminiSandbox(GranolaSandbox):
    """GranolaSandbox plus a stubbed Drive (rclone on PATH) and a stubbed HTTP layer for the
    Meet and OAuth calls. By default the query returns no docs, the token refresh succeeds
    and the account joined no conferences; tests add docs with `add_doc` and Meet data with
    `route`."""

    TOKEN_URL = "https://oauth2.googleapis.com/token"
    MEET = "https://meet.googleapis.com/v2"

    def setUp(self):
        super().setUp()
        self.gemini = os.path.join(self.ws, "meetings", "gemini")
        self.rclone_dir = os.path.join(self.tmp, "rclone")
        os.makedirs(os.path.join(self.rclone_dir, "docs"))
        self.rclone_calls_log = os.path.join(self.tmp, "rclone-calls")
        write_exec(os.path.join(self.bin, "rclone"), RCLONE_STUB)
        self.pyhook = os.path.join(self.tmp, "pyhook")
        os.makedirs(self.pyhook)
        with open(os.path.join(self.pyhook, "sitecustomize.py"), "w") as f:
            f.write(SITECUSTOMIZE)
        self.http_routes = os.path.join(self.tmp, "http-routes.json")
        self.http_calls_log = os.path.join(self.tmp, "http-calls")
        self.query = []
        self._save_query()
        rclone_token = json.dumps({"access_token": "stale-access", "token_type": "Bearer",
                                   "refresh_token": "refresh-123", "expiry": "2026-09-30T00:00:00Z",
                                   "expires_in": 3599})
        with open(os.path.join(self.rclone_dir, "config.json"), "w") as f:
            json.dump({"gdrive": {"type": "drive", "client_id": "client-abc",
                                  "client_secret": "secret-xyz", "scope": "drive.readonly",
                                  "team_drive": "", "token": rclone_token}}, f)
        self.routes = {}
        self.route("POST", self.TOKEN_URL, {"access_token": "fresh-access", "expires_in": 3599,
                                            "token_type": "Bearer"})
        self.route("GET", self.MEET + "/conferenceRecords", {})

    def env(self, **over):
        e = super().env(RCLONE_STUB_DIR=self.rclone_dir, RCLONE_CALLS=self.rclone_calls_log,
                        HTTP_STUB_ROUTES=self.http_routes, HTTP_STUB_CALLS=self.http_calls_log,
                        PYTHONPATH=self.pyhook)
        e.update(over)
        return e

    def gemini_notes(self, *args, **env):
        """Runs `gemini-notes sync <gemini dir> --remote gdrive:`, capturing stdout and stderr separately."""
        argv = args or ("sync", self.gemini, "--remote", "gdrive:")
        return subprocess.run((GEMINI_NOTES,) + tuple(argv), env=self.env(**env),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    # --- fixtures ---------------------------------------------------------
    def _save_query(self):
        with open(os.path.join(self.rclone_dir, "query.json"), "w") as f:
            json.dump(self.query or None, f)   # rclone prints `null` for an empty result

    def add_doc(self, doc_id, name, body, created="2026-09-30T11:53:44.187Z",
                modified="2026-09-30T11:55:03.872Z"):
        """Adds (or replaces) one doc in the query result, with its Markdown export."""
        self.query = [d for d in self.query if d["id"] != doc_id]
        self.query.append({
            "createdTime": created, "id": doc_id,
            "mimeType": "application/vnd.google-apps.document", "modifiedTime": modified,
            "name": name, "size": str(len(body)),
            "webViewLink": "https://docs.google.com/document/d/%s/edit?usp=drivesdk" % doc_id})
        self._save_query()
        with open(os.path.join(self.rclone_dir, "docs", doc_id + ".md"), "w") as f:
            f.write(body)

    def route(self, method, url, body, status=200, page_token=None, timeout=False,
              incomplete_read=False, http_error_body_incomplete_read=False):
        key = "%s %s" % (method, url) + (" " + page_token if page_token else "")
        if timeout:
            self.routes[key] = {"timeout": True}
        elif incomplete_read:
            self.routes[key] = {"incomplete_read": True}
        elif http_error_body_incomplete_read:
            self.routes[key] = {"status": status, "http_error_body_incomplete_read": True}
        else:
            self.routes[key] = {"status": status, "body": body}
        with open(self.http_routes, "w") as f:
            json.dump(self.routes, f)

    # --- observations -----------------------------------------------------
    def rclone_calls(self):
        if not os.path.exists(self.rclone_calls_log):
            return []
        with open(self.rclone_calls_log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def http_calls(self):
        if not os.path.exists(self.http_calls_log):
            return []
        with open(self.http_calls_log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def gemini_files(self):
        if not os.path.isdir(self.gemini):
            return []
        return sorted(os.listdir(self.gemini))
