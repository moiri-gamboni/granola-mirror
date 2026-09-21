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
case "${CLAUDE_STUB_MODE:-ok}" in
  fail)    printf 'boom: %s\n' "${CLAUDE_STUB_ERR:-stub failure}" >&2; exit 1 ;;
  empty)   exit 0 ;;
  nonhash) printf 'not a heading\n'; exit 0 ;;
esac
if printf '%s' "$input" | grep -q 'GRANOLA_FAIL_TOKEN'; then
  printf 'boom: this meeting is wedged\n' >&2; exit 1
fi
printf '# Meeting note\n\nBody paragraph.\n'
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
DIGEST_STUB = '#!/usr/bin/env bash\nexit "${DIGEST_RC:-0}"\n'

CURL_STUB = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SANDBOX_NTFY_LOG"
exit 0
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
        self.ntfy_log = os.path.join(self.tmp, "ntfy")

    # --- env / invocation -------------------------------------------------
    def env(self, **over):
        e = dict(os.environ,
                 HOME=self.home,
                 PATH=self.bin + os.pathsep + os.environ["PATH"],
                 CLAUDE_CALLS=self.claude_calls,
                 CLAUDE_ARGV=self.claude_argv,
                 SANDBOX_NTFY_LOG=self.ntfy_log)
        e.pop("GRANOLA_LOCK_HELD", None)
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
        self.refresh = os.path.join(clone, "refresh.sh")
        return self.refresh

    def enable_digest_stub(self):
        write_exec(os.path.join(self.bin, "granola-digest"), DIGEST_STUB)

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
                    header_line=True):
        """Write one mirror .md. tmark '__match__' stamps the transcript with `updated`
        (coherent); None omits the marker (grandfathered); any string stamps that literal.
        summary=None uses granola's no-summary placeholder (unsettled). transcript=None omits
        the section. header_line=False drops the `granola updated_at` comment (unstamped)."""
        path = os.path.join(self.mirror, name)
        lines = []
        if header_line:
            lines.append("<!-- granola updated_at: %s -->" % updated)
        lines += ["# %s" % name.rsplit(".", 1)[0], "",
                  "- **Date:** %s" % updated[:10], "",
                  summary if summary is not None else "_(no summary)_"]
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
