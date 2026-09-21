"""migrate-banners.py — the one-time stamp migration.

Two loops, both local (no MCP). Loop A hash-stamps the existing auto notes so a
hand-corrected note survives and no existing note trips the unparseable-hold alarm on the
first version-model run — the hash it records MUST come from `notes.sh --hash`, which the
cross-check below proves by stamping a fixture and asserting notes.sh then reads it as
current. Loop B marker-stamps mirror transcripts at or above the stored floor, leaving the
historical below-floor set grandfathered so a mass re-summarization can't queue a refetch
storm.
"""
import os
import subprocess
import unittest

from _harness import GranolaSandbox, MIGRATE, NOTES_SH


class Migration(GranolaSandbox):
    OLD_BANNER = "<!-- auto-generated 2026-08-19 by an earlier notes.sh from granola/%s — unattended extraction; judgment calls flagged in Sources & reliability -->"

    def migrate(self, *args):
        return subprocess.run(("python3", MIGRATE, self.mirror) + args, env=self.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # --- Loop A -----------------------------------------------------------
    def test_loop_a_stamps_an_old_note_so_notes_sh_reads_it_current(self):
        """The shared-hash cross-check: stamp, then notes.sh must skip the note as current
        (body hash matches, version equal) with no model call."""
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name)
        self.write_note(name, banner=self.OLD_BANNER % name,
                        body="# Standup — 2026-08-19\n\nAn analyst's own words.\n")
        r = self.migrate("--floor", "2000-01-01")
        self.assertEqual(r.returncode, 0, r.stdout)
        banner = self.read(self.note_path(name)).splitlines()[0]
        self.assertIn("source-updated-at: 2026-08-19T10:00:00Z", banner)
        self.assertRegex(banner, r"body-sha256: [0-9a-f]{64}")
        # now the version model must see it as current, not held, not regenerated
        run = self.notes_sh(self.mirror, "--since", "2000-01-01")
        self.assertEqual(self.claude_call_count(), 0, run.stdout)
        self.assertEqual(self.run_summary()["current"], 1, run.stdout)

    def test_loop_a_preserves_the_hand_written_body_verbatim(self):
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name)
        body = "# Standup — 2026-08-19\n\nLine one.\nLine two, hand corrected.\n"
        self.write_note(name, banner=self.OLD_BANNER % name, body=body)
        self.migrate("--floor", "2000-01-01")
        after = self.read(self.note_path(name))
        self.assertEqual("".join(after.splitlines(keepends=True)[2:]), body)

    def test_loop_a_is_idempotent(self):
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name)
        self.write_note(name, banner=self.OLD_BANNER % name, body="# S\n\nBody.\n")
        self.migrate("--floor", "2000-01-01")
        first = self.read(self.note_path(name))
        r = self.migrate("--floor", "2000-01-01")
        self.assertEqual(self.read(self.note_path(name)), first, "second run must not restamp")
        self.assertIn("already", r.stdout)

    # --- Loop B -----------------------------------------------------------
    def test_loop_b_stamps_an_at_floor_grandfathered_transcript(self):
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name, updated="2026-08-19T10:00:00Z", tmark=None)  # no marker
        self.migrate("--floor", "2026-08-01")
        body = self.read(os.path.join(self.mirror, name))
        self.assertIn("<!-- transcript for updated_at: 2026-08-19T10:00:00Z -->", body)

    def test_loop_b_leaves_below_floor_files_grandfathered(self):
        name = "2026-03-01-old-not_zzz.md"
        self.mirror_file(name, updated="2026-03-01T10:00:00Z", tmark=None)
        before = self.read(os.path.join(self.mirror, name))
        self.migrate("--floor", "2026-08-01")
        self.assertEqual(self.read(os.path.join(self.mirror, name)), before,
                         "a below-floor file must stay marker-free (refetch-storm guard)")

    def test_loop_b_leaves_a_marker_bearing_transcript_untouched(self):
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name, updated="2026-08-19T10:00:00Z", tmark="__match__")
        before = self.read(os.path.join(self.mirror, name))
        self.migrate("--floor", "2026-08-01")
        self.assertEqual(self.read(os.path.join(self.mirror, name)), before)

    def test_loop_b_skips_files_without_a_transcript(self):
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name, transcript=None)
        before = self.read(os.path.join(self.mirror, name))
        self.migrate("--floor", "2026-08-01")
        self.assertEqual(self.read(os.path.join(self.mirror, name)), before)

    def test_the_floor_defaults_to_the_stored_since_file(self):
        with open(os.path.join(self.state, "granola-notes-since"), "w") as f:
            f.write("2026-08-01")
        self.mirror_file("2026-08-19-new-not_aaa.md", updated="2026-08-19T10:00:00Z", tmark=None)
        self.mirror_file("2026-03-01-old-not_zzz.md", updated="2026-03-01T10:00:00Z", tmark=None)
        self.migrate()  # no --floor -> read the stored floor
        self.assertIn("transcript for updated_at",
                      self.read(os.path.join(self.mirror, "2026-08-19-new-not_aaa.md")))
        self.assertNotIn("transcript for updated_at",
                         self.read(os.path.join(self.mirror, "2026-03-01-old-not_zzz.md")))


if __name__ == "__main__":
    unittest.main()
