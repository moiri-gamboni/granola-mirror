#!/usr/bin/env bash
# notes.sh — write a structured meeting note for each mirrored Granola meeting that
# has a verbatim transcript, following the meetings skill (skills/meetings/SKILL.md
# in this repository). Notes land in <workspace>/meetings/notes/<basename>.note.md,
# alongside the session-written notes — same procedure, unattended; the generator
# banner on line 1 and the granola basename (…-not_<id>) carry the provenance.
#
# Version-addressed, not mtime-addressed. The banner records the source version the
# note was generated from (`source-updated-at:`, the mirror's `granola updated_at`)
# and a hash of the note's own body (`body-sha256:`). Every decision is a comparison
# of those recorded values:
#   - banner version == mirror version, body hash matches  -> current (skip)
#   - version differs, banner+hash intact                  -> regenerate
#   - body hash != banner hash                             -> a hand edit -> HELD (kept)
#   - banner absent / unparseable                          -> HELD + alarm-armed
#   - transcript stamped against an older version than the header -> incoherent -> not generated
# Fail-closed: any value that won't parse resolves to HELD, never to a silent overwrite.
# `--force` is the sole sanctioned override of a hold (and clears the wedge marker).
#
# Security posture: the `claude -p` call is a pure stdin->stdout transform over untrusted
# transcript content, so it runs with NO tools — `--tools ""` disables the built-in set and
# `--strict-mcp-config` with no `--mcp-config` keeps the user's configured MCP servers from
# loading. `--tools ""` alone would still leave the MCP tools reachable, so both flags are
# required; together they close the prompt-injection egress path.
#
#   notes.sh DIR [FILE...] [--since YYYY-MM-DD] [--force]
#   notes.sh --hash NOTEFILE     print the sha256 of a note's body (banner+blank stripped);
#                                the ONE body-hash implementation, also called by migrate-banners.py
#
#   FILE...   process just these mirror files (testing / manual reprocess): bypasses the
#             floor and the settle/coherence gates (the human named it), but STILL honors a
#             hold — a hand-edited note is not clobbered without --force
#   --since   only meetings whose filename date is >= this (default: the first-run date,
#             persisted in ~/.local/state/granola-notes-since — the pre-existing backlog is
#             deliberately excluded; backfill is an explicit --since)
set -uo pipefail
# readlink -f: resolve through symlinks so the skill is found in the clone even when
# invoked via a ~/bin symlink.
SELF_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

# --- the ONE body-hash implementation ---------------------------------------
# A note is `<banner line>\n<blank>\n<body...>`; the hash covers exactly the body.
# The writer hashes a staged file of the same shape, so writer and reader agree byte
# for byte, and migrate-banners.py calls `notes.sh --hash` rather than reimplementing it.
hash_note_file() { sed '1,2d' "$1" | sha256sum | cut -d' ' -f1; }

if [ "${1:-}" = "--hash" ]; then
  [ -n "${2:-}" ] && [ -f "$2" ] || { echo "usage: notes.sh --hash NOTEFILE" >&2; exit 2; }
  hash_note_file "$2"; exit 0
fi

SINCE="" FORCE=0 DIR="" FILES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    *) if [ -z "$DIR" ]; then DIR="$1"; else FILES+=("$1"); fi; shift ;;
  esac
done
if [ -z "$DIR" ] || [ ! -d "$DIR" ]; then
  echo "usage: notes.sh DIR [FILE...] [--since YYYY-MM-DD] [--force]" >&2; exit 2
fi
DIR="$(cd "$DIR" && pwd)"
# Workspace root — the git toplevel of the mirror dir, so the mirror may sit anywhere
# inside the workspace repo; non-git deployments fall back to the mirror dir's parent.
BASE="$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null || dirname "$DIR")"
NOTES_DIR="$BASE/meetings/notes"
STATE="$HOME/.local/state"; mkdir -p "$STATE" "$NOTES_DIR"
WEDGE_DIR="$STATE/granola-note-wedge"
HELD_PREFIX="$STATE/granola-note-held-"
RUN_JSON="$STATE/granola-notes-run.json"
# Rejected model output is kept here, not deleted: a failed generation's stdout/stderr is
# the only evidence of WHY it failed, and a reject branch that tidies its temp files
# away leaves the failure undiagnosable. 180-day retention.
REJECTS="$STATE/granola-rejects"; mkdir -p "$REJECTS"
find "$REJECTS" -type f -mtime +180 -delete

# Pipeline lock. refresh.sh owns the run and holds ~/.locks/granola-pipeline; it exports
# GRANOLA_LOCK_HELD=1 so this notes.sh (its child) does not re-acquire. A STANDALONE
# notes.sh (a manual FILE/backfill run) acquires the same lock NON-BLOCKING and refuses on
# contention rather than stalling behind a running pipeline for up to 3h — the residual on a
# refuse is only a deferred manual run, never data loss.
# (Soft spot, documented not guarded: a debug shell that exports GRANOLA_LOCK_HELD disables
# this self-lock; the worst case under mktemp scratch + atomic note write is duplicated model
# spend on one note, not corruption.)
if [ -z "${GRANOLA_LOCK_HELD:-}" ]; then
  LOCKS="$HOME/.locks"; mkdir -p "$LOCKS"
  exec {LFD}>"$LOCKS/granola-pipeline"
  if ! flock -n "$LFD"; then
    echo "notes.sh: granola pipeline is running — try again shortly" >&2
    exit 75
  fi
fi

# The procedure is load-bearing: a missing skill file silently degrades every note,
# so fail loudly instead of skipping (same stance as granola-digest).
PROC="$SELF_DIR/skills/meetings/SKILL.md"
[ -f "$PROC" ] || { echo "notes.sh: meeting procedure missing at $PROC" >&2; exit 1; }
WF="$BASE/workflows/meetings"

# Default SINCE: persisted first-run date, so the cron only ever sees new
# meetings and the historical mirror is not a surprise 100+-note backfill.
SINCE_FILE="$STATE/granola-notes-since"
if [ -z "$SINCE" ]; then
  if [ -f "$SINCE_FILE" ]; then
    SINCE="$(cat "$SINCE_FILE")"
  else
    SINCE="$(date +%F)"
    echo "$SINCE" > "$SINCE_FILE"
    echo "notes.sh: first run — noting meetings from $SINCE on ($SINCE_FILE)"
  fi
fi

# --- version / banner accessors (empty on any parse failure -> fail-closed) --
src_version() {   # mirror file's `granola updated_at` — the source version
  local m; m=$(grep -m1 -oE 'granola updated_at: [^ ]+' "$1" 2>/dev/null) || true
  printf '%s' "${m#granola updated_at: }"
}
tmark_version() { # transcript's coherence marker; empty = grandfathered = coherent
  local m; m=$(grep -m1 -oE 'transcript for updated_at: [^ ]+' "$1" 2>/dev/null) || true
  printf '%s' "${m#transcript for updated_at: }"
}
banner_field() {  # value of a key-anchored field on the note's banner line (line 1)
  sed -n '1p' "$1" 2>/dev/null | grep -m1 -oE "$2: [^ ]+" | sed "s/^$2: //" | head -1
}

has_transcript() {   # a '## Transcript' section with non-blank content after it
  awk '/^## Transcript$/{f=1;next} f&&NF{ok=1;exit} END{exit !ok}' "$1"
}
settled() {   # summary present in the HEADER portion (before '## Transcript'), matching
              # granola-transcripts' NO_SUMMARY test — a placeholder there means a live
              # meeting; a speaker saying it inside the transcript must not count.
  ! sed -n '/^## Transcript$/q;p' "$1" | grep -qF '_(no summary)_'
}

wedge_bump() {   # $1 basename, $2 failed source version
  mkdir -p "$WEDGE_DIR"
  local wf="$WEDGE_DIR/$1.json" streak=0
  [ -f "$wf" ] && streak=$(grep -oE '"streak": *[0-9]+' "$wf" | grep -oE '[0-9]+' | head -1)
  [ -n "$streak" ] || streak=0
  streak=$((streak+1))
  printf '{"streak": %d, "failed_version": "%s", "last_at": "%s"}\n' \
    "$streak" "$2" "$(date -Is)" > "$wf"
}
wedge_clear() { rm -f "$WEDGE_DIR/$1.json"; }
held_mark()   { : > "$HELD_PREFIX$1"; }
held_clear()  { rm -f "$HELD_PREFIX$1"; }

PROMPT="The meeting-note-extraction procedure is included below and is the spec for how to read this transcript — follow it in full: extraction priorities, nuance-preservation rules, reliability conventions, privacy (never paste a secret's value), what to drop, and scaling the note to the meeting's consequence (a standup gets a TL;DR + action items, not the full skeleton). Apply both transcript-corrections glossaries, also included: the reviewed tier may be applied silently; the auto tier per its own header (never silently — keep the original garble visible and flag reliance).

This is an unattended run (a cron), so it deviates from the procedure in exactly these ways:
- Output ONLY the note file body, in markdown, starting directly at the '# <Meeting title> — <YYYY-MM-DD>' heading. No preamble, no meta-commentary, no code fence around the note: your entire output is written verbatim to the note file.
- The procedure's 'surface judgment calls and open items to the human in chat' step has no chat here: put those judgment calls, each with a High/Med/Low confidence, in the note's 'Sources & reliability' section instead.
- Do not edit any file and do not propose glossary rows — the daily digest already proposes glossary additions for these same meetings. Unresolved garbles belong in 'Sources & reliability'.
- The meeting arrives as Granola's header + AI summary followed by the verbatim transcript. The summary is an UNRELIABLE hint — lossy and sometimes wrong; the transcript is the source of truth, so the note's claims must be supported by the transcript (header metadata like date/attendees may be used, remembering the procedure's caveat that invite-derived attendee lists overcount actual joiners)."

candidates=()
if [ ${#FILES[@]} -gt 0 ]; then
  candidates=("${FILES[@]}")
else
  # Mirror filenames start with the meeting date (YYYY-MM-DD-...), so a plain
  # lexical compare against SINCE is a date filter.
  for f in "$DIR"/*.md; do
    [ -e "$f" ] || continue
    [[ "$(basename "$f")" < "$SINCE" ]] && continue
    candidates+=("$f")
  done
fi

written=0 current=0 missing=0 settling=0 failed=0 attempted=0
unstamped=0 held_incoherent=0 held_edited=0 held_unparseable=0
FIRST_ERR=""
for f in "${candidates[@]}"; do
  [ -f "$f" ] || { echo "notes.sh: no such file: $f" >&2; failed=$((failed+1)); continue; }
  fbase="$(basename "$f" .md)"
  note="$NOTES_DIR/$fbase.note.md"
  src="$(src_version "$f")"
  if [ -z "$src" ]; then unstamped=$((unstamped+1)); continue; fi   # never write a broken note

  # An existing note is inspected regardless of FILE/floor, so a hand edit is honored
  # even when the file is named explicitly. --force is the only bypass.
  if [ "$FORCE" -eq 0 ] && [ -f "$note" ]; then
    bsrc="$(banner_field "$note" 'source-updated-at')"
    bhash="$(banner_field "$note" 'body-sha256')"
    if [ -z "$bsrc" ] || [ -z "$bhash" ]; then
      held_mark "$fbase"; held_unparseable=$((held_unparseable+1)); continue   # alarm class
    fi
    if [ "$(hash_note_file "$note")" != "$bhash" ]; then
      held_edited=$((held_edited+1)); continue   # deliberate hand edit -> counter, no alarm
    fi
    if [ "$bsrc" = "$src" ]; then current=$((current+1)); continue; fi
    # version differs, banner+hash intact -> fall through to regenerate
  fi

  # Settle + coherence gates — skipped for an explicit FILE arg (the human named it).
  if [ ${#FILES[@]} -eq 0 ]; then
    if ! settled "$f"; then settling=$((settling+1)); continue; fi
  fi
  if ! has_transcript "$f"; then missing=$((missing+1)); continue; fi
  if [ ${#FILES[@]} -eq 0 ]; then
    tm="$(tmark_version "$f")"
    if [ -n "$tm" ] && [ "$tm" != "$src" ]; then
      held_incoherent=$((held_incoherent+1)); continue   # mismatched pair — never fabricate
    fi
  fi

  echo "[$(date -Is)] notes.sh: $(basename "$f") (source-updated-at: $src)"
  attempted=$((attempted+1))
  tmp="$(mktemp "$STATE/granola-note.XXXXXX")" err="$(mktemp "$STATE/granola-note-err.XXXXXX")"
  {
    echo "# Procedure: how to extract a note from a meeting transcript (follow this)"
    cat "$PROC"
    echo
    echo "# Glossary: transcript corrections, reviewed tier (garble -> correct form; apply these)"
    [ -f "$WF/transcript-corrections.md" ] && cat "$WF/transcript-corrections.md"
    echo
    echo "# Glossary: auto tier (unreviewed proposals; treat per its header)"
    [ -f "$WF/transcript-corrections-auto.md" ] && cat "$WF/transcript-corrections-auto.md"
    echo
    echo "# The meeting: Granola header + AI summary (UNRELIABLE, lossy), then the verbatim transcript (source of truth)"
    cat "$f"
  } | CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000 timeout 3600 claude -p --model claude-sonnet-5 --effort xhigh \
        --tools "" --strict-mcp-config "$PROMPT" > "$tmp" 2>"$err"
  # 128000 = sonnet-5's actual output cap; Claude Code's 64k default is half that, and a long
  # meeting at high effort exceeds it (thinking counts toward output tokens).
  rc=$?
  if [ "$rc" -ne 0 ] || [ ! -s "$tmp" ] || [ "$(head -c1 "$tmp")" != "#" ]; then
    if [ "$rc" -ne 0 ]; then why="rc=$rc"
    elif [ ! -s "$tmp" ]; then why="empty output"
    else why="output does not start with '#'"
    fi
    keep="$REJECTS/$(date +%Y%m%dT%H%M%S)-$fbase"
    mv -f "$tmp" "$keep.out"; mv -f "$err" "$keep.err"
    echo "notes.sh: claude failed on $(basename "$f") ($why): $(head -c 300 "$keep.err") — rejected output kept at $keep.out" >&2
    [ -z "$FIRST_ERR" ] && FIRST_ERR="$why; kept $keep.out; stderr: $(head -c 300 "$keep.err")"
    wedge_bump "$fbase" "$src"
    failed=$((failed+1)); continue
  fi
  # Hash the body via the SAME implementation the reader uses: stage banner-shaped, hash.
  staged="$(mktemp "$STATE/granola-note-stg.XXXXXX")"
  { printf 'x\n\n'; cat "$tmp"; } > "$staged"
  bodyhash="$(hash_note_file "$staged")"; rm -f "$staged"
  banner="<!-- auto-generated $(date +%F) by granola-mirror/notes.sh from granola/$(basename "$f") — source-updated-at: $src body-sha256: $bodyhash — unattended extraction; judgment calls flagged in Sources & reliability -->"
  # Atomic publish: assemble into a temp beside $note, then mv. A SIGKILL mid-write
  # (the documented GRANOLA_LOCK_HELD soft-spot) then leaves the old note intact
  # rather than a truncated one — the "never corruption" guarantee the headers assert.
  npub="$(mktemp "$(dirname "$note")/.granola-note.XXXXXX")"
  { printf '%s\n\n' "$banner"; cat "$tmp"; } > "$npub"
  mv -f "$npub" "$note"
  rm -f "$tmp" "$err"
  wedge_clear "$fbase"; held_clear "$fbase"   # a successful write resolves any prior hold/wedge
  written=$((written+1))
done

echo "notes.sh: $written written, $current current, $missing awaiting transcript, $settling settling, $failed failed, held: $held_incoherent incoherent / $held_edited edited / $held_unparseable unparseable, $unstamped unstamped -> $NOTES_DIR"

# Machine-readable run summary — the notes.sh -> refresh.sh contract for the run-level
# alarm. The wedge ledger and held markers are read directly by refresh.sh; only the
# counts and the first failure's stderr need passing, and FIRST_ERR goes via env so its
# quotes/newlines can't break the JSON.
export FIRST_ERR
python3 - "$attempted" "$written" "$failed" "$current" "$missing" "$settling" \
         "$unstamped" "$held_incoherent" "$held_edited" "$held_unparseable" > "$RUN_JSON" <<'PY'
import json, os, sys
k = ["attempted", "written", "failed", "current", "missing", "settling",
     "unstamped", "held_incoherent", "held_edited", "held_unparseable"]
d = {name: int(v) for name, v in zip(k, sys.argv[1:])}
d["first_error"] = os.environ.get("FIRST_ERR", "")
print(json.dumps(d))
PY

[ "$failed" -eq 0 ]
