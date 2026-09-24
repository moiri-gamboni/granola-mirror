# granola-mirror

Keeps a local markdown copy of your Granola meeting notes, with their verbatim transcripts, in a git workspace, and writes a structured note for each meeting unattended: decisions, action items, open questions and the context a later reader needs, extracted by Claude Code from the transcript. The same extraction procedure ships as a Claude Code skill, `/granola-mirror:meetings`, for writing or revising a note in a session. The code lives here; the mirror and the notes live in the workspace you point it at.

Requirements: Python 3 (standard library only), bash, `flock`, git, and a logged-in [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI (`claude`) for the notes. Transcripts need Granola's MCP OAuth access; the webhook trigger needs Granola webhooks (Business plan).

## Set up

1. Clone this repository. The scripts find their siblings through symlinks, so you may link them onto your PATH (for example `~/bin/granola-refresh -> refresh.sh`).
2. Put a Granola public-API key (`grn_…`, from Granola's account settings, https://docs.granola.ai) at `~/.config/granola/api-key`, mode 0600.
3. Pick a mirror directory inside the workspace repository, for example `<workspace>/meetings/granola`, and write it to `~/.config/granola/env`:

   ```sh
   GRANOLA_MIRROR=/path/to/workspace/meetings/granola
   ```

   Then `granola sync <mirror-dir>` fills it with one file per meeting (header and AI summary).
4. For transcripts, provide `~/.config/granola/mcp-tokens.json` (`access_token`, `refresh_token`) and `~/.config/granola/mcp-client.json` (`client_id`, `as` = authorization-server URL, `res` = resource), obtained through Granola's MCP OAuth flow. Nothing here performs that authorization; `granola-transcripts` refreshes the token from then on, and exits 3 when the refresh token itself has expired and you need to authorize again. `granola-transcripts sync <mirror-dir>` adds the transcripts.
5. Schedule the pipeline, for example in an `/etc/cron.d` file (drop the user field in a personal crontab). `claude` must be on the scheduler's PATH (cron's and systemd's defaults usually lack `~/.local/bin`), or every note generation fails:

   ```cron
   PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
   7 * * * *  you  /path/to/granola-mirror/refresh.sh --commit           2>&1 | logger -t granola-refresh
   30 6 * * * you  /path/to/granola-mirror/refresh.sh --commit --digest  2>&1 | logger -t granola-refresh
   ```

   The hourly line picks up new meetings; the daily `--digest` line additionally runs an optional digest command (see [Adding a digest](#adding-a-digest)). The runs serialize on one lock, so the lines need no `flock` of their own.
6. Optional, so a note lands minutes after a meeting ends instead of at the next hourly run: register a webhook endpoint once.

   ```sh
   curl -X POST https://public-api.granola.ai/v1/webhook-endpoints \
     -H "Authorization: Bearer $(cat ~/.config/granola/api-key)" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://<your hooks host>/granola", "scopes": ["personal", "public"]}'
   ```

   The response is the only place Granola shows the signing secret: save it (`whsec_…`) to `~/.config/granola/webhook-secret`, mode 0600, and keep the response for the record of the registered URL. Run `webhook_receiver.py` as a service (it listens on `127.0.0.1:8097`), with `claude` on its PATH, and route the public `/granola` path to it through a tunnel or reverse proxy. `GET /granola` answers `granola-webhook ok`, which checks the route. The receiver refuses to start without the secret or without a `GRANOLA_MIRROR` that is an existing directory.
7. Optional, for the skill in interactive sessions:

   ```sh
   claude plugin marketplace add /path/to/granola-mirror
   claude plugin install granola-mirror@granola-mirror
   ```

8. Optional, for failure alerts: run an [ntfy](https://ntfy.sh) server on `localhost:2586`. `refresh.sh` posts to topic `claude-<user>`, with a bearer token from `~/services/.ntfy-token` if that file exists. Without a server the alerts are dropped silently; the log still has them.

## What ends up in the workspace

The workspace is the git toplevel of the mirror directory, or the mirror directory's parent when it is not in a repository.

- **The mirror** (`$GRANOLA_MIRROR`): one `YYYY-MM-DD-<slug>-<note-id>.md` per meeting, holding Granola's header (with a `granola updated_at` version line), its AI summary, and a `## Transcript` section, one speaker turn per line, stamped with the meeting version it was fetched against.
- **`meetings/notes/<mirror-basename>.note.md`**: the generated notes. Line 1 is a generator banner. Notes you write by hand can sit in the same directory; `--commit` only commits generated ones (`*-not_*.note.md`, after Granola's `not_` note ids).
- **`workflows/meetings/transcript-corrections.md`** and **`transcript-corrections-auto.md`**: optional glossaries of transcription errors (garbled names and terms and their correct forms): established rows, and unconfirmed ones. Every note generation reads whichever exist, appends its meeting's proposed new rows to the auto tier if that file exists, and promotes auto-tier rows its meeting independently confirms into the reviewed tier or drops ones it contradicts; see the skill for how each tier is applied.
- **`updates/granola/`**: where a digest command writes its output, if you add one.

## How notes are kept in step with meetings

Each run looks at every mirrored meeting dated on or after the notes floor (the date `notes.sh` first ran, stored in `~/.local/state/granola-notes-since`). It spends a model call only when the meeting has ended (Granola has written a real summary, not its `_(no summary)_` placeholder), has a transcript, and the transcript was fetched against the meeting's current version. A transcript fetched mid-meeting would be partial, so a meeting still in progress waits, and a transcript that predates the latest edit waits for `granola-transcripts` to fetch it again. A transcript with no version stamp counts as current and is never fetched again.

The note's banner records the meeting version it was generated from (`source-updated-at:`) and a hash of the note's body (`body-sha256:`). On each run:

| The note | What happens |
|---|---|
| matches its banner, meeting unchanged | skipped |
| matches its banner, meeting changed since | regenerated |
| body no longer matches its hash (someone edited it) | kept as edited, counted in the run summary, no alert |
| banner missing or unreadable | kept, and alerted as a held note |

Short of `--force`, nothing is overwritten unless the pipeline can show it wrote the current text itself.

The model runs as `claude -p --model claude-sonnet-5 --effort xhigh` (one-hour limit) with no tools at all (`--tools "" --strict-mcp-config`), because the transcript is untrusted input. It is given `skills/meetings/SKILL.md` from this clone, both glossaries and the meeting file. Its output is the note, then a `<!-- glossary-additions -->` line and the glossary changes, which never reach the note: new rows are appended to the auto tier under a heading naming the date and meeting; a `promote: <row>` line moves that exact auto-tier row to the reviewed tier's *Promoted from the auto tier* section, and `drop: <row>` deletes it. A promote or drop whose row is not exactly one line of the auto tier changes nothing and is counted as `glossary_misses` in the run summary. Output without exactly one marker line, or with a heading after it, counts as a failed generation. The skill's *Without a chat* section is what an unattended run follows: judgment calls go into the note's *Sources & reliability* section instead of a conversation. Editing the skill changes both the unattended notes and the interactive skill.

To work with the notes:

- **Correct a note**: edit it and leave line 1 in place. The pipeline keeps your version from then on.
- **Discard your edits, or regenerate one meeting**: `notes.sh <mirror-dir> <mirror-file> --force`.
- **Note one meeting now**: `notes.sh <mirror-dir> <mirror-file>`. Naming files skips the floor, the ended check and the current-transcript check; a note that is current or hand-edited is still left alone.
- **Note meetings from before the first run**: `notes.sh <mirror-dir> --since 2026-03-01`.

A `notes.sh` run by hand while the pipeline is running exits with code 75 instead of waiting; run it again when the pipeline is done.

## Monitoring

Every trigger logs under one tag, `journalctl -t granola-refresh`, as long as the cron lines pipe to `logger` as above (the webhook runner does). `~/.local/state/granola-webhook-events.jsonl` gets one line per verified webhook delivery, so a recent line means deliveries are arriving. When a generation fails, the model's output and error are kept under `~/.local/state/granola-rejects/` for 180 days; the log line names the file and which check failed.

`refresh.sh` sends each alert once per occurrence: again only after the condition has cleared and returned, except where the table says it repeats. A `notes.sh` run by hand never alerts.

| Alert | Meaning | What to do |
|---|---|---|
| Granola MCP re-auth needed | the transcript OAuth refresh token expired; summaries still update | authorize again to rewrite `mcp-tokens.json` (repeats every 3 days until fixed) |
| every generation failed | every note attempted this run failed; the alert carries the first error | usually `claude` is not on PATH, its login expired, or its usage limit is reached |
| note wedged | one meeting failed 3 or more runs in a row while others succeeded | read its files in `granola-rejects/` (repeats daily while stuck) |
| note held | a note's banner is missing or unreadable | restore the banner, or `notes.sh <mirror-dir> <mirror-file> --force` |
| notes did not run | `notes.sh` stopped before processing anything: the skill file or the mirror directory is missing | check the clone and `GRANOLA_MIRROR` |
| digest failed | the digest command exited non-zero; its pending list is kept for the next run | see the digest's own log |
| pipeline lock timeout | a run waited `GRANOLA_LOCK_WAIT` seconds (default 3 hours) for the lock | a run is stuck or the machine is overloaded |

## Adding a digest

`refresh.sh --digest` runs `granola-digest <mirror-dir> <pending-file>` if a command of that name is on PATH, and skips the step otherwise; no digest command ships with this repository. The pending file lists every mirror file that changed since the last successful digest, one path per line, and is emptied when the command exits 0. It runs before the notes step. A digest that writes its output under `<workspace>/updates/granola/` gets it committed by `--commit`.

## Reference

### Commands

`granola` and `granola-transcripts` print their full usage with `--help`; `notes.sh` and `refresh.sh` carry it in their header comments.

- **`granola folders | notes | get <id> | sync DIR`**: Granola's public API (summaries only; transcripts are not available through it). `sync` rewrites only meetings whose `updated_at` changed, keeps their transcript sections, and stays under Granola's 5 requests per second limit.
- **`granola-transcripts sync DIR | get <uuid> | reformat DIR`**: transcripts through Granola's MCP. `sync` fetches only missing or outdated transcripts and backs off when Granola rate-limits it; `reformat` re-splits fetched transcripts locally. Exit 3: the OAuth refresh token expired.
- **`refresh.sh [--commit] [--digest] [mirror-dir]`**: the pipeline: summaries, transcripts, the digest (with `--digest`), notes, then (with `--commit`) a commit of the mirror, the generated notes, `updates/granola/` and the auto glossary tier (in whichever repository holds it), leaving anything else staged untouched. The mirror comes from the argument, then `GRANOLA_MIRROR`, then `~/.config/granola/env`.
- **`notes.sh DIR [FILE...] [--since YYYY-MM-DD] [--force]`**: the notes step (see [How notes are kept in step with meetings](#how-notes-are-kept-in-step-with-meetings)). `notes.sh --hash NOTEFILE` prints a note's body hash.
- **`webhook_receiver.py`**: verifies Granola's signed webhook events (`note.generated`, `note.edited`, `note.access_granted`, `note.regenerated`) and runs `refresh.sh --commit` for the whole mirror, coalescing bursts of events into one run.
- **`migrate-banners.py MIRROR_DIR [--floor YYYY-MM-DD] [--dry-run]`**: a one-time upgrade for a deployment whose generated notes predate the version and hash fields in the banner. A new deployment never needs it.

### Configuration

| File | Read by | Contents |
|---|---|---|
| `~/.config/granola/env` | `refresh.sh`, `webhook_receiver.py` | shell-sourceable `KEY=VALUE` lines; the one key read is `GRANOLA_MIRROR` |
| `~/.config/granola/api-key` | `granola` | the `grn_` public-API key, mode 0600 |
| `~/.config/granola/mcp-tokens.json` | `granola-transcripts` | MCP OAuth `access_token` and `refresh_token`; rewritten on every refresh |
| `~/.config/granola/mcp-client.json` | `granola-transcripts` | OAuth client record: `client_id`, `as`, `res` |
| `~/.config/granola/webhook-secret` | `webhook_receiver.py` | the webhook signing secret (`whsec_…`), mode 0600 |
| `~/services/.ntfy-token` | `refresh.sh` | optional ntfy bearer token |

| Variable | Read by | Meaning |
|---|---|---|
| `GRANOLA_MIRROR` | `refresh.sh`, `webhook_receiver.py` | the mirror directory, if not given as an argument; the receiver reads the environment, then the env file |
| `GRANOLA_LOCK_WAIT` | `refresh.sh` | seconds to wait for the pipeline lock before alerting; default `10800` |
| `GRANOLA_LOCK_HELD` | `notes.sh` | set to `1` by `refresh.sh` for the `notes.sh` it runs, which then skips taking the lock itself |

### State

Under `~/.local/state/` unless noted:

| Path | Written by | Role |
|---|---|---|
| `granola-changed.txt` | `granola sync` | the last run's changed meetings |
| `granola-digest-pending.txt` | `refresh.sh` | changed meetings since the last successful digest |
| `granola-notes-since` | `notes.sh` | the notes floor date |
| `granola-notes-run.json` | `notes.sh` | the run summary `refresh.sh` alerts from |
| `granola-note-wedge/<basename>.json` | `notes.sh` | a meeting's consecutive-failure count |
| `granola-note-held-<basename>` | `notes.sh` | marks a note held for an unreadable banner |
| `granola-rejects/` | `notes.sh` | failed model output, 180 days |
| `granola-alert-*`, `granola-wedge-alerted-*`, `granola-held-alerted-*`, `granola-oauth-alerted` | `refresh.sh` | which alerts have been sent |
| `granola-webhook-pending`, `granola-webhook-events.jsonl` | `webhook_receiver.py` | events awaiting a run; the log of verified events |
| `~/.locks/granola-pipeline` | `refresh.sh`, `notes.sh` | the pipeline lock |
| `~/.locks/granola-webhook-runner` | `webhook_receiver.py` | keeps one webhook-triggered run at a time |
