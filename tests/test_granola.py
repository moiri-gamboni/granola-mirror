"""granola — the mirror header carries the Google Calendar event id, the key that pairs a
Granola capture with the Gemini notes doc of the same meeting."""
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


if __name__ == "__main__":
    unittest.main()
