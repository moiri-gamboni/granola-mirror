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
import subprocess
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


class GlossaryCommit(RefreshSandbox):
    def test_both_glossary_tiers_are_committed_in_their_own_repo(self):
        """A promotion edits the reviewed tier as well as the auto tier, and workflows/ is
        its own repo in the polyrepo layout, so --commit must commit both files there."""
        git_env = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        wf_repo = os.path.dirname(self.wf)
        row = "- Alice | \"Alyss\" | Med | 2026-08-01 sync"
        with open(os.path.join(self.wf, "transcript-corrections-auto.md"), "w") as f:
            f.write("# auto\n\n%s\n" % row)
        with open(os.path.join(self.wf, "transcript-corrections.md"), "w") as f:
            f.write("# reviewed\n")
        for repo in (self.ws, wf_repo):
            subprocess.run(("git", "init", "-q", repo), check=True)
        subprocess.run(("git", "-C", wf_repo, "add", "."), check=True)
        subprocess.run(("git", "-C", wf_repo, "commit", "-qm", "init"), check=True,
                       env=dict(os.environ, **git_env))
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        r = self.run_refresh("--commit", self.mirror, CLAUDE_STUB_GLOSSARY="promote: " + row,
                             **git_env)
        self.assertEqual(r.returncode, 0, r.stdout)
        status = subprocess.run(("git", "-C", wf_repo, "status", "--porcelain"),
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "", r.stdout)
        shown = subprocess.run(("git", "-C", wf_repo, "show", "--stat", "--format=%s", "HEAD"),
                               capture_output=True, text=True, check=True).stdout
        self.assertIn("transcript-corrections.md", shown)
        self.assertIn("transcript-corrections-auto.md", shown)


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


class GeminiRefresh(RefreshSandbox):
    def configure_env_file(self, text):
        cfg = os.path.join(self.home, ".config", "granola")
        os.makedirs(cfg, exist_ok=True)
        with open(os.path.join(cfg, "env"), "w") as f:
            f.write(text)

    def pages(self, title):
        return [c for c in self.ntfy_calls() if "Title: " + title in c]

    def test_an_unset_remote_skips_the_step(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        r = self.run_refresh(self.mirror, GEMINI_RCLONE_REMOTE="")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.gemini_notes_calls_made(), [])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "meetings", "gemini")))

    def test_env_file_remote_runs_with_an_argument_and_before_the_digest(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        self.configure_env_file("GEMINI_RCLONE_REMOTE=drive-test:\n")
        self.enable_digest_stub()
        r = self.run_refresh("--digest", self.mirror, GEMINI_RCLONE_REMOTE="")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.gemini_notes_calls_made(), [
            "sync %s --remote drive-test:" % os.path.join(self.ws, "meetings", "gemini")
        ])
        self.assertLess(r.stdout.index("granola-refresh: transcripts"),
                        r.stdout.index("gemini-notes stub called"), r.stdout)
        self.assertLess(r.stdout.index("gemini-notes stub called"),
                        r.stdout.index("granola-refresh: digest"), r.stdout)
        self.assertTrue(os.path.isdir(os.path.join(self.ws, "meetings", "gemini")))

    def test_environment_remote_overrides_the_env_file(self):
        self.configure_env_file("GRANOLA_MIRROR=%s\nGEMINI_RCLONE_REMOTE=file-remote:\n" % self.mirror)
        r = self.run_refresh(GRANOLA_MIRROR="", GEMINI_RCLONE_REMOTE="env-remote:")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.gemini_notes_calls_made(), [
            "sync %s --remote env-remote:" % os.path.join(self.ws, "meetings", "gemini")
        ])

    def test_query_failure_pages_line_one_then_success_rearms_and_notes_still_run(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        err = "gemini-notes: query failed: first query problem\nsecond diagnostic line"
        first = self.run_refresh(self.mirror, GEMINI_RCLONE_REMOTE="drive-test:",
                                 GEMINI_NOTES_RC="1", GEMINI_NOTES_STDERR=err)
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertIn(err, first.stdout, "all child stderr lines must remain in the refresh log")
        self.assertEqual(self.claude_call_count(), 1, first.stdout)
        pages = self.pages("Gemini notes fetch failed")
        self.assertEqual(len(pages), 1, self.ntfy_calls())
        self.assertIn("first query problem; see journalctl -t granola-refresh", pages[0])
        self.assertNotIn("second diagnostic line", pages[0])

        healthy = self.run_refresh(self.mirror, GEMINI_RCLONE_REMOTE="drive-test:",
                                   GEMINI_NOTES_RC="0", GEMINI_NOTES_STDERR="")
        self.assertEqual(healthy.returncode, 0, healthy.stdout)
        self.assertIn("granola-refresh: notes", healthy.stdout)
        again = self.run_refresh(self.mirror, GEMINI_RCLONE_REMOTE="drive-test:",
                                 GEMINI_NOTES_RC="1", GEMINI_NOTES_STDERR=err)
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("granola-refresh: notes", again.stdout)
        self.assertEqual(len(self.pages("Gemini notes fetch failed")), 2, self.ntfy_calls())

    def test_export_failures_and_a_later_query_failure_page_independently(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        export_err = ("gemini-notes: export failed doc-aaa: missing\n"
                      "gemini-notes: export failed doc-bbb: denied")
        first = self.run_refresh(self.mirror, GEMINI_RCLONE_REMOTE="drive-test:",
                                 GEMINI_NOTES_RC="2", GEMINI_NOTES_STDERR=export_err)
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertIn(export_err, first.stdout)
        doc_pages = self.pages("Gemini notes: some docs failed")
        self.assertEqual(len(doc_pages), 1, self.ntfy_calls())
        self.assertIn("doc-aaa doc-bbb", doc_pages[0])
        self.assertEqual(self.pages("Gemini notes fetch failed"), [])

        query_err = "gemini-notes: query failed: later query problem"
        second = self.run_refresh(self.mirror, GEMINI_RCLONE_REMOTE="drive-test:",
                                  GEMINI_NOTES_RC="1", GEMINI_NOTES_STDERR=query_err)
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn(query_err, second.stdout)
        self.assertIn("granola-refresh: notes", second.stdout)
        self.assertEqual(len(self.pages("Gemini notes: some docs failed")), 1,
                         "no export-failed lines disarm the document alarm")
        self.assertEqual(len(self.pages("Gemini notes fetch failed")), 1, self.ntfy_calls())
        self.assertFalse(os.path.exists(os.path.join(self.state, "granola-alert-gemini-docs")))
        self.assertTrue(os.path.exists(os.path.join(self.state, "granola-alert-gemini")))

    def test_meet_failure_pages_the_first_line_and_clears_when_absent(self):
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        first_line = "gemini-notes: meet failed: synthetic list failure"
        stderr = first_line + "\ngemini-notes: meet failed rec-123: conference failure"
        first = self.run_refresh(self.mirror, GEMINI_RCLONE_REMOTE="drive-test:",
                                 GEMINI_NOTES_RC="2", GEMINI_NOTES_STDERR=stderr)
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertIn(stderr, first.stdout)
        meet_pages = self.pages("Gemini notes: Meet transcripts failed")
        self.assertEqual(len(meet_pages), 1, self.ntfy_calls())
        self.assertIn(first_line, meet_pages[0])
        self.assertNotIn("conference failure", meet_pages[0])

        healthy = self.run_refresh(self.mirror, GEMINI_RCLONE_REMOTE="drive-test:",
                                   GEMINI_NOTES_RC="0", GEMINI_NOTES_STDERR="")
        self.assertEqual(healthy.returncode, 0, healthy.stdout)
        self.assertIn("granola-refresh: notes", healthy.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.state, "granola-alert-gemini-meet")))
        again = self.run_refresh(self.mirror, GEMINI_RCLONE_REMOTE="drive-test:",
                                 GEMINI_NOTES_RC="2",
                                 GEMINI_NOTES_STDERR="gemini-notes: meet failed rec-456: retry")
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("granola-refresh: notes", again.stdout)
        self.assertEqual(len(self.pages("Gemini notes: Meet transcripts failed")), 2,
                         self.ntfy_calls())

    def test_commit_includes_gemini_files_and_supersede_deletion(self):
        git_env = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        gemini = os.path.join(self.ws, "meetings", "gemini")
        old_mirror = os.path.join(gemini, "2026-08-01-old-gem_old123.md")
        old_note = os.path.join(self.notes, "2026-08-01-old-gem_old123.note.md")
        os.makedirs(gemini, exist_ok=True)
        with open(old_mirror, "w") as f:
            f.write("old mirror\n")
        with open(old_note, "w") as f:
            f.write("old Gemini-only note\n")
        with open(os.path.join(self.mirror, ".keep"), "w") as f:
            f.write("tracked mirror directory\n")
        subprocess.run(("git", "init", "-q", self.ws), check=True)
        subprocess.run(("git", "-C", self.ws, "add", "granola", "meetings/gemini", "meetings/notes"), check=True)
        subprocess.run(("git", "-C", self.ws, "commit", "-qm", "seed"), check=True,
                       env=dict(os.environ, **git_env))

        os.unlink(old_mirror)
        os.unlink(old_note)
        new_mirror = os.path.join(gemini, "2026-08-19-team-sync-gem_doc123.md")
        new_note = os.path.join(self.notes, "2026-08-19-team-sync-gem_doc123.note.md")
        with open(new_mirror, "w") as f:
            f.write("new mirror\n")
        with open(new_note, "w") as f:
            f.write("new Gemini-only note\n")
        manual = os.path.join(self.notes, "hand-written.note.md")
        with open(manual, "w") as f:
            f.write("leave this untracked\n")

        r = self.run_refresh("--commit", self.mirror, GEMINI_RCLONE_REMOTE="drive-test:", **git_env)
        self.assertEqual(r.returncode, 0, r.stdout)
        shown = subprocess.run(("git", "-C", self.ws, "show", "--name-status", "--format=", "HEAD"),
                               capture_output=True, text=True, check=True).stdout
        self.assertIn("A\tmeetings/gemini/2026-08-19-team-sync-gem_doc123.md", shown)
        self.assertIn("D\tmeetings/gemini/2026-08-01-old-gem_old123.md", shown)
        self.assertIn("A\tmeetings/notes/2026-08-19-team-sync-gem_doc123.note.md", shown)
        self.assertIn("D\tmeetings/notes/2026-08-01-old-gem_old123.note.md", shown)
        self.assertNotIn("hand-written.note.md", shown)


class EnvFileConfig(RefreshSandbox):
    def test_the_env_file_supplies_the_mirror_dir(self):
        """With no argument and no environment value, ~/.config/granola/env provides
        GRANOLA_MIRROR, so schedulers can invoke refresh.sh with no per-deployment path."""
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        cfg = os.path.join(self.home, ".config", "granola")
        os.makedirs(cfg, exist_ok=True)
        with open(os.path.join(cfg, "env"), "w") as f:
            f.write(f"GRANOLA_MIRROR={self.mirror}\nGEMINI_RCLONE_REMOTE=drive-test:\n")
        r = self.run_refresh(GRANOLA_MIRROR="", GEMINI_RCLONE_REMOTE="")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(self.note_path("2026-08-19-standup-not_aaa.md")), r.stdout)
        self.assertEqual(self.gemini_notes_calls_made(), [
            "sync %s --remote drive-test:" % os.path.join(self.ws, "meetings", "gemini")
        ])

    def test_the_env_file_workspace_applies_to_a_mirror_given_as_an_argument(self):
        """The webhook receiver passes the mirror as an argument, so the env file's
        GRANOLA_WORKSPACE must be read even then. With meetings/ its own repository in a
        plain workspace folder, --commit commits the mirror and the auto note there."""
        git_env = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        meetings = os.path.join(self.ws, "meetings")
        subprocess.run(("git", "init", "-q", meetings), check=True)
        new_mirror = os.path.join(meetings, "granola")
        os.rename(self.mirror, new_mirror)
        self.mirror = new_mirror
        self.mirror_file("2026-08-19-standup-not_aaa.md")
        cfg = os.path.join(self.home, ".config", "granola")
        os.makedirs(cfg, exist_ok=True)
        with open(os.path.join(cfg, "env"), "w") as f:
            f.write(f"GRANOLA_MIRROR={self.mirror}\nGRANOLA_WORKSPACE={self.ws}\n")
        r = self.run_refresh("--commit", self.mirror, **git_env)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(os.path.exists(self.note_path("2026-08-19-standup-not_aaa.md")), r.stdout)
        tracked = subprocess.run(("git", "-C", meetings, "ls-files"),
                                 capture_output=True, text=True, check=True).stdout.split()
        self.assertIn("granola/2026-08-19-standup-not_aaa.md", tracked, r.stdout)
        self.assertIn("notes/2026-08-19-standup-not_aaa.note.md", tracked, r.stdout)

    def test_no_path_anywhere_still_refuses(self):
        r = self.run_refresh(GRANOLA_MIRROR="")
        self.assertEqual(r.returncode, 2, r.stdout)


if __name__ == "__main__":
    unittest.main()
