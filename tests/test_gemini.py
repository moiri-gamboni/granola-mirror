"""gemini-notes — the Google Meet "Notes by Gemini" mirror.

Drive is a stubbed rclone serving a query result and Markdown exports shaped like the real
ones (Quick notes, the notes tab with its `Attachments` calendar link, the transcript tab,
an inline base64 image); the Meet REST API and Google's token endpoint are a stubbed
urlopen. Every calendar eid is built here from placeholders, the way Google builds them:
unpadded base64 of "<event id> <organizer calendar>".
"""
import base64
import json
import os
import unittest
import urllib.parse

from _harness import GeminiSandbox

DOC = "1DocAAAAaaaa-bbbb_cccc"
DOC2 = "1DocBBBBaaaa-bbbb_cccc"
SUFFIX = " - Notes by Gemini"


def eid(text, alphabet="std", percent=False):
    e = base64.b64encode(text.encode()).decode().rstrip("=")
    if alphabet == "url":
        e = e.replace("+", "-").replace("/", "_")
    if percent:
        e = urllib.parse.quote(e, safe="")
    return e


def cal_link(e):
    return "https://calendar.google.com/calendar/event?eid=%s" % e


def export_md(title="Team sync", attach_eid=None, quick_eid=None, transcript=True, image=False):
    """A Markdown export of a notes doc with every tab, shaped like Google's."""
    lines = ["*Please **rate the new Quick notes tab** by taking a [short survey](https://example.com/survey).*",
             "", "## ", ""]
    if quick_eid:
        lines += ["Typed by an attendee: see [the invite](%s)" % cal_link(quick_eid), ""]
    lines += ["Sep 30, 2026", "", "## **%s**" % title, "",
              "Invited [Alice Example](mailto:alice@example.com) [Bob Example](mailto:bob@example.com)", ""]
    if attach_eid:
        lines += ["Attachments [%s](%s)" % (title, cal_link(attach_eid)), ""]
    lines += ["### **Summary**", "", "Alice and Bob agreed on the plan.", "",
              "### **Next steps**", "", "* [Bob Example] Send the draft.", ""]
    if image:
        lines += ["  ![][image1]", ""]
    if transcript:
        lines += ["## **%s \\- Transcript**" % title, "",
                  "### **00:00:05** {#00:00:05}", "",
                  "**Alice Example:** Hello Bob.", "",
                  "**Bob Example:** Hi Alice.", "",
                  "### **Transcription ended after 00:01:00**", ""]
    if image:
        lines += ["[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==>"]
    return "\n".join(lines) + "\n"


class Fetch(GeminiSandbox):
    def only_file(self):
        files = self.gemini_files()
        self.assertEqual(len(files), 1, files)
        return os.path.join(self.gemini, files[0])

    def test_a_doc_is_written_under_the_contract_header(self):
        self.add_doc(DOC, "Alice / Bob - 2026/09/30 13:30 CEST" + SUFFIX,
                     export_md(attach_eid=eid("evt123 alice@example.com")))
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.gemini_files(),
                         ["2026-09-30-alice-bob-2026-09-30-13-30-cest-gem_%s.md" % DOC])
        text = self.read(self.only_file())
        head = text.split("\n")[:8]
        self.assertEqual(head, [
            "<!-- gemini modified: 2026-09-30T11:55:03.872Z -->",
            "# Alice / Bob - 2026/09/30 13:30 CEST",
            "",
            "- **Date:** 2026-09-30",
            "- **Calendar event:** evt123",
            "- **Google Doc:** https://docs.google.com/document/d/%s/edit" % DOC,
            "",
            "*Please **rate the new Quick notes tab** by taking a [short survey](https://example.com/survey).*",
        ])
        self.assertIn("**Alice Example:** Hello Bob.", text)
        self.assertIn("gemini-notes: query returned 1 docs", r.stdout)
        self.assertIn("gemini-notes: exported %s (" % DOC, r.stdout)

    def test_eids_decode_in_every_encoding_google_uses(self):
        plain = "evt123 ~~~~?@example.com"          # its base64 holds both '+' and '/'
        self.assertTrue("+" in eid(plain) and "/" in eid(plain))
        cases = {
            "1DocStd": (eid(plain), "evt123"),
            "1DocUrl": (eid(plain, alphabet="url"), "evt123"),
            "1DocPct": (eid(plain, percent=True), "evt123"),
            "1DocRec": (eid("0series9abc_20260801T160000Z team@g"), "0series9abc_20260801T160000Z"),
        }
        for i, (doc, (e, _)) in enumerate(sorted(cases.items())):
            self.add_doc(doc, "Meeting %d%s" % (i, SUFFIX), export_md(attach_eid=e))
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for doc, (e, want) in cases.items():
            with self.subTest(doc=doc, eid=e):
                [name] = [f for f in self.gemini_files() if f.endswith("-gem_%s.md" % doc)]
                self.assertIn("\n- **Calendar event:** %s\n" % want,
                              self.read(os.path.join(self.gemini, name)))

    def test_only_the_attachments_line_names_the_event(self):
        self.add_doc(DOC, "Sync" + SUFFIX, export_md(attach_eid=eid("realevt alice@example.com"),
                                                      quick_eid=eid("pasted bob@example.com")))
        self.gemini_notes()
        text = self.read(self.only_file())
        self.assertIn("- **Calendar event:** realevt\n", text)
        self.assertNotIn("**Calendar event:** pasted", text)

    def test_a_doc_without_an_attachments_link_has_no_event_line(self):
        self.add_doc(DOC, "Ad hoc" + SUFFIX, export_md(quick_eid=eid("pasted bob@example.com")))
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Calendar event", self.read(self.only_file()))

    def test_an_unchanged_doc_is_not_exported_again(self):
        self.add_doc(DOC, "Sync" + SUFFIX, export_md())
        self.gemini_notes()
        path = self.only_file()
        before = (self.read(path), os.stat(path).st_mtime_ns)
        exports = len([c for c in self.rclone_calls() if c[:2] == ["backend", "copyid"]])
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len([c for c in self.rclone_calls() if c[:2] == ["backend", "copyid"]]), exports)
        self.assertEqual((self.read(path), os.stat(path).st_mtime_ns), before)

    def test_a_changed_renamed_doc_rewrites_its_own_file(self):
        self.add_doc(DOC, "Old title" + SUFFIX, export_md(title="Old title"))
        self.gemini_notes()
        path = self.only_file()
        self.add_doc(DOC, "New title" + SUFFIX, export_md(title="New title"),
                     modified="2026-10-01T09:00:00.000Z")
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.only_file(), path, "the file keeps its name for life")
        text = self.read(path)
        self.assertTrue(text.startswith("<!-- gemini modified: 2026-10-01T09:00:00.000Z -->\n# New title\n"), text)
        self.assertIn("## **New title \\- Transcript**", text)

    def test_an_inline_image_line_becomes_a_placeholder(self):
        self.add_doc(DOC, "Sync" + SUFFIX, export_md(image=True))
        self.gemini_notes()
        text = self.read(self.only_file())
        self.assertNotIn("base64", text)
        self.assertIn("\n[inline image omitted; see the Google Doc]\n", text)
        self.assertIn("  ![][image1]", text, "only the data: line is replaced")

    def test_one_failed_export_leaves_the_others_written(self):
        self.add_doc(DOC, "Good" + SUFFIX, export_md())
        self.add_doc(DOC2, "Bad" + SUFFIX, export_md())
        r = self.gemini_notes(RCLONE_FAIL_IDS=DOC2)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.gemini_files(), ["2026-09-30-good-gem_%s.md" % DOC])
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("gemini-notes: export failed %s: " % DOC2), lines)
        self.assertIn("File not found", lines[0])

    def test_a_failed_query_writes_nothing(self):
        self.add_doc(DOC, "Sync" + SUFFIX, export_md())
        r = self.gemini_notes(RCLONE_QUERY_ERR="2026/09/30 NOTICE: Failed to backend: token expired")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "gemini-notes: query failed: 2026/09/30 NOTICE: Failed to backend: token expired\n")
        self.assertEqual(self.gemini_files(), [])
        self.assertEqual(self.http_calls(), [], "no Meet step after a failed query")

    def test_an_empty_query_result_is_a_clean_run(self):
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gemini-notes: query returned 0 docs", r.stdout)


REC = "conferenceRecords/rec1"
TRANSCRIPT = REC + "/transcripts/t1"
ALICE = REC + "/participants/111"
BOB = REC + "/participants/222"
GUEST = REC + "/participants/333"


def entry(participant, start, text):
    return {"name": TRANSCRIPT + "/entries/e" + start.replace(":", ""), "participant": participant,
            "text": text, "languageCode": "en-US",
            "startTime": "2026-09-30T%sZ" % start, "endTime": "2026-09-30T%sZ" % start}


class Meet(GeminiSandbox):
    """The structured transcript of each conference the account joined, appended to the
    mirror file of the doc the transcript names."""

    def setUp(self):
        super().setUp()
        self.add_doc(DOC, "Sync" + SUFFIX, export_md())
        self.path = os.path.join(self.gemini, "2026-09-30-sync-gem_%s.md" % DOC)
        self.route("GET", self.MEET + "/conferenceRecords", {"conferenceRecords": [{
            "name": REC, "startTime": "2026-09-30T10:00:00.250000Z",
            "endTime": "2026-09-30T11:20:00Z", "expireTime": "2026-10-30T11:20:00Z",
            "space": "spaces/space1"}]})
        self.route("GET", self.MEET + "/" + REC + "/transcripts", {"transcripts": [{
            "name": TRANSCRIPT, "state": "FILE_GENERATED",
            "startTime": "2026-09-30T10:00:10Z", "endTime": "2026-09-30T11:19:00Z",
            "docsDestination": {"document": DOC,
                                "exportUri": "https://docs.google.com/document/d/%s/edit?usp=drive_web" % DOC}}]})
        self.route("GET", self.MEET + "/" + TRANSCRIPT + "/entries", {
            "transcriptEntries": [entry(ALICE, "10:01:40.100", "Shall we start?"),
                                  entry(BOB, "10:01:58.900", "Yes, the draft is ready.")],
            "nextPageToken": "page2"})
        self.route("GET", self.MEET + "/" + TRANSCRIPT + "/entries", {
            "transcriptEntries": [entry(GUEST, "11:14:23.700", "Joining by phone.")]},
            page_token="page2")
        self.route("GET", self.MEET + "/" + REC + "/participants", {
            "participants": [{"name": ALICE, "signedinUser": {"user": "users/111", "displayName": "Alice Example"},
                              "earliestStartTime": "2026-09-30T10:00:05Z", "latestEndTime": "2026-09-30T11:19:30Z"}],
            "nextPageToken": "ppage2"})
        self.route("GET", self.MEET + "/" + REC + "/participants", {
            "participants": [{"name": BOB, "signedinUser": {"user": "users/222", "displayName": "Bob Example"}},
                             {"name": GUEST, "phoneUser": {"displayName": "Caller 1"}}]},
            page_token="ppage2")

    def entries_calls(self):
        return [c for c in self.http_calls() if "/entries" in c["url"]]

    def test_the_transcript_is_appended_across_pages_with_participant_names(self):
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "")
        text = self.read(self.path)
        section = text[text.index("\n## Meet transcript\n"):]
        self.assertEqual(section, "\n## Meet transcript\n\n"
                                  "<!-- meet transcript: %s -->\n\n" % TRANSCRIPT +
                         "[00:01:39] **Alice Example:** Shall we start?\n"
                         "[00:01:58] **Bob Example:** Yes, the draft is ready.\n"
                         "[01:14:23] **Caller 1:** Joining by phone.\n")
        self.assertIn("**Alice Example:** Hello Bob.", text, "the doc export stays above it")

    def test_the_token_is_refreshed_from_rclones_config_and_used(self):
        self.gemini_notes()
        calls = self.http_calls()
        token = [c for c in calls if c["url"] == self.TOKEN_URL]
        self.assertEqual(len(token), 1)
        self.assertEqual(dict(urllib.parse.parse_qsl(token[0]["body"])), {
            "grant_type": "refresh_token", "refresh_token": "refresh-123",
            "client_id": "client-abc", "client_secret": "secret-xyz"})
        meet = [c for c in calls if c["url"].startswith(self.MEET)]
        self.assertTrue(meet)
        for c in meet:
            self.assertEqual(c["method"], "GET")
            self.assertEqual(c["headers"].get("Authorization"), "Bearer fresh-access")
        self.assertFalse(any("revoke" in c["url"] for c in calls))
        self.assertEqual({tuple(c[:2]) for c in self.rclone_calls()},
                         {("backend", "query"), ("backend", "copyid"), ("config", "dump")},
                         "rclone's config is only read")

    def test_a_mirrored_transcript_is_not_fetched_again(self):
        self.gemini_notes()
        before = (self.read(self.path), len(self.entries_calls()))
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.read(self.path), len(self.entries_calls())), before)

    def test_a_reexported_doc_keeps_its_meet_section_verbatim(self):
        self.gemini_notes()
        section = self.read(self.path)[self.read(self.path).index("\n## Meet transcript\n"):]
        self.add_doc(DOC, "Sync" + SUFFIX, export_md(title="Edited title"),
                     modified="2026-10-01T09:00:00.000Z")
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.read(self.path)
        self.assertIn("## **Edited title \\- Transcript**", text, "the doc was re-exported")
        self.assertTrue(text.endswith(section), text)
        self.assertEqual(text.count("## Meet transcript"), 1)
        self.assertEqual(len(self.entries_calls()), 2, "entries fetched once (two pages)")

    def test_a_transcript_whose_doc_is_not_mirrored_is_skipped_and_counted(self):
        self.route("GET", self.MEET + "/" + REC + "/transcripts", {"transcripts": [{
            "name": TRANSCRIPT, "state": "FILE_GENERATED", "startTime": "2026-09-30T10:00:10Z",
            "docsDestination": {"document": "1NotShared"}}]})
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.entries_calls(), [])
        self.assertNotIn("Meet transcript", self.read(self.path))
        self.assertIn("1 skipped (doc not mirrored)", r.stdout)

    def test_a_transcript_still_being_generated_waits(self):
        self.route("GET", self.MEET + "/" + REC + "/transcripts", {"transcripts": [{
            "name": TRANSCRIPT, "state": "ENDED", "startTime": "2026-09-30T10:00:10Z",
            "docsDestination": {"document": DOC}}]})
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.entries_calls(), [])
        self.assertNotIn("Meet transcript", self.read(self.path))

    def test_a_transcript_with_no_entries_is_marked_without_lines(self):
        self.route("GET", self.MEET + "/" + TRANSCRIPT + "/entries", {})
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.read(self.path)
        self.assertTrue(text.endswith("\n## Meet transcript\n\n<!-- meet transcript: %s -->\n" % TRANSCRIPT), text)
        self.gemini_notes()
        self.assertEqual(len(self.entries_calls()), 1, "an empty transcript is not fetched again")

    def test_a_failed_token_refresh_keeps_the_docs_and_exits_2(self):
        self.route("POST", self.TOKEN_URL, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
                   status=400)
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.path))
        [line] = r.stderr.splitlines()
        self.assertTrue(line.startswith("gemini-notes: meet failed: "), line)
        self.assertIn("invalid_grant", line)
        self.assertEqual([c for c in self.http_calls() if c["url"].startswith(self.MEET)], [])

    def test_a_failed_conference_list_keeps_the_docs_and_exits_2(self):
        self.route("GET", self.MEET + "/conferenceRecords", {"error": {"code": 403, "status": "PERMISSION_DENIED"}},
                   status=403)
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.path))
        [line] = r.stderr.splitlines()
        self.assertTrue(line.startswith("gemini-notes: meet failed: "), line)
        self.assertIn("403", line)

    def test_a_failed_conference_names_it_and_leaves_the_others(self):
        self.route("GET", self.MEET + "/conferenceRecords", {"conferenceRecords": [
            {"name": REC, "startTime": "2026-09-30T10:00:00.250000Z"},
            {"name": "conferenceRecords/rec2", "startTime": "2026-09-29T10:00:00Z"}]})
        self.route("GET", self.MEET + "/conferenceRecords/rec2/transcripts",
                   {"error": {"code": 403}}, status=403)
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        [line] = r.stderr.splitlines()
        self.assertTrue(line.startswith("gemini-notes: meet failed rec2: "), line)
        self.assertIn("## Meet transcript", self.read(self.path), "rec1 still appended")

    def test_a_doc_failure_and_a_meet_failure_both_report(self):
        self.add_doc(DOC2, "Other" + SUFFIX, export_md())
        self.route("POST", self.TOKEN_URL, {"error": "invalid_grant"}, status=400)
        r = self.gemini_notes(RCLONE_FAIL_IDS=DOC2)
        self.assertEqual(r.returncode, 2)
        self.assertEqual([l.split(":")[1].strip() for l in r.stderr.splitlines()],
                         ["export failed %s" % DOC2, "meet failed"])

    def test_a_timed_out_conference_is_reported_as_a_meet_failure(self):
        self.route("GET", self.MEET + "/" + REC + "/participants", None, timeout=True)
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        [line] = r.stderr.splitlines()
        self.assertTrue(line.startswith("gemini-notes: meet failed rec1: "), line)
        self.assertIn("timed out", line)
        self.assertTrue(os.path.exists(self.path))

    def test_two_transcripts_of_one_doc_are_appended_in_start_order(self):
        later = TRANSCRIPT[:-2] + "t2"
        self.route("GET", self.MEET + "/" + REC + "/transcripts", {"transcripts": [
            {"name": later, "state": "FILE_GENERATED", "startTime": "2026-09-30T10:40:00Z",
             "docsDestination": {"document": DOC}},
            {"name": TRANSCRIPT, "state": "FILE_GENERATED", "startTime": "2026-09-30T10:00:10Z",
             "docsDestination": {"document": DOC}}]})
        self.route("GET", self.MEET + "/" + later + "/entries",
                   {"transcriptEntries": [entry(BOB, "10:40:05.000", "Back again.")]})
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.read(self.path)
        first, second = (text.index("<!-- meet transcript: %s -->" % t) for t in (TRANSCRIPT, later))
        self.assertLess(first, second)
        self.assertTrue(text.endswith("[00:40:04] **Bob Example:** Back again.\n"), text)

    def test_the_harness_gemini_file_matches_a_written_meet_section(self):
        self.gemini_notes()
        written = self.read(self.path)
        helper = self.read(self.gemini_file("2026-09-30-other-gem_1DocOther.md", meet_lines=[
            "[00:01:39] **Alice Example:** Shall we start?",
            "[00:01:58] **Bob Example:** Yes, the draft is ready.",
            "[01:14:23] **Caller 1:** Joining by phone."]))
        section = lambda t: t[t.index("\n## Meet transcript\n") - 1:]
        self.assertEqual(section(helper), section(written))


class Harness(GeminiSandbox):
    def test_gemini_file_writes_what_the_fetcher_writes(self):
        """Other suites build Gemini mirror files with gemini_file(); it must not drift from
        the fetcher's own output."""
        path = self.gemini_file("2026-09-30-team-sync-gem_%s.md" % DOC, event=None)
        want = self.read(path)
        os.remove(path)
        self.add_doc(DOC, "Team sync" + SUFFIX, want.split("/edit\n\n", 1)[1],
                     created="2026-09-30T09:00:00.000Z", modified="2026-09-30T12:00:00.000Z")
        r = self.gemini_notes()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(path), want)


class Cli(GeminiSandbox):
    def test_help_prints_usage(self):
        r = self.gemini_notes("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gemini-notes sync DIR --remote REMOTE", r.stdout)


if __name__ == "__main__":
    unittest.main()
