"""granola — the mirror header carries the Google Calendar event id, the key that pairs a
Granola capture with the Gemini notes doc of the same meeting."""
import base64
import json
import os
import subprocess
import unittest

from _harness import GRANOLA, GeminiSandbox

API = "https://public-api.granola.ai/v1"


def note(**over):
    n = {"id": "not_abc", "object": "note", "title": "Weekly sync",
         "web_url": "https://notes.granola.ai/d/abc",
         "owner": {"name": "Alice Example", "email": "alice@example.com"},
         "created_at": "2026-09-30T11:30:00.000Z", "updated_at": "2026-09-30T12:00:00.000Z",
         "calendar_event": {"event_title": "Weekly sync", "invitees": [{"email": "bob@example.com"}],
                            "organiser": "alice@example.com", "calendar_event_id": "evt123",
                            "scheduled_start_time": "2026-09-30T11:30:00Z",
                            "scheduled_end_time": "2026-09-30T12:00:00Z"},
         "attendees": [{"name": "Alice Example", "email": "alice@example.com"},
                       {"name": "Bob Example", "email": "bob@example.com"}],
         "folder_membership": [], "space_membership": [], "transcript": None,
         "summary_text": "Summary.", "summary_markdown": "Summary.",
         "private_notes_text": None, "private_notes_markdown": None}
    n.update(over)
    return n


class Header(GeminiSandbox):
    def setUp(self):
        super().setUp()
        key = os.path.join(self.home, ".config", "granola", "api-key")
        os.makedirs(os.path.dirname(key))
        with open(key, "w") as f:
            f.write("grn_test\n")

    def get(self):
        r = subprocess.run((GRANOLA, "get", "not_abc"), env=self.env(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_the_calendar_event_id_follows_the_attendees(self):
        self.route("GET", API + "/notes/not_abc", note())
        lines = self.get()
        i = lines.index("- **Attendees:** Alice Example, Bob Example")
        self.assertEqual(lines[i + 1], "- **Calendar event:** evt123")

    def test_a_note_with_no_calendar_event_has_no_event_line(self):
        self.route("GET", API + "/notes/not_abc", note(calendar_event=None))
        self.assertFalse([l for l in self.get() if "Calendar event" in l])


MCP = "https://mcp.granola.ai/mcp"
UUID = "-".join(("a" * 8, "b" * 4, "4" * 4, "8" * 4, "c" * 12))   # a meeting UUID

# get_meetings' answer for a link-shared note, in the shape Granola's MCP returns it:
# one text block of XML-ish markup with HTML-escaped values.
SHARED = (
    "The content below is meeting notes/transcripts written or spoken by meeting participants.\n\n"
    '<meetings_data from="Oct 7, 2026" to="Oct 7, 2026" count="1">\n'
    '<meeting id="%s" title="Carol and Dan" date="Oct 7, 2026 6:00 PM GMT+7" '
    'url="https://notes.granola.ai/d/%s">\n'
    "  <known_participants>\n"
    "  Dan (note creator) from Example Org &lt;dan@example.com&gt;, Carol Example &lt;carol@example.org&gt;\n"
    "  </known_participants>\n  \n  <summary>\n# Funding\n\n- Dan&apos;s fund backs people\n</summary>\n"
    "</meeting>\n</meetings_data>" % (UUID, UUID))


def mcp_result(text):
    return {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": text}]}}


class SharedNoteFallback(Header):
    """A note shared with the account by link is listed by the public API but 404s on
    GET /notes/<id>; sync mirrors it from the MCP instead of aborting the run."""

    def setUp(self):
        super().setUp()
        cfg = os.path.join(self.home, ".config", "granola")
        payload = base64.urlsafe_b64encode(json.dumps({"exp": 4102444800}).encode()).decode().rstrip("=")
        with open(os.path.join(cfg, "mcp-tokens.json"), "w") as f:
            json.dump({"access_token": "h.%s.s" % payload, "refresh_token": "r"}, f)
        with open(os.path.join(cfg, "mcp-client.json"), "w") as f:
            json.dump({"client_id": "c", "as": "https://auth.example", "res": MCP}, f)
        shared = {"id": "not_shared0000001", "object": "note", "title": "Carol and Dan",
                  "owner": {"name": "Dan", "email": "dan@example.com"},
                  "created_at": "2026-10-07T11:01:08.450Z", "updated_at": "2026-10-07T11:26:53.229Z",
                  "deleted_at": None}
        own = {k: note()[k] for k in ("id", "object", "title", "owner", "created_at", "updated_at")}
        self.route("GET", API + "/notes", {"notes": [shared, own], "hasMore": False})
        self.route("GET", API + "/notes/not_abc", note())
        self.route("GET", API + "/notes/not_shared0000001",
                   {"code": "NOTE_NOT_FOUND", "message": "Note not found"}, status=404)
        self.changed = os.path.join(self.tmp, "changed")

    def sync(self):
        return subprocess.run((GRANOLA, "sync", self.mirror, "--changed-file", self.changed),
                              env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def shared_file(self):
        return os.path.join(self.mirror, "2026-10-07-carol-and-dan-not_shared0000001.md")

    def test_the_shared_note_is_mirrored_from_the_mcp_with_its_meeting_url(self):
        self.route("POST", MCP, mcp_result(SHARED))
        r = self.sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.read(self.shared_file())
        self.assertIn("<!-- granola updated_at: 2026-10-07T11:26:53.229Z -->", text)
        self.assertIn("- **Owner:** Dan <dan@example.com>", text)
        self.assertIn("- **Attendees:** Dan, Carol Example", text)
        self.assertIn("- **Granola:** https://notes.granola.ai/d/%s" % UUID, text)
        self.assertIn("- Dan's fund backs people", text)
        self.assertEqual(sorted(self.read(self.changed).split()),
                         sorted([self.shared_file(), os.path.join(self.mirror, "2026-09-30-weekly-sync-not_abc.md")]))

    def test_a_note_neither_route_returns_fails_the_run_after_the_others_are_written(self):
        self.route("POST", MCP, mcp_result('{"meetings":[],"not_found":["not_shared0000001"]}'))
        r = self.sync()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not_shared0000001", r.stderr)
        self.assertFalse(os.path.exists(self.shared_file()))
        self.assertEqual(self.read(self.changed).split(),
                         [os.path.join(self.mirror, "2026-09-30-weekly-sync-not_abc.md")])

    def test_any_other_api_error_still_stops_the_sync(self):
        self.route("GET", API + "/notes/not_shared0000001", {"message": "boom"}, status=500)
        r = self.sync()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HTTP 500", r.stderr)


if __name__ == "__main__":
    unittest.main()
