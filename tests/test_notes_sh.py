"""notes.sh — the version-addressed note model and its tool-less call.

The mirror file carries the source version (`granola updated_at`); the note's banner records
the version it was generated from plus a hash of its own body. Every decision — regenerate,
skip as current, hold a hand edit, refuse an incoherent pair — is a comparison of those
recorded values, never an mtime. Fail-closed: any value that won't parse resolves to *held*,
so an unreadable banner is never silently overwritten.
"""
import os
import re
import subprocess
import unittest

from _harness import GranolaSandbox

LOW = ("--since", "2000-01-01")   # include the dated fixtures; the real floor is tested separately


class Generation(GranolaSandbox):
    def test_a_coherent_settled_transcript_is_generated(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        self.assertTrue(os.path.exists(self.note_path("2026-08-19-standup-not_aaa.md")))
        self.assertEqual(self.run_summary()["written"], 1)

    def test_a_freshly_written_note_reads_back_as_current(self):
        """The writer/reader hash round-trip: generate, then a second run must skip it as
        current (banner version equal AND body hash matches) with no model call."""
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        self.notes_sh(self.mirror, *LOW)
        before = self.claude_call_count()
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), before, "current note must not regenerate")
        self.assertEqual(self.run_summary()["current"], 1, r.stdout)

    def test_a_source_version_bump_regenerates(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md", updated="2026-08-19T10:00:00Z")
        self.notes_sh(self.mirror, *LOW)
        self.mirror_file("2026-08-19-standup-not_aaa.md", updated="2026-08-19T12:00:00Z")
        before = self.claude_call_count()
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), before + 1, r.stdout)
        self.assertEqual(self.run_summary()["written"], 1)

    def test_a_missing_source_version_is_never_written_as_a_broken_note(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md", header_line=False)
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), 0, r.stdout)
        self.assertFalse(os.path.exists(self.note_path("2026-08-19-standup-not_aaa.md")))
        self.assertEqual(self.run_summary()["unstamped"], 1)


class Gates(GranolaSandbox):
    def test_an_unsettled_meeting_is_not_generated(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md", summary=None)
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), 0, r.stdout)
        self.assertEqual(self.run_summary()["settling"], 1)

    def test_a_summary_placeholder_inside_the_transcript_still_counts_as_settled(self):
        """The settle check reads only the pre-`## Transcript` portion, so a speaker
        literally saying '_(no summary)_' does not freeze the note as settling forever."""
        self.mirror_file("2026-08-19-standup-not_aaa.md", summary="Real summary here.",
                         transcript="Alice: I wrote _(no summary)_ in the doc.  Bob: ok.")
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        self.assertEqual(self.run_summary()["written"], 1)

    def test_a_transcript_that_has_not_arrived_yet_is_awaited(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md", transcript=None)
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), 0, r.stdout)
        self.assertEqual(self.run_summary()["missing"], 1)


class Coherence(GranolaSandbox):
    def test_an_incoherent_pair_is_held_never_generated(self):
        """Transcript stamped against an older note version than the header carries: the
        pipeline must not fabricate a note from a mismatched summary/transcript pair."""
        self.mirror_file("2026-08-19-standup-not_aaa.md", updated="2026-08-19T12:00:00Z",
                         tmark="2026-08-19T09:00:00Z")
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), 0, r.stdout)
        self.assertFalse(os.path.exists(self.note_path("2026-08-19-standup-not_aaa.md")))
        self.assertEqual(self.run_summary()["held_incoherent"], 1)

    def test_a_grandfathered_transcript_with_no_marker_is_treated_as_coherent(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md", tmark=None)
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        self.assertEqual(self.run_summary()["written"], 1)


class HandEditGuard(GranolaSandbox):
    def _generate_then_edit(self, name="2026-08-19-standup-not_aaa.md"):
        self.mirror_file(name)
        self.notes_sh(self.mirror, *LOW)
        note = self.note_path(name)
        with open(note, "a") as f:      # a hand edit that changes the body hash
            f.write("\nAn analyst's correction.\n")
        return name, note

    def test_a_hand_edited_note_is_held_not_clobbered(self):
        name, _ = self._generate_then_edit()
        before = self.claude_call_count()
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), before, "a hand edit must not be overwritten")
        self.assertEqual(self.run_summary()["held_edited"], 1, r.stdout)
        self.assertFalse(self.held_marker(name), "hash-mismatch is a counter, not the alarm class")

    def test_force_overrides_a_hand_edit(self):
        name, note = self._generate_then_edit()
        before = self.claude_call_count()
        r = self.notes_sh(self.mirror, "--force", *LOW)
        self.assertEqual(self.claude_call_count(), before + 1, r.stdout)
        # after --force the note is machine-written again -> reads current, marker cleared
        self.assertFalse(self.held_marker(name))
        r2 = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.run_summary()["current"], 1, r2.stdout)

    def test_a_banner_absent_note_is_held_and_marked_for_the_alarm(self):
        """The banner-absent / unparseable class is the one refresh.sh pages on — a note it
        cannot verify at all (a pre-migration note, or a wiped banner)."""
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name)
        self.write_note(name, banner="<!-- auto-generated 2026-08-19 by notes.sh -->",
                        body="# Old\n\nBody without a version banner.\n")
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), 0, r.stdout)
        self.assertEqual(self.run_summary()["held_unparseable"], 1)
        self.assertTrue(self.held_marker(name), "banner-absent hold must arm the alarm")


class FileArgs(GranolaSandbox):
    def test_an_explicit_file_bypasses_the_settle_gate(self):
        p = self.mirror_file("2026-08-19-standup-not_aaa.md", summary=None)  # unsettled
        r = self.notes_sh(self.mirror, p)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)

    def test_an_explicit_file_still_honors_a_hand_edit_hold(self):
        name = "2026-08-19-standup-not_aaa.md"
        p = self.mirror_file(name)
        self.notes_sh(self.mirror, p)
        with open(self.note_path(name), "a") as f:
            f.write("\nEdit.\n")
        before = self.claude_call_count()
        r = self.notes_sh(self.mirror, p)      # named explicitly, but no --force
        self.assertEqual(self.claude_call_count(), before, "FILE arg must not clobber a hand edit")
        self.assertEqual(self.run_summary()["held_edited"], 1, r.stdout)


class HashMode(GranolaSandbox):
    def test_hash_mode_strips_banner_and_blank_then_hashes(self):
        """The one shared body-hash the banner migration also calls. A note and a bare body
        with identical post-banner bytes must hash identically."""
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name)
        self.notes_sh(self.mirror, *LOW)
        note = self.note_path(name)
        h = self.hash_note(note)
        self.assertRegex(h, r"^[0-9a-f]{64}$")
        # sed '1,2d' equivalent: everything after banner + blank hashes to the same value.
        import hashlib
        body = "".join(self.read(note).splitlines(keepends=True)[2:])
        self.assertEqual(h, hashlib.sha256(body.encode()).hexdigest())


class WedgeLedger(GranolaSandbox):
    def test_a_failed_generation_increments_the_wedge_ledger_and_success_clears_it(self):
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name, updated="2026-08-19T10:00:00Z")
        self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_MODE="fail", CLAUDE_STUB_ERR="rate limit")
        w = self.wedge(name)
        self.assertIsNotNone(w, "a failed generation must record a wedge entry")
        self.assertEqual(w["streak"], 1)
        self.assertEqual(w["failed_version"], "2026-08-19T10:00:00Z")
        self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_MODE="fail")
        self.assertEqual(self.wedge(name)["streak"], 2)
        self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_MODE="ok")
        self.assertIsNone(self.wedge(name), "a successful write must clear the wedge ledger")

    def test_the_first_failure_stderr_is_captured_in_the_run_summary(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_MODE="fail", CLAUDE_STUB_ERR="OAuth expired")
        s = self.run_summary()
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["attempted"], 1)
        self.assertIn("OAuth expired", s["first_error"])


class GlossaryProposals(GranolaSandbox):
    """Each generated note carries its meeting's glossary proposals after a marker line;
    notes.sh strips them from the note and appends them to the auto tier."""
    NAME = "2026-08-19-standup-not_aaa.md"

    def auto_tier(self):
        path = os.path.join(self.wf, "transcript-corrections-auto.md")
        with open(path, "w") as f:
            f.write("# auto tier\n")
        return path

    def test_proposals_are_appended_to_the_auto_tier_and_kept_out_of_the_note(self):
        auto = self.auto_tier()
        row = "- Alice (ops lead) | garble seen: \"Alyss\" | High | 2026-08-19 standup"
        self.mirror_file(self.NAME)
        r = self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_GLOSSARY=row)
        self.assertEqual(r.returncode, 0, r.stdout)
        tier = self.read(auto)
        self.assertRegex(tier, r"\n## \d{4}-\d{2}-\d{2} — 2026-08-19-standup-not_aaa\n")
        self.assertIn(row, tier)
        note = self.read(self.note_path(self.NAME))
        self.assertIn("Body paragraph.", note)
        self.assertNotIn("glossary-additions", note)
        self.assertNotIn("Alyss", note)
        # The stored body hash covers the note without its tail, so it reads back current.
        self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.run_summary()["current"], 1)

    def test_none_appends_nothing(self):
        auto = self.auto_tier()
        self.mirror_file(self.NAME)
        self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.read(auto), "# auto tier\n")

    ROW = "- Alice (ops lead) | garble seen: \"Alyss\" | Med | 2026-08-01 sync"

    def tiers(self):
        auto = os.path.join(self.wf, "transcript-corrections-auto.md")
        with open(auto, "w") as f:
            f.write("# auto tier\n\n## 2026-08-01 — sync\n\n%s\n- Bob | \"Rob\" | Low | x\n" % self.ROW)
        reviewed = os.path.join(self.wf, "transcript-corrections.md")
        with open(reviewed, "w") as f:
            f.write("# reviewed tier\n\n## People\n| a | b |\n")
        return auto, reviewed

    def test_promote_moves_the_exact_row_to_the_reviewed_tier(self):
        auto, reviewed = self.tiers()
        self.mirror_file(self.NAME)
        r = self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_GLOSSARY="promote: " + self.ROW)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn(self.ROW, self.read(auto))
        self.assertIn("- Bob", self.read(auto))
        rev = self.read(reviewed)
        self.assertIn("## Promoted from the auto tier", rev)
        self.assertRegex(rev, re.escape(self.ROW) + r" · promoted \d{4}-\d{2}-\d{2}, backed by "
                         + re.escape("2026-08-19-standup-not_aaa"))
        # A second promotion appends under the same section rather than a new one.
        self.mirror_file("2026-08-20-standup-not_bbb.md")
        self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_GLOSSARY="promote: - Bob | \"Rob\" | Low | x")
        self.assertEqual(self.read(reviewed).count("## Promoted from the auto tier"), 1)

    def test_drop_removes_the_exact_row(self):
        auto, reviewed = self.tiers()
        before = self.read(reviewed)
        self.mirror_file(self.NAME)
        r = self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_GLOSSARY="drop: " + self.ROW)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn(self.ROW, self.read(auto))
        self.assertEqual(self.read(reviewed), before)

    def test_a_target_that_is_not_an_exact_row_changes_nothing(self):
        auto, reviewed = self.tiers()
        a0, r0 = self.read(auto), self.read(reviewed)
        self.mirror_file(self.NAME)
        r = self.notes_sh(self.mirror, *LOW,
                          CLAUDE_STUB_GLOSSARY="promote: - Alice (ops lead)\ndrop: - Carol | x")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.read(auto), self.read(reviewed)), (a0, r0))
        self.assertIn("not found exactly once", r.stdout)
        self.assertEqual(self.run_summary()["glossary_misses"], 2)

    def test_none_variants_append_nothing(self):
        auto = self.auto_tier()
        self.mirror_file(self.NAME)
        self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_GLOSSARY="- none\n**None**\nnone ")
        self.assertEqual(self.read(auto), "# auto tier\n")

    def test_a_heading_after_the_marker_fails_the_generation(self):
        """Note content placed after the marker would otherwise leave the note and land
        in the auto tier with nothing alarming."""
        auto = self.auto_tier()
        self.mirror_file(self.NAME)
        tail = "- a | b | High | m\n\n## Sources & reliability\n- Open: who is Alyss? (Low)"
        r = self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_GLOSSARY=tail)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.note_path(self.NAME)))
        self.assertEqual(self.read(auto), "# auto tier\n")
        self.assertIn("heading", self.run_summary()["first_error"])

    def test_two_markers_fail_the_generation(self):
        self.auto_tier()
        self.mirror_file(self.NAME)
        r = self.notes_sh(self.mirror, *LOW,
                          CLAUDE_STUB_GLOSSARY="none\n<!-- glossary-additions -->\nnone")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.note_path(self.NAME)))

    def test_an_unwritable_auto_tier_fails_loudly(self):
        auto = self.auto_tier()
        os.chmod(auto, 0o444)
        self.addCleanup(os.chmod, auto, 0o644)
        self.mirror_file(self.NAME)
        r = self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_GLOSSARY="- a | b | High | m")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("appended", r.stdout)
        self.assertIn("glossary", self.run_summary()["first_error"])

    def test_a_missing_marker_fails_the_generation(self):
        self.auto_tier()
        self.mirror_file(self.NAME)
        r = self.notes_sh(self.mirror, *LOW, CLAUDE_STUB_MODE="nomarker")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.note_path(self.NAME)))
        self.assertEqual(self.run_summary()["failed"], 1)


class ToolDenial(GranolaSandbox):
    def test_the_generation_call_runs_tool_less(self):
        """The claude -p transform must carry BOTH --tools "" and --strict-mcp-config: the
        first disables the built-ins, the second keeps the user's MCP servers from loading.
        --tools "" alone leaves MCP tools reachable, so both are required (static argv check;
        the model's actual compliance is a one-time manual build check, not a paid live probe)."""
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        self.notes_sh(self.mirror, *LOW)
        argv = self.last_claude_argv()
        self.assertIsNotNone(argv, "the generation call was never made")
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "", "--tools value must be empty")
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--mcp-config", argv, "no MCP config may be passed")


class WorkspaceLayout(GranolaSandbox):
    def test_a_mirror_inside_a_git_workspace_resolves_notes_at_the_toplevel(self):
        """A mirror nested below the workspace root (e.g. <ws>/meetings/granola) resolves
        the workspace as the git toplevel, so notes must land in <ws>/meetings/notes —
        a dirname-based derivation would silently write <ws>/meetings/meetings/notes."""
        subprocess.run(("git", "init", "-q", self.ws), check=True)
        new_mirror = os.path.join(self.ws, "meetings", "granola")
        os.rename(self.mirror, new_mirror)
        self.mirror = new_mirror
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        r = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(self.note_path("2026-08-19-standup-not_aaa.md")), r.stdout)
        self.assertFalse(os.path.isdir(os.path.join(self.ws, "meetings", "meetings")))

    def test_granola_workspace_names_the_workspace_when_the_meetings_folder_is_its_own_repo(self):
        """When the workspace is a plain folder and meetings/ is its own repository, the git
        toplevel is meetings/ itself; GRANOLA_WORKSPACE names the real workspace, so notes
        land in <ws>/meetings/notes and not in <ws>/meetings/meetings/notes."""
        meetings = os.path.join(self.ws, "meetings")
        subprocess.run(("git", "init", "-q", meetings), check=True)
        new_mirror = os.path.join(meetings, "granola")
        os.rename(self.mirror, new_mirror)
        self.mirror = new_mirror
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        r = self.notes_sh(self.mirror, *LOW, GRANOLA_WORKSPACE=self.ws)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(self.note_path("2026-08-19-standup-not_aaa.md")), r.stdout)
        self.assertFalse(os.path.isdir(os.path.join(meetings, "meetings")))


class GeminiNotes(GranolaSandbox):
    GRANOLA = "2026-09-30-team-sync-not_aaa.md"
    GEMINI = "2026-09-30-team-sync-gem_doc123.md"

    def prompt(self):
        argv = self.last_claude_argv()
        self.assertIsNotNone(argv, "the generation call was never made")
        return argv[-1]

    def test_a_shared_event_id_generates_one_ordered_pair_and_records_the_gemini_version(self):
        granola = self.mirror_file(self.GRANOLA, event="evt123")
        gemini = self.gemini_file(self.GEMINI, event="evt123",
                                  modified="2026-09-30T12:00:00.000Z")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        call_input = self.last_claude_input()
        self.assertLess(call_input.index("capture 1 of 2: Granola"),
                        call_input.index("capture 2 of 2: Google Meet notes doc"))
        self.assertIn("Alice: hello there.", call_input)
        self.assertIn("Alice Example:** Hello Bob.", call_input)
        self.assertIn(self.read(granola), call_input)
        self.assertIn(self.read(gemini).rstrip("\n"), call_input)
        note = self.read(self.note_path(self.GRANOLA))
        self.assertIn("gemini-updated-at: 2026-09-30T12:00:00.000Z+m0", note)
        self.assertIn("1 paired", r.stdout)

    def test_a_relative_explicit_granola_path_stays_current_and_pairs_when_forced(self):
        granola = self.mirror_file(self.GRANOLA, event="evt123")
        gemini = self.gemini_file(self.GEMINI, event="evt123")
        self.notes_sh(self.mirror, *LOW)
        relative_granola = os.path.relpath(granola, os.getcwd())
        before = self.claude_call_count()

        current = self.notes_sh(self.mirror, relative_granola, *LOW)

        self.assertEqual(self.claude_call_count(), before, current.stdout)
        self.assertEqual(self.run_summary()["current"], 1, current.stdout)
        self.assertIn("gemini-updated-at:", self.read(self.note_path(self.GRANOLA)))

        forced = self.notes_sh(self.mirror, relative_granola, "--force", *LOW)

        self.assertEqual(forced.returncode, 0, forced.stdout)
        self.assertEqual(self.claude_call_count(), before + 1, forced.stdout)
        self.assertIn(self.read(gemini).rstrip("\n"), self.last_claude_input())
        self.assertIn("gemini-updated-at:", self.read(self.note_path(self.GRANOLA)))

    def test_paired_prompts_differ_when_the_gemini_doc_has_a_transcript(self):
        self.mirror_file(self.GRANOLA, event="evt123")
        self.gemini_file(self.GEMINI, event="evt123", transcript=False)

        notes_only = self.notes_sh(self.mirror, *LOW)
        notes_only_prompt = self.prompt()
        notes_only_input = self.last_claude_input()
        self.assertEqual(notes_only.returncode, 0, notes_only.stdout)
        self.assertIn("Gemini's short AI summary.", notes_only_input)
        self.assertIn("Alice and Bob agreed on the plan.", notes_only_input)
        self.assertIn("Send the draft.", notes_only_input)
        self.assertNotIn("**Alice Example:** Hello Bob.", notes_only_input)

        self.gemini_file(self.GEMINI, event="evt123", transcript=True,
                         modified="2026-09-30T13:00:00.000Z")
        transcribed = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(transcribed.returncode, 0, transcribed.stdout)
        self.assertNotEqual(self.prompt(), notes_only_prompt)
        self.assertIn("**Alice Example:** Hello Bob.", self.last_claude_input())

    def test_a_nonempty_meet_section_replaces_the_document_transcript_tab(self):
        self.mirror_file(self.GRANOLA, event="evt123")
        self.gemini_file(self.GEMINI, event="evt123", transcript=True,
                         topic_heading="Meeting transcription tooling")
        self.notes_sh(self.mirror, *LOW)
        prompt_without_meet = self.prompt()
        self.gemini_file(self.GEMINI, event="evt123", transcript=True,
                         modified="2026-09-30T13:00:00.000Z",
                         meet_lines=["[00:00:30] **Mira Example:** Meet recognition text."],
                         topic_heading="Meeting transcription tooling")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 2, r.stdout)
        self.assertNotEqual(self.prompt(), prompt_without_meet)
        call_input = self.last_claude_input()
        self.assertIn("conferenceRecords/rec1/transcripts/t1", call_input)
        self.assertIn("[00:00:30] **Mira Example:** Meet recognition text.", call_input)
        self.assertNotIn("**Alice Example:** Hello Bob.", call_input)
        self.assertIn("Alice and Bob agreed on the plan.", call_input)
        self.assertIn("Send the draft.", call_input)
        self.assertIn("Gemini's short AI summary.", call_input)
        self.assertIn("## **Meeting transcription tooling**", call_input)

    def test_a_gemini_version_change_regenerates_the_paired_note(self):
        self.mirror_file(self.GRANOLA, event="evt123")
        self.gemini_file(self.GEMINI, event="evt123", modified="2026-09-30T12:00:00.000Z")
        self.notes_sh(self.mirror, *LOW)
        before = self.claude_call_count()
        same = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(self.claude_call_count(), before, same.stdout)
        self.assertEqual(self.run_summary()["current"], 1, same.stdout)
        self.gemini_file(self.GEMINI, event="evt123", modified="2026-09-30T13:00:00.000Z")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), before + 1, r.stdout)
        self.assertIn("gemini-updated-at: 2026-09-30T13:00:00.000Z+m0",
                      self.read(self.note_path(self.GRANOLA)))

    def test_appending_a_meet_section_regenerates_a_paired_note(self):
        self.mirror_file(self.GRANOLA, event="evt123")
        gemini = self.gemini_file(self.GEMINI, event="evt123")
        self.notes_sh(self.mirror, *LOW)
        before = self.claude_call_count()
        with open(gemini, "a") as f:
            f.write("\n## Meet transcript\n\n"
                    "<!-- meet transcript: conferenceRecords/rec1/transcripts/t1 -->\n\n"
                    "[00:00:30] **Mira Example:** Late Meet entry.\n")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(self.claude_call_count(), before + 1, r.stdout)
        self.assertIn("gemini-updated-at: 2026-09-30T12:00:00.000Z+m1",
                      self.read(self.note_path(self.GRANOLA)))
        self.assertIn("[00:00:30] **Mira Example:** Late Meet entry.", self.last_claude_input())

    def test_appending_a_meet_section_regenerates_a_gemini_only_note(self):
        gemini = self.gemini_file(self.GEMINI, event=None)
        self.notes_sh(self.mirror, *LOW)
        before = self.claude_call_count()
        with open(gemini, "a") as f:
            f.write("\n## Meet transcript\n\n"
                    "<!-- meet transcript: conferenceRecords/rec1/transcripts/t1 -->\n\n"
                    "[00:00:30] **Mira Example:** Late Meet entry.\n")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(self.claude_call_count(), before + 1, r.stdout)
        self.assertIn("source-updated-at: 2026-09-30T12:00:00.000Z+m1",
                      self.read(self.note_path(self.GEMINI)))
        self.assertIn("[00:00:30] **Mira Example:** Late Meet entry.", self.last_claude_input())

    def test_partner_versions_and_capture_blocks_follow_filename_order(self):
        self.mirror_file(self.GRANOLA, event="evt123")
        later_name = "2026-09-30-z-team-gem_bbb.md"
        earlier_name = "2026-09-30-a-team-gem_aaa.md"
        self.gemini_file(later_name, event="evt123", modified="2026-09-30T13:00:00.000Z")
        self.gemini_file(earlier_name, event="evt123", modified="2026-09-30T12:00:00.000Z")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        call_input = self.last_claude_input()
        self.assertLess(call_input.index("/aaa/edit"), call_input.index("/bbb/edit"))
        self.assertIn("gemini-updated-at: 2026-09-30T12:00:00.000Z+m0,2026-09-30T13:00:00.000Z+m0",
                      self.read(self.note_path(self.GRANOLA)))

    def test_different_event_ids_do_not_pair_on_date_or_title(self):
        self.mirror_file(self.GRANOLA, event="evt123")
        self.gemini_file(self.GEMINI, event="evt456")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 2, r.stdout)
        self.assertTrue(os.path.exists(self.note_path(self.GRANOLA)))
        self.assertTrue(os.path.exists(self.note_path(self.GEMINI)))
        self.assertNotIn("gemini-updated-at:", self.read(self.note_path(self.GRANOLA)))

    def test_same_event_id_five_day_gap_does_not_pair(self):
        granola_name = "2026-08-01-team-sync-not_aaa.md"
        gemini_name = "2026-08-06-team-sync-gem_doc123.md"
        self.mirror_file(granola_name, event="evt123")
        self.gemini_file(gemini_name, event="evt123")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 2, r.stdout)
        self.assertTrue(os.path.exists(self.note_path(gemini_name)))
        self.assertNotIn("gemini-updated-at:", self.read(self.note_path(granola_name)))

    def test_same_event_id_adjacent_dates_do_not_pair(self):
        granola_name = "2026-08-01-team-sync-not_aaa.md"
        gemini_name = "2026-08-02-team-sync-gem_doc123.md"
        self.mirror_file(granola_name, event="evt123")
        self.gemini_file(gemini_name, event="evt123")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 2, r.stdout)
        self.assertTrue(os.path.exists(self.note_path(granola_name)))
        self.assertTrue(os.path.exists(self.note_path(gemini_name)))
        self.assertNotIn("gemini-updated-at:", self.read(self.note_path(granola_name)))

    def test_a_granola_note_without_a_partner_remains_current_without_a_gemini_key(self):
        self.mirror_file(self.GRANOLA)
        self.gemini_file("2026-09-30-unrelated-gem_other123.md", event="evt999")
        self.notes_sh(self.mirror, *LOW)
        note = self.read(self.note_path(self.GRANOLA))
        self.assertNotIn("gemini-updated-at:", note)
        before = self.claude_call_count()

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(self.claude_call_count(), before, r.stdout)
        self.assertEqual(self.run_summary()["current"], 2)

    def test_an_absent_gemini_mirror_does_not_regenerate_a_paired_note(self):
        self.mirror_file(self.GRANOLA, event="evt123")
        self.gemini_file(self.GEMINI, event="evt123")
        self.notes_sh(self.mirror, *LOW)
        gemini_dir = os.path.join(self.ws, "meetings", "gemini")
        os.unlink(os.path.join(gemini_dir, self.GEMINI))
        os.rmdir(gemini_dir)
        before = self.claude_call_count()

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(self.claude_call_count(), before, r.stdout)
        self.assertEqual(self.run_summary()["current"], 1)

    def test_an_unclaimed_gemini_transcript_gets_its_own_note(self):
        self.gemini_file(self.GEMINI, event=None, transcript=True)

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        self.assertTrue(os.path.exists(self.note_path(self.GEMINI)))
        self.assertIn("from gemini/" + self.GEMINI, self.read(self.note_path(self.GEMINI)))
        call_input = self.last_claude_input()
        self.assertIn("Gemini's short AI summary.", call_input)
        self.assertIn("Alice and Bob agreed on the plan.", call_input)
        self.assertIn("Send the draft.", call_input)
        self.assertIn("**Alice Example:** Hello Bob.", call_input)

    def test_a_gemini_only_note_is_current_on_rerun(self):
        self.gemini_file(self.GEMINI, event=None)
        self.notes_sh(self.mirror, *LOW)
        before = self.claude_call_count()

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(self.claude_call_count(), before, r.stdout)
        self.assertEqual(self.run_summary()["current"], 1)

    def test_a_gemini_doc_change_regenerates_the_note_at_the_same_path(self):
        self.gemini_file(self.GEMINI, event=None)
        self.notes_sh(self.mirror, *LOW)
        note_path = self.note_path(self.GEMINI)
        before = self.claude_call_count()
        self.gemini_file(self.GEMINI, event=None, modified="2026-09-30T13:00:00.000Z")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(self.claude_call_count(), before + 1, r.stdout)
        self.assertEqual(self.note_path(self.GEMINI), note_path)
        self.assertIn("source-updated-at: 2026-09-30T13:00:00.000Z+m0",
                      self.read(note_path))

    def test_a_hand_edited_gemini_only_note_is_held_after_a_doc_change(self):
        self.gemini_file(self.GEMINI, event=None)
        self.notes_sh(self.mirror, *LOW)
        note_path = self.note_path(self.GEMINI)
        with open(note_path, "a") as f:
            f.write("\nHand edit.\n")
        with open(note_path, "rb") as f:
            before_bytes = f.read()
        before = self.claude_call_count()
        self.gemini_file(self.GEMINI, event=None, modified="2026-09-30T13:00:00.000Z")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(self.claude_call_count(), before, r.stdout)
        self.assertEqual(self.run_summary()["held_edited"], 1)
        with open(note_path, "rb") as f:
            self.assertEqual(f.read(), before_bytes)

    def test_a_gemini_only_meet_transcript_is_used_without_a_granola_claim(self):
        self.gemini_file(self.GEMINI, event=None, transcript=True,
                         meet_lines=["[00:00:30] **Mira Example:** Meet recognition text."])

        gemini_only = self.notes_sh(self.mirror, *LOW)
        gemini_only_prompt = self.prompt()
        self.assertEqual(gemini_only.returncode, 0, gemini_only.stdout)
        self.assertIn("Gemini's short AI summary.", self.last_claude_input())
        self.assertIn("Alice and Bob agreed on the plan.", self.last_claude_input())
        self.assertIn("Send the draft.", self.last_claude_input())
        self.assertIn("[00:00:30] **Mira Example:** Meet recognition text.", self.last_claude_input())

        self.gemini_file(self.GEMINI, event="evt123", transcript=True,
                         meet_lines=["[00:00:30] **Mira Example:** Meet recognition text."])
        self.mirror_file(self.GRANOLA, event="evt123")
        paired = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(paired.returncode, 0, paired.stdout)
        call_input = self.last_claude_input()
        self.assertIn("[00:00:30] **Mira Example:** Meet recognition text.", call_input)
        self.assertNotIn("**Alice Example:** Hello Bob.", call_input)
        self.assertIn("capture 1 of 2: Granola", call_input)
        self.assertNotEqual(self.prompt(), gemini_only_prompt)

    def test_an_unclaimed_notes_only_gemini_doc_is_noted_as_unverified(self):
        self.gemini_file(self.GEMINI, event=None, transcript=False)

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(self.note_path(self.GEMINI)))
        notes_only_prompt = self.prompt()
        call_input = self.last_claude_input()
        self.assertIn("Gemini's short AI summary.", call_input)
        self.assertIn("Alice and Bob agreed on the plan.", call_input)
        self.assertIn("Send the draft.", call_input)
        self.assertNotIn("**Alice Example:** Hello Bob.", call_input)
        self.gemini_file(self.GEMINI, event=None, transcript=True,
                         modified="2026-09-30T13:00:00.000Z")
        with_transcript = self.notes_sh(self.mirror, *LOW)
        self.assertEqual(with_transcript.returncode, 0, with_transcript.stdout)
        self.assertNotEqual(self.prompt(), notes_only_prompt)
        self.assertIn("**Alice Example:** Hello Bob.", self.last_claude_input())

    def test_an_empty_meet_capture_without_a_granola_twin_is_skipped(self):
        gemini = self.gemini_file(self.GEMINI, event=None, transcript=False, meet_lines=[])
        empty_doc = self.read(gemini)
        self.assertIn("## **Team sync \\- Transcript**", empty_doc)
        self.assertIn("### **Transcription ended after 00:00:11**", empty_doc)
        self.assertNotIn("**Alice Example:** Hello Bob.", empty_doc)

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 0, r.stdout)
        self.assertFalse(os.path.exists(self.note_path(self.GEMINI)))

        explicit = self.notes_sh(self.mirror, gemini, *LOW)

        self.assertEqual(explicit.returncode, 0, explicit.stdout)
        self.assertEqual(self.claude_call_count(), 0, explicit.stdout)
        self.assertFalse(os.path.exists(self.note_path(self.GEMINI)))
        self.assertEqual(explicit.stdout.count("skipping empty Gemini capture"), 1,
                         explicit.stdout)

    def test_an_empty_gemini_capture_deletes_its_verified_generated_note(self):
        import hashlib

        gemini = self.gemini_file(self.GEMINI, event=None, transcript=False, meet_lines=[])
        body = "# Empty capture\n\nThis was generated before the empty Meet section arrived.\n"
        bodyhash = hashlib.sha256(body.encode()).hexdigest()
        note = self.write_note(
            self.GEMINI,
            "<!-- auto-generated 2026-09-30 by granola-mirror/notes.sh "
            "from gemini/%s — source-updated-at: 2026-09-30T12:00:00.000Z+m1 "
            "body-sha256: %s — unattended extraction -->" % (self.GEMINI, bodyhash),
            body=body,
        )

        r = self.notes_sh(self.mirror, *LOW)

        self.assertFalse(os.path.exists(note), r.stdout)
        self.assertEqual(self.claude_call_count(), 0, r.stdout)
        self.assertIn("1 empty captures skipped", r.stdout)
        self.assertIn("1 superseded", r.stdout)

    def test_an_edited_note_for_an_empty_gemini_capture_is_kept_and_counted(self):
        gemini = self.gemini_file(self.GEMINI, event=None, transcript=False, meet_lines=[])
        self.notes_sh(self.mirror, *LOW)
        body = "# Empty capture\n\nThis note was edited.\n"
        import hashlib
        bodyhash = hashlib.sha256(body.encode()).hexdigest()
        note = self.write_note(
            self.GEMINI,
            "<!-- auto-generated 2026-09-30 by granola-mirror/notes.sh "
            "from gemini/%s — source-updated-at: 2026-09-30T12:00:00.000Z+m1 "
            "body-sha256: %s — unattended extraction -->" % (self.GEMINI, bodyhash),
            body=body,
        )
        with open(note, "a") as f:
            f.write("\nHuman edit.\n")
        before = self.read(note)

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(self.read(note), before)
        self.assertEqual(self.run_summary()["held_edited"], 1)
        self.assertIn("1 edited", r.stdout)
        self.assertEqual(self.claude_call_count(), 0, r.stdout)

    def test_an_empty_meet_capture_is_ignored_when_pairing_a_real_doc_and_removes_its_old_note(self):
        import hashlib

        self.mirror_file(self.GRANOLA, event="evt123")
        real_name = "2026-09-30-team-sync-gem_real123.md"
        empty_name = "2026-09-30-team-sync-gem_empty123.md"
        real = self.gemini_file(real_name, event="evt123",
                                modified="2026-09-30T13:00:00.000Z")
        empty = self.gemini_file(empty_name, event="evt123",
                                 modified="2026-09-30T12:00:00.000Z",
                                 transcript=False, meet_lines=[])
        body = "# Existing Gemini note\n\nKept as-is.\n"
        bodyhash = hashlib.sha256(body.encode()).hexdigest()
        old_note = self.write_note(
            empty_name,
            "<!-- auto-generated 2026-09-30 by granola-mirror/notes.sh "
            "from gemini/%s — source-updated-at: 2026-09-30T12:00:00.000Z+m1 "
            "body-sha256: %s — unattended extraction -->" % (empty_name, bodyhash),
            body=body,
        )
        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        call_input = self.last_claude_input()
        self.assertIn(self.read(real).rstrip("\n"), call_input)
        self.assertNotIn("/empty123/edit", call_input)
        self.assertIn("capture 2 of 2: Google Meet notes doc", call_input)
        pair_note = self.read(self.note_path(self.GRANOLA))
        self.assertIn("gemini-updated-at: 2026-09-30T13:00:00.000Z+m0", pair_note)
        self.assertNotIn("2026-09-30T12:00:00.000Z", pair_note)
        self.assertFalse(os.path.exists(old_note), "verified note for an empty capture is superseded")
        self.assertIn("1 superseded", r.stdout)
        self.assertTrue(os.path.exists(empty), "the empty capture source must remain untouched")

    def test_a_granola_file_without_a_transcript_does_not_claim_its_gemini_partner(self):
        self.mirror_file(self.GRANOLA, event="evt123", transcript=None)
        self.gemini_file(self.GEMINI, event="evt123", transcript=False)

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        self.assertTrue(os.path.exists(self.note_path(self.GEMINI)))
        self.assertFalse(os.path.exists(self.note_path(self.GRANOLA)))

    def test_a_transcribed_granola_twin_replaces_an_unedited_gemini_only_note(self):
        gemini = self.gemini_file(self.GEMINI, event="evt123")
        self.notes_sh(self.mirror, *LOW)
        gemini_note = self.note_path(self.GEMINI)
        self.assertTrue(os.path.exists(gemini_note))
        granola = self.mirror_file(self.GRANOLA, event="evt123")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(self.note_path(self.GRANOLA)))
        self.assertFalse(os.path.exists(gemini_note), "verified generated note should be superseded")
        self.assertIn("1 superseded", r.stdout)
        self.assertIn(self.read(granola), self.last_claude_input())

    def test_a_current_paired_note_still_supersedes_a_verified_gemini_only_note(self):
        import hashlib

        self.mirror_file(self.GRANOLA, event="evt123")
        self.gemini_file(self.GEMINI, event="evt123")
        self.notes_sh(self.mirror, *LOW)
        body = "# Recovered Gemini note\n\nThis duplicate should be removed.\n"
        bodyhash = hashlib.sha256(body.encode()).hexdigest()
        gemini_note = self.write_note(
            self.GEMINI,
            "<!-- auto-generated 2026-09-30 by granola-mirror/notes.sh "
            "from gemini/%s — source-updated-at: 2026-09-30T12:00:00.000Z+m0 "
            "body-sha256: %s — unattended extraction -->" % (self.GEMINI, bodyhash),
            body=body,
        )
        before = self.claude_call_count()

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(self.claude_call_count(), before, r.stdout)
        self.assertFalse(os.path.exists(gemini_note))
        self.assertIn("1 superseded", r.stdout)

    def test_a_failed_paired_generation_keeps_the_gemini_only_note(self):
        self.gemini_file(self.GEMINI, event="evt123")
        self.notes_sh(self.mirror, *LOW)
        gemini_note = self.note_path(self.GEMINI)
        self.assertTrue(os.path.exists(gemini_note))
        self.mirror_file(self.GRANOLA, event="evt123", transcript="Alice: GRANOLA_FAIL_TOKEN")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(gemini_note))
        self.assertEqual(self.run_summary()["failed"], 1)

    def test_two_granola_captures_for_one_event_each_pair_with_one_gemini_doc(self):
        first = "2026-09-30-team-sync-not_first.md"
        second = "2026-09-30-team-sync-not_second.md"
        self.mirror_file(first, event="evt123")
        self.mirror_file(second, event="evt123")
        self.gemini_file(self.GEMINI, event="evt123")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 2, r.stdout)
        self.assertIn("gemini-updated-at:", self.read(self.note_path(first)))
        self.assertIn("gemini-updated-at:", self.read(self.note_path(second)))
        self.assertFalse(os.path.exists(self.note_path(self.GEMINI)))
        self.assertIn("2 paired", r.stdout)

    def test_superseding_after_a_gemini_only_note_was_deleted_clears_its_held_marker(self):
        self.gemini_file(self.GEMINI, event="evt123")
        old_note = self.write_note(self.GEMINI, "<!-- legacy generated note -->")
        self.notes_sh(self.mirror, *LOW)
        self.assertTrue(self.held_marker(self.GEMINI))
        os.unlink(old_note)
        self.mirror_file(self.GRANOLA, event="evt123")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(self.note_path(self.GRANOLA)))
        self.assertFalse(os.path.exists(old_note))
        self.assertFalse(self.held_marker(self.GEMINI))

    def test_a_hand_edited_gemini_only_note_is_kept_and_counted_when_a_pair_arrives(self):
        self.gemini_file(self.GEMINI, event="evt123")
        self.notes_sh(self.mirror, *LOW)
        gemini_note = self.note_path(self.GEMINI)
        with open(gemini_note, "a") as f:
            f.write("\nHand edit.\n")
        self.mirror_file(self.GRANOLA, event="evt123")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(gemini_note))
        self.assertIn("Hand edit.", self.read(gemini_note))
        self.assertTrue(os.path.exists(self.note_path(self.GRANOLA)))
        self.assertEqual(self.run_summary()["held_edited"], 1)
        self.assertIn("1 edited", r.stdout)

    def test_an_unparseable_gemini_only_banner_is_kept_and_held_when_a_pair_arrives(self):
        self.gemini_file(self.GEMINI, event="evt123")
        gemini_note = self.write_note(self.GEMINI, "<!-- legacy note -->")
        self.mirror_file(self.GRANOLA, event="evt123")

        r = self.notes_sh(self.mirror, *LOW)

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(gemini_note))
        self.assertEqual(self.run_summary()["held_unparseable"], 1)
        self.assertTrue(self.held_marker(self.GEMINI))

    def test_explicitly_naming_a_claimed_gemini_file_exits_two_with_its_granola_twin(self):
        self.mirror_file(self.GRANOLA, event="evt123")
        gemini = self.gemini_file(self.GEMINI, event="evt123")

        r = self.notes_sh(self.mirror, gemini)

        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn(self.GRANOLA, r.stdout)
        self.assertEqual(self.claude_call_count(), 0, r.stdout)

    def test_explicitly_naming_an_unclaimed_pre_floor_gemini_file_bypasses_the_floor(self):
        name = "2026-07-16-team-sync-gem_old123.md"
        gemini = self.gemini_file(name, event=None)

        r = self.notes_sh(self.mirror, gemini, "--since", "2026-08-19")

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        self.assertTrue(os.path.exists(self.note_path(name)))

    def test_explicitly_naming_an_unclaimed_gemini_file_by_relative_path_bypasses_the_floor(self):
        name = "2026-07-16-team-sync-gem_relative123.md"
        gemini = self.gemini_file(name, event=None)
        relative_gemini = os.path.relpath(gemini, os.getcwd())

        r = self.notes_sh(self.mirror, relative_gemini, "--since", "2026-08-19")

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        self.assertTrue(os.path.exists(self.note_path(name)))

    def test_an_unclaimed_pre_floor_gemini_file_is_skipped_without_an_explicit_file(self):
        name = "2026-07-16-team-sync-gem_old123.md"
        self.gemini_file(name, event=None)

        r = self.notes_sh(self.mirror, "--since", "2026-08-19")

        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 0, r.stdout)
        self.assertFalse(os.path.exists(self.note_path(name)))


if __name__ == "__main__":
    unittest.main()
