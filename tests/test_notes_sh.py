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


if __name__ == "__main__":
    unittest.main()
