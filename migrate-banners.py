#!/usr/bin/env python3
"""migrate-banners.py — one-time stamp migration for the version-addressed note model.

Run once on the live box after the version-model notes.sh lands. Two local loops, no MCP:

  Loop A (hash-stamp): rewrite each existing auto note's banner to carry the version
    fields the model reads — `source-updated-at:` (the mirror's current `granola updated_at`)
    and `body-sha256:` (the note body's current hash). No regeneration: a hand-corrected
    note keeps its body and survives, and every existing note stops tripping the
    banner-unparseable hold (and its alarm) on the first version-model run. The hash comes
    from `notes.sh --hash` — the ONE body-hash implementation — never a reimplementation
    here, so one byte of divergence can't hold every note forever.

  Loop B (marker-stamp): for each mirror file at or above the stored floor that has a
    transcript but no coherence marker, insert `<!-- transcript for updated_at: <header> -->`.
    Floor-scoped deliberately: stamping a transcript makes it refetch-eligible when the note
    version later moves, which is wanted for the active at-floor set but not for the
    historical below-floor files — a Granola mass re-summarization would otherwise queue a
    refetch of the whole history against a slow-refill bucket. The below-floor set stays grandfathered
    (marker-free = coherent, never refetched). A later `--since` backfill re-runs Loop B with
    a wider `--floor`.

  migrate-banners.py MIRROR_DIR [--floor YYYY-MM-DD] [--notes-dir DIR]
                                [--notes-sh PATH] [--dry-run]
"""
import argparse
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
UPDATED_RE = re.compile(r"granola updated_at: (\S+)")          # mirror source version
TMARK_RE = re.compile(r"<!-- transcript for updated_at: \S+ -->")
BANNER_STAMPED_RE = re.compile(r"source-updated-at: \S+.*body-sha256: [0-9a-f]+")
MARK = "\n## Transcript\n"


def source_version(body):
    m = UPDATED_RE.search(body)
    return m.group(1) if m else ""


def body_hash(note_sh, note_path):
    """Hash a note's body through notes.sh --hash — the single shared implementation."""
    r = subprocess.run(("bash", note_sh, "--hash", note_path),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError("notes.sh --hash failed on %s: %s" % (note_path, r.stderr.strip()))
    return r.stdout.strip()


def loop_a(mirror, notes_dir, note_sh, dry):
    """Hash-stamp existing auto notes. Returns (stamped, already, skipped)."""
    stamped = already = skipped = 0
    for note in sorted(glob.glob(os.path.join(notes_dir, "*-not_*.note.md"))):
        with open(note) as f:
            lines = f.read().splitlines(keepends=True)
        if not lines:
            skipped += 1
            continue
        banner = lines[0]
        if BANNER_STAMPED_RE.search(banner):
            already += 1
            continue
        base = os.path.basename(note)[: -len(".note.md")]
        mirror_file = os.path.join(mirror, base + ".md")
        if not os.path.exists(mirror_file):
            print("  loop A: no mirror file for %s — skipped" % os.path.basename(note))
            skipped += 1
            continue
        with open(mirror_file) as f:
            ver = source_version(f.read())
        if not ver:
            print("  loop A: %s has no source version — skipped" % os.path.basename(mirror_file))
            skipped += 1
            continue
        h = body_hash(note_sh, note)
        # Insert the two key-anchored fields before the banner's closing `-->`, preserving
        # the human-readable provenance text. The body is untouched, so the recorded hash
        # stays valid.
        stripped = banner.rstrip()
        assert stripped.endswith("-->"), "unexpected banner shape: %r" % banner
        newline = stripped[:-3].rstrip() + " source-updated-at: %s body-sha256: %s -->\n" % (ver, h)
        if dry:
            print("  loop A would stamp %s (source-updated-at: %s)" % (os.path.basename(note), ver))
        else:
            lines[0] = newline
            with open(note, "w") as f:
                f.write("".join(lines))
        stamped += 1
    return stamped, already, skipped


def loop_b(mirror, floor, dry):
    """Marker-stamp at-floor grandfathered transcripts. Returns counts dict."""
    c = dict(stamped=0, already=0, below_floor=0, no_transcript=0, no_version=0)
    for mf in sorted(glob.glob(os.path.join(mirror, "*.md"))):
        if os.path.basename(mf) < floor:
            c["below_floor"] += 1
            continue
        with open(mf) as f:
            body = f.read()
        if MARK not in body:
            c["no_transcript"] += 1
            continue
        if TMARK_RE.search(body):
            c["already"] += 1
            continue
        ver = source_version(body)
        if not ver:
            c["no_version"] += 1
            continue
        idx = body.find(MARK) + len(MARK)
        new = body[:idx] + "\n<!-- transcript for updated_at: %s -->\n" % ver + body[idx:]
        if dry:
            print("  loop B would stamp %s (updated_at: %s)" % (os.path.basename(mf), ver))
        else:
            with open(mf, "w") as f:
                f.write(new)
        c["stamped"] += 1
    return c


def resolve_floor(arg):
    if arg:
        return arg
    since = os.path.expanduser("~/.local/state/granola-notes-since")
    if os.path.exists(since):
        with open(since) as f:
            return f.read().strip()
    return None


def main(argv):
    ap = argparse.ArgumentParser(prog="migrate-banners.py")
    ap.add_argument("mirror")
    ap.add_argument("--floor", default="")
    ap.add_argument("--notes-dir", default="")
    ap.add_argument("--notes-sh", default=os.path.join(HERE, "notes.sh"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    mirror = os.path.abspath(a.mirror)
    notes_dir = a.notes_dir or os.path.join(os.path.dirname(mirror), "meetings", "notes")

    stamped, already, skipped = loop_a(mirror, notes_dir, a.notes_sh, a.dry_run)
    print("migrate-banners: loop A — %d stamped, %d already, %d skipped" % (stamped, already, skipped))

    floor = resolve_floor(a.floor)
    if floor is None:
        print("migrate-banners: loop B skipped — no --floor and no stored granola-notes-since; "
              "pass --floor to marker-stamp a scoped set")
        return 0
    b = loop_b(mirror, floor, a.dry_run)
    print("migrate-banners: loop B (floor %s) — %d stamped, %d already, %d below-floor, "
          "%d no-transcript, %d no-version"
          % (floor, b["stamped"], b["already"], b["below_floor"], b["no_transcript"], b["no_version"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
