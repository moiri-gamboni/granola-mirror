"""refresh.sh — the one internal pipeline lock and the alarm set it owns.

Every trigger serializes on ~/.locks/granola-pipeline; a standalone notes.sh refuses rather
than stalling, and only a real 3h wait pages as a lock timeout. refresh.sh owns all push
alarms so a manual note run never pages: a wholly-failed run (with the first error verbatim),
a single wedged meeting while others move, a held note, a failed digest, the lock timeout.

The pipeline runs against a clone whose fetchers are stubs (no network) and a stubbed claude
(no model call, no rate-limit token); ntfy is a stubbed curl that records to a log.
"""
import fcntl
import json
import os
import shutil
import unittest

from _harness import GM, GranolaSandbox


class RefreshSandbox(GranolaSandbox):
    def setUp(self):
        super().setUp()
        self.build_refresh_clone()
        self.seed_floor("2000-01-01")   # an established install: the dated fixtures are in scope

    def hold_lock(self):
        lock = os.path.join(self.home, ".locks", "granola-pipeline")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        f = open(lock, "w")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        self.addCleanup(f.close)
        return f

    def seed_wedge(self, mirror_name, streak, version):
        d = os.path.join(self.state, "granola-note-wedge")
        os.makedirs(d, exist_ok=True)
        base = mirror_name.rsplit(".", 1)[0]
        with open(os.path.join(d, base + ".json"), "w") as f:
            json.dump({"streak": streak, "failed_version": version, "last_at": "2026-08-19T00:00:00+00:00"}, f)


class Lock(RefreshSandbox):
    def test_standalone_notes_refuses_while_the_pipeline_is_running(self):
        """A manual notes.sh must not stall up to 3h behind a running pipeline; it refuses."""
        p = self.mirror_file("2026-08-19-standup-not_aaa.md")
        self.hold_lock()
        r = self.notes_sh(self.mirror, p)    # standalone: no GRANOLA_LOCK_HELD
        self.assertEqual(r.returncode, 75, r.stdout)
        self.assertIn("pipeline is running", r.stdout)
        self.assertEqual(self.claude_call_count(), 0)

    def test_a_lock_timeout_pages_as_rc_75(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        self.hold_lock()
        r = self.run_refresh(self.mirror, GRANOLA_LOCK_WAIT="1")
        self.assertEqual(r.returncode, 75, r.stdout)
        self.assertTrue(any("lock timeout" in c.lower() for c in self.ntfy_calls()),
                        self.ntfy_calls())
        self.assertEqual(self.claude_call_count(), 0, "the pipeline body never ran")

    def test_a_normal_run_generates_and_stays_silent(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        r = self.run_refresh(self.mirror)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        self.assertTrue(os.path.exists(self.note_path("2026-08-19-standup-not_aaa.md")))
        self.assertEqual(self.ntfy_calls(), [], "a clean run must not page")


class RunLevelAlarm(RefreshSandbox):
    def test_a_wholly_failed_run_pages_with_the_first_error(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        r = self.run_refresh(self.mirror, CLAUDE_STUB_MODE="fail", CLAUDE_STUB_ERR="claude: command not found")
        pages = "\n".join(self.ntfy_calls())
        self.assertIn("every generation failed", pages, r.stdout)
        self.assertIn("claude: command not found", pages)

    def test_a_zero_attempt_run_does_not_page_a_total_failure(self):
        """All candidates settling / awaiting transcript: no generation was attempted, so a
        total failure would be a false page."""
        self.mirror_file("2026-08-19-standup-not_aaa.md", summary=None)  # unsettled
        r = self.run_refresh(self.mirror)
        self.assertFalse(any("every generation failed" in c for c in self.ntfy_calls()), r.stdout)


class WedgeAlarm(RefreshSandbox):
    def test_a_wedged_meeting_pages_when_another_succeeds(self):
        wedged = "2026-08-19-wedged-not_aaa.md"
        ok = "2026-08-19-fine-not_bbb.md"
        self.mirror_file(wedged, updated="2026-08-19T10:00:00Z",
                         summary="Summary. GRANOLA_FAIL_TOKEN")     # this one fails
        self.mirror_file(ok)                                        # this one succeeds
        self.seed_wedge(wedged, streak=2, version="2026-08-19T10:00:00Z")  # -> 3 this run
        r = self.run_refresh(self.mirror)
        pages = "\n".join(self.ntfy_calls())
        self.assertIn("wedged", pages, r.stdout)
        self.assertIn("2026-08-19-wedged-not_aaa", pages)

    def test_an_env_class_outage_pages_once_at_the_run_level_not_per_meeting(self):
        """When every meeting fails (claude off PATH), the wedge alarm stays silent — the
        run-level alarm is the single page, not one wedge push per meeting."""
        a = "2026-08-19-a-not_aaa.md"
        b = "2026-08-19-b-not_bbb.md"
        self.mirror_file(a, updated="2026-08-19T10:00:00Z")
        self.mirror_file(b, updated="2026-08-19T10:00:00Z")
        self.seed_wedge(a, streak=5, version="2026-08-19T10:00:00Z")
        self.seed_wedge(b, streak=5, version="2026-08-19T10:00:00Z")
        r = self.run_refresh(self.mirror, CLAUDE_STUB_MODE="fail")
        self.assertFalse(any("wedged" in c for c in self.ntfy_calls()),
                         "no per-meeting wedge page when nothing succeeded: %s" % self.ntfy_calls())
        self.assertTrue(any("every generation failed" in c for c in self.ntfy_calls()), r.stdout)


class HeldAlarm(RefreshSandbox):
    def test_a_held_note_pages_once(self):
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name)
        self.write_note(name, banner="<!-- auto-generated 2026-08-19 by notes.sh -->",
                        body="# Old\n\nNo version banner.\n")
        self.run_refresh(self.mirror)
        first = [c for c in self.ntfy_calls() if "held" in c.lower()]
        self.assertTrue(first, "a banner-absent note must page")
        self.run_refresh(self.mirror)          # still held next run
        second = [c for c in self.ntfy_calls() if "held" in c.lower()]
        self.assertEqual(len(first), len(second), "held alarm must fire once, not every run")

    def test_a_resolved_hold_re_arms_the_alarm(self):
        name = "2026-08-19-standup-not_aaa.md"
        self.mirror_file(name)
        self.write_note(name, banner="<!-- auto-generated 2026-08-19 by notes.sh -->",
                        body="# Old\n\nNo banner.\n")
        self.run_refresh(self.mirror)
        n1 = len([c for c in self.ntfy_calls() if "held" in c.lower()])
        # a --force resolution: the note is machine-written again, the held marker cleared
        self.notes_sh(self.mirror, os.path.join(self.mirror, name), "--force")
        self.run_refresh(self.mirror)          # reconcile disarms the resolved hold
        # re-break it and confirm a fresh page
        self.write_note(name, banner="<!-- auto-generated 2026-08-19 by notes.sh -->",
                        body="# Broken again.\n")
        self.run_refresh(self.mirror)
        n2 = len([c for c in self.ntfy_calls() if "held" in c.lower()])
        self.assertGreater(n2, n1, "a re-broken note must page again after resolution")


class DigestAlarm(RefreshSandbox):
    def test_a_failed_digest_pages(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        self.enable_digest_stub()
        r = self.run_refresh(self.mirror, "--digest", DIGEST_RC="1")
        self.assertTrue(any("digest failed" in c.lower() for c in self.ntfy_calls()), r.stdout)

    def test_a_healthy_digest_is_silent(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        self.enable_digest_stub()
        r = self.run_refresh(self.mirror, "--digest", DIGEST_RC="0")
        self.assertFalse(any("digest failed" in c.lower() for c in self.ntfy_calls()), r.stdout)


class NotesPrerunAlarm(RefreshSandbox):
    """notes.sh refuses before writing its run summary when the skill file is missing beside
    it; run_alarms sees "no run" and would stay silent forever, so refresh.sh pages on the
    absent summary itself."""

    def clone_skill(self):
        return os.path.join(os.path.dirname(self.refresh), "skills", "meetings", "SKILL.md")

    def prerun_pages(self):
        return [c for c in self.ntfy_calls() if "did not run" in c.lower()]

    def test_a_missing_skill_file_pages_once(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        os.remove(self.clone_skill())
        r = self.run_refresh(self.mirror)
        self.assertEqual(self.claude_call_count(), 0, "no generation without the procedure")
        self.assertEqual(len(self.prerun_pages()), 1, r.stdout + "\n" + "\n".join(self.ntfy_calls()))
        self.run_refresh(self.mirror)          # still missing next run
        self.assertEqual(len(self.prerun_pages()), 1, "the alarm fires once per arming, not every run")

    def test_a_normal_run_re_arms_the_alarm(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        os.remove(self.clone_skill())
        self.run_refresh(self.mirror)
        n1 = len(self.prerun_pages())
        shutil.copy(os.path.join(GM, "skills", "meetings", "SKILL.md"), self.clone_skill())
        r = self.run_refresh(self.mirror)      # a run with a summary disarms
        self.assertEqual(self.claude_call_count(), 1, r.stdout)
        os.remove(self.clone_skill())
        self.run_refresh(self.mirror)
        self.assertGreater(len(self.prerun_pages()), n1, "a restored then re-broken clone must page again")


class OAuthAlarm(RefreshSandbox):
    def test_transcript_oauth_expiry_still_pages(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        r = self.run_refresh(self.mirror, TRANSCRIPTS_RC="3")
        self.assertTrue(any("re-auth" in c.lower() for c in self.ntfy_calls()), r.stdout)


class EnvFileConfig(RefreshSandbox):
    def test_the_env_file_supplies_the_mirror_dir(self):
        """With no argument and no environment value, ~/.config/granola/env provides
        GRANOLA_MIRROR, so schedulers can invoke refresh.sh with no per-deployment path."""
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        cfg = os.path.join(self.home, ".config", "granola")
        os.makedirs(cfg, exist_ok=True)
        with open(os.path.join(cfg, "env"), "w") as f:
            f.write(f"GRANOLA_MIRROR={self.mirror}\n")
        r = self.run_refresh(GRANOLA_MIRROR="")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(self.note_path("2026-08-19-standup-not_aaa.md")), r.stdout)

    def test_no_path_anywhere_still_refuses(self):
        r = self.run_refresh(GRANOLA_MIRROR="")
        self.assertEqual(r.returncode, 2, r.stdout)


if __name__ == "__main__":
    unittest.main()
