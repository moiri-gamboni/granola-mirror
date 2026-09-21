#!/usr/bin/env bash
# refresh.sh — keep a Granola notes mirror current, then derive what's new from it.
#   1. incremental summaries (public API key)   2. transcripts (OAuth MCP, missing only)
#   3. with --digest: granola-digest, if installed — an optional phone brief from a separate
#      tool, over everything accumulated since the last brief
#   4. notes.sh — a structured meeting note per transcript, via the meetings skill
# The fetch steps are idempotent and only touch new/changed notes, so a frequent cadence
# stays well under the MCP transcript rate limit: the cron runs this hourly so a note lands
# shortly after its transcript does, and once daily with --digest. The webhook receiver is
# the primary trigger; the cron is the fallback sweep. Each run appends its changed-note
# list to a pending file the digest consumes and clears, so frequent ticks don't starve the
# daily brief of coverage.
#
# One blocking lock (~/.locks/granola-pipeline) owns the whole run: every trigger source
# (cron tick, webhook kick, a manual run) serializes on it, so two runs never interleave a
# fetch and a note-write. refresh.sh acquires it, exports GRANOLA_LOCK_HELD=1 so the notes.sh
# it invokes doesn't re-acquire, and holds it across summaries -> transcripts -> digest ->
# notes -> commit. A wait longer than 3h is the only thing that pages as "lock timeout"
# (rc 75); every other failure is that step's own signal. (Soft spot, documented not
# guarded: a debug shell exporting GRANOLA_LOCK_HELD disables the notes.sh self-lock; the
# residual is duplicated model spend on one note under mktemp+atomic-write, never data loss.)
#
# refresh.sh owns ALL push alarms (so a manual notes.sh never pages): a notes step that
# never started (no run summary), a run where every attempted generation failed (carrying
# the first error verbatim), a single meeting wedged while others succeed, a held
# (unverifiable-banner) note, a failed digest, and the lock timeout. Plus the pre-existing
# MCP OAuth-expiry ntfy.
#
#   refresh.sh [--commit] [--digest] <mirror-dir>        (or set GRANOLA_MIRROR)
set -uo pipefail
# readlink -f: resolve through symlinks — ~/bin/granola-refresh points here, and the
# siblings must be found in the clone, not in ~/bin.
SELF_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
COMMIT=0 DIGEST=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --commit) COMMIT=1 ;;
    --digest) DIGEST=1 ;;
    *) ARGS+=("$a") ;;
  esac
done
DIR=""
[ ${#ARGS[@]} -gt 0 ] && DIR="${ARGS[0]}"
DIR="${DIR:-${GRANOLA_MIRROR:-}}"
# Per-deployment constants file: ~/.config/granola/env (KEY=VALUE, shell-sourceable)
# supplies GRANOLA_MIRROR when neither the argument nor the environment does, so
# schedulers can invoke this script with no deployment-specific path at all.
if [ -z "$DIR" ] && [ -f "$HOME/.config/granola/env" ]; then
  # shellcheck source=/dev/null
  . "$HOME/.config/granola/env"
  DIR="${GRANOLA_MIRROR:-}"
fi
[ -n "$DIR" ] || { echo "usage: granola-refresh [--commit] <mirror-dir>   (or set GRANOLA_MIRROR in the environment or ~/.config/granola/env)" >&2; exit 2; }
# Workspace root — where meetings/notes/, workflows/meetings/ and updates/granola live:
# the git toplevel of the mirror dir, so the mirror may sit anywhere inside the workspace
# repo. Non-git deployments fall back to the mirror dir's parent.
WS="$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null || dirname "$DIR")"
STATE="$HOME/.local/state"; mkdir -p "$STATE"
CHANGED="$STATE/granola-changed.txt"
PENDING="$STATE/granola-digest-pending.txt"
LOCKS="$HOME/.locks"; mkdir -p "$LOCKS"
LOCKFILE="$LOCKS/granola-pipeline"
LOCK_WAIT="${GRANOLA_LOCK_WAIT:-10800}"      # 3h; env seam exists so the rc-75 path is testable
RUN_JSON="$STATE/granola-notes-run.json"
WEDGE_DIR="$STATE/granola-note-wedge"
HELD_PREFIX="$STATE/granola-note-held-"

ntfy() {   # ntfy PRIORITY TITLE MESSAGE
  local tok=""
  [ -f "$HOME/services/.ntfy-token" ] && tok=$(cat "$HOME/services/.ntfy-token")
  curl -sS -m 10 -H "Priority: $1" -H "Title: $2" ${tok:+-H "Authorization: Bearer $tok"} \
    -d "$3" "http://localhost:2586/claude-$(whoami)" >/dev/null || true
}

# alert_once KEY PRIORITY TITLE MESSAGE — at most one ntfy per arming; the caller re-arms by
# calling `disarm KEY` when the condition clears, so a persistent fault doesn't spam but a
# recover-then-refail alerts again.
alert_once() {
  local st="$STATE/granola-alert-$1"
  [ -f "$st" ] && return 0
  ntfy "$2" "$3" "$4"
  : > "$st"
}
disarm() { rm -f "$STATE/granola-alert-$1"; }

# Commit just the mirror + today's brief + the auto meeting notes + the auto glossary
# tier (the file granola-digest appends its garble proposals to). Pathspec commits, so
# anything else already staged is left untouched. In a polyrepo layout workflows/ can be
# its own repo — the auto tier is committed in whichever repo it actually lives.
commit_mirror() {
  local repo updates auto autorepo
  # rev-parse is expected to fail when the mirror isn't in a repo — a supported setup
  if ! repo=$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null); then
    echo "  $DIR is not inside a git repo — skipping commit"
    return 0
  fi
  updates="$WS/updates/granola"
  auto="$WS/workflows/meetings/transcript-corrections-auto.md"
  autorepo=""
  # same expected-fail probe as above, for the auto tier's own location
  [ -f "$auto" ] && autorepo=$(git -C "$(dirname "$auto")" rev-parse --show-toplevel 2>/dev/null)
  local paths=("$DIR")
  # only include optional paths that actually have changes — a pathspec matching
  # nothing known to git makes `git commit -- <paths>` fail outright
  # The auto notes live alongside session-written ones in meetings/notes/; the
  # -not_<id> granola basename scopes the pathspec so a half-drafted session note
  # is never swept into a cron commit. Quoted: git expands the glob, not the shell.
  local autonotes; autonotes="$WS/meetings/notes/*-not_*.note.md"
  [ -d "$updates" ] && [ -n "$(git -C "$repo" status --porcelain -- "$updates")" ] && paths+=("$updates")
  [ -n "$(git -C "$repo" status --porcelain -- "$autonotes" 2>/dev/null)" ] && paths+=("$autonotes")
  [ "$autorepo" = "$repo" ] && [ -n "$(git -C "$repo" status --porcelain -- "$auto")" ] && paths+=("$auto")
  if [ -n "$(git -C "$repo" status --porcelain -- "${paths[@]}")" ]; then
    git -C "$repo" add -- "${paths[@]}"
    if git -C "$repo" commit -q -m "granola: mirror refresh $(date +%F)" -- "${paths[@]}"; then
      echo "  $(git -C "$repo" log --oneline -1)"
    else
      echo "  commit failed"
    fi
  else
    echo "  nothing to commit"
  fi
  if [ -n "$autorepo" ] && [ "$autorepo" != "$repo" ] && \
     [ -n "$(git -C "$autorepo" status --porcelain -- "$auto")" ]; then
    git -C "$autorepo" add -- "$auto"
    if git -C "$autorepo" commit -q -m "corrections: auto-tier proposals $(date +%F)" -- "$auto"; then
      echo "  $(git -C "$autorepo" log --oneline -1)"
    else
      echo "  auto-tier commit failed"
    fi
  fi
}

# One meeting stuck while others move — a genuine per-note problem, not the whole run down.
wedge_alarm() {
  local wf="$1" base astate prev_ver w_streak w_failed_version w_last_at
  base="$(basename "$wf" .json)"
  eval "$(python3 - "$wf" <<'PY'
import json, sys, shlex
d = json.load(open(sys.argv[1]))
for k in ("streak", "failed_version", "last_at"):
    print("w_%s=%s" % (k, shlex.quote(str(d.get(k, "")))))
PY
)"
  [ "${w_streak:-0}" -ge 3 ] || return 0
  astate="$STATE/granola-wedge-alerted-$base"
  # re-arm on source-version change OR alert-state age > 24h (a daily nag while stuck)
  if [ -f "$astate" ]; then
    prev_ver="$(sed -n '1p' "$astate")"
    if [ "$prev_ver" = "${w_failed_version:-}" ] && [ -n "$(find "$astate" -mtime -1 2>/dev/null)" ]; then
      return 0
    fi
  fi
  ntfy high "Granola note wedged" \
    "$base has failed ${w_streak} times (source ${w_failed_version:-?}, last ${w_last_at:-?}). It keeps retrying — check claude on PATH / OAuth / rate cap."
  printf '%s\n' "${w_failed_version:-}" > "$astate"
}

# A held note is one the pipeline refuses to touch because it can't verify the banner
# (absent/unparseable). One alert per hold episode; a --force resolution clears the marker
# and re-arms. A hash-mismatch (deliberate hand edit) is NOT here — it's a run counter.
held_alarms() {
  local m base astate
  for m in "$HELD_PREFIX"*; do
    [ -e "$m" ] || continue
    base="$(basename "$m")"; base="${base#granola-note-held-}"
    astate="$STATE/granola-held-alerted-$base"
    if [ ! -f "$astate" ]; then
      ntfy high "Granola note held" \
        "$base has an unverifiable banner and is held, not regenerated. Resolve with: notes.sh <mirror-dir> <mirror-file> --force (or fix the banner)."
      : > "$astate"
    fi
  done
  # reconcile: an alerted hold with no current marker was resolved -> re-arm
  for astate in "$STATE"/granola-held-alerted-*; do
    [ -e "$astate" ] || continue
    base="$(basename "$astate")"; base="${base#granola-held-alerted-}"
    [ -e "$HELD_PREFIX$base" ] || rm -f "$astate"
  done
}

# All the note-run alarms, read from notes.sh's run summary + the ledger/marker files.
run_alarms() {
  [ -f "$RUN_JSON" ] || return 0
  local attempted failed written first_error r_attempted r_failed r_written r_first_error
  eval "$(python3 - "$RUN_JSON" <<'PY'
import json, sys, shlex
d = json.load(open(sys.argv[1]))
for k in ("attempted", "failed", "written", "first_error"):
    print("r_%s=%s" % (k, shlex.quote(str(d.get(k, "")))))
PY
)"
  attempted="${r_attempted:-0}"; failed="${r_failed:-0}"; written="${r_written:-0}"
  first_error="${r_first_error:-}"
  # run-level: every ATTEMPTED generation failed (zero-attempt guard: an all-held / no-candidate
  # run never pages as a total failure). The first error names claude-not-on-PATH / OAuth / cap.
  if [ "$attempted" -gt 0 ] && [ "$attempted" -eq "$failed" ]; then
    alert_once total-failure high "Granola notes: every generation failed" \
      "All $attempted note generation(s) failed this run. First error: ${first_error:-<none>}"
  else
    disarm total-failure
  fi
  # per-meeting wedge — only when at least one other meeting succeeded, so an env-class outage
  # (which fails every note) pages once at the run level, not once per meeting.
  if [ "$written" -gt 0 ] && [ -d "$WEDGE_DIR" ]; then
    for wf in "$WEDGE_DIR"/*.json; do
      [ -e "$wf" ] || continue
      wedge_alarm "$wf"
    done
  fi
  held_alarms
}

pipeline() {
  echo "[$(date -Is)] granola-refresh: summaries -> $DIR"
  "$SELF_DIR/granola" sync "$DIR" --changed-file "$CHANGED"; local src=$?
  [ "$src" -eq 0 ] || echo "[$(date -Is)]   summaries fetch failed (rc=$src)."
  # Accumulate for the daily digest: each tick's changed list is appended (deduped)
  # and only a successful --digest run clears it, so frequent ticks can't starve the brief.
  if [ -s "$CHANGED" ]; then
    cat "$CHANGED" >> "$PENDING"
    sort -u -o "$PENDING" "$PENDING"
  fi

  echo "[$(date -Is)] granola-refresh: transcripts"
  "$SELF_DIR/granola-transcripts" sync "$DIR"; local rc=$?
  if [ "$rc" -eq 0 ]; then
    rm -f "$STATE/granola-oauth-alerted"
  elif [ "$rc" -eq 3 ]; then
    # MCP OAuth refresh token expired -> ntfy, at most once every 3 days
    if [ ! -f "$STATE/granola-oauth-alerted" ] || [ -z "$(find "$STATE/granola-oauth-alerted" -mtime -3)" ]; then
      ntfy urgent "Granola MCP re-auth needed" "OAuth token expired — transcripts paused (summaries still updating). Re-run the browser approval to restore transcripts."
      touch "$STATE/granola-oauth-alerted"
    fi
    echo "[$(date -Is)]   MCP token expired — re-auth needed (ntfy sent)."
  else
    echo "[$(date -Is)]   transcript step failed (rc=$rc)."
  fi

  # Optional personal digest (deployed from the private repo); daily (--digest) only,
  # and before the notes step so the phone brief isn't delayed behind long note runs.
  if [ "$DIGEST" -eq 1 ] && command -v granola-digest >/dev/null; then
    echo "[$(date -Is)] granola-refresh: digest"
    if granola-digest "$DIR" "$PENDING"; then
      : > "$PENDING"; disarm digest
    else
      echo "[$(date -Is)]   digest step failed."
      alert_once digest high "Granola digest failed" \
        "The daily phone brief failed; the pending list is retained so the next run retries with full coverage."
    fi
  fi

  echo "[$(date -Is)] granola-refresh: notes"
  # GRANOLA_LOCK_HELD: this refresh already holds the pipeline lock, so notes.sh must not
  # re-acquire it. Clear the prior run's summary first: if notes.sh exits before writing a
  # fresh one (skill missing, bad DIR), run_alarms must see "no run" and not a stale one it
  # could disarm the total-failure alarm from.
  rm -f "$RUN_JSON"
  GRANOLA_LOCK_HELD=1 "$SELF_DIR/notes.sh" "$DIR" || echo "[$(date -Is)]   notes step failed."
  # No summary at all means notes.sh refused before its first candidate (skill file or
  # mirror dir missing): run_alarms treats "no run" as nothing to report, so this is the
  # one place that pages for a notes step that never started.
  if [ -f "$RUN_JSON" ]; then
    disarm notes-prerun
  else
    alert_once notes-prerun high "Granola notes did not run" \
      "notes.sh exited before writing a run summary (skill file or mirror dir missing) — see journalctl -t granola-refresh"
  fi
  run_alarms

  if [ "$COMMIT" -eq 1 ]; then
    echo "[$(date -Is)] granola-refresh: commit"
    commit_mirror
  fi
  echo "[$(date -Is)] granola-refresh: done"
}

# Acquire the pipeline lock (blocking, up to LOCK_WAIT), then run. Only a real lock timeout
# (rc 75, the flock -E sentinel) pages; any failure inside pipeline() is that step's concern.
{
  flock -w "$LOCK_WAIT" -E 75 "$PLFD"; rc=$?      # capture directly — `if ! flock` resets $? to 0
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" -eq 75 ]; then
      echo "[$(date -Is)] granola-refresh: lock timeout after ${LOCK_WAIT}s — another run held it too long."
      alert_once lock-timeout urgent "Granola pipeline lock timeout" \
        "A refresh waited ${LOCK_WAIT}s for ~/.locks/granola-pipeline and gave up. A run is stuck or the box is overloaded."
    else
      echo "[$(date -Is)] granola-refresh: could not acquire lock (rc=$rc)."
    fi
    exit "$rc"
  fi
  disarm lock-timeout      # acquired cleanly -> re-arm the timeout alarm
  pipeline
} {PLFD}>"$LOCKFILE"
