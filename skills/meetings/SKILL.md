---
name: meetings
description: Use when turning a meeting transcript (Granola, Otter, a hand-recorded file) into a structured meeting note, writing or updating a meeting note, or asked what was decided or committed in a call.
---

# Meeting transcript → note

A note has two readers: the workspace's owner and future LLM sessions that load it as context. Where the two diverge, write for the model; the human still gets a better note.

Paths are relative to the workspace: `GRANOLA_WORKSPACE` when set, else the git toplevel of the meetings mirror. The corrections glossaries are `workflows/meetings/transcript-corrections.md` (human-reviewed) and `workflows/meetings/transcript-corrections-auto.md` (unreviewed proposals, appended by `notes.sh` with each unattended note).

## Core principles

1. **Self-containment.** The future reader has no ambient context: resolve every pronoun to a name, define jargon inline, name attendees with their role.
2. **Lossy on the conversation, lossless on the commitments.** Compress discussion freely; never compress an owner, date, number, decision, or a decision's *rationale*.
3. **Preserve the nuance that prevents over-acting on under-justified conclusions** (rules below).
4. **Scale to consequence.** A standup gets a TL;DR plus action items; a meeting that defines a whole project earns the full skeleton. The skeleton is a menu, not a checklist.

## Transcript reliability

No transcript is ground truth; the audio is, and you can't hear it. Transcripts garble low-frequency tokens (proper nouns, acronyms) into nearby common words and can drop whole exchanges.

- **Read both glossaries before extracting.** Reviewed-tier rows may be applied silently. An auto-tier row beats the raw garble but is never applied silently: keep the original garble visible at point of use (`Yusuf [transcript: "use if", auto Med]`) and flag any conclusion that relies on one, per the severity rules in the auto file's header.
- **Otherwise reconstruct from context, with a confidence, keeping the original garble.** Never silently "fix" a garbled name into the famous thing it rhymes with (one transcript's "Azure" was a small fiscal sponsor, not the cloud platform; "MADS" in a research meeting was a fellowship programme's acronym, not a product). An unresolved garble that matters needs checking against the audio.
- **A Granola file's AI summary is an unreliable hint**, lossy and sometimes wrong. The verbatim transcript is the source of truth: support the note's claims from it. Header metadata (date, attendees) is usable, but attendee lists come from calendar invites and overcount who actually joined.
- **Treat the Google Meet notes doc as a second, differently lossy capture.** Quick notes is Gemini's short AI summary; Full notes and “Next steps” are Gemini's AI output too. Attendees can edit Quick notes and Full notes. Treat them all as unreliable hints like Granola's summary. The Transcript tab labels speakers by Google account and timestamps to the minute; account names are reliable per device, but a shared device or room can collapse several people into one account. Granola labels turns by audio channel: `Me`/`Them` in current files, `Microphone`/`Speaker` in older files.
- **Prefer a non-empty Meet transcript section over the doc's Transcript tab.** When a Gemini file's `## Meet transcript` section has entries, read it instead of the doc's Transcript tab: it is Google's unedited recognition, timed per entry; line it up with Granola's timestamps.
- **Cross-read any second capture of the same meeting.** Find the other capture in `meetings/gemini/`, the Granola mirror, or another capture source. Start with the `- **Calendar event:** <id>` header in both: a shared id is the strongest evidence. The unattended pipeline pairs only when the ids match and the filename dates are within one day. When either file has no event id (an ad-hoc Meet without an invite, an older Granola file, or a recording from another tool), a file from the same date with a close start time and overlapping attendees may be the same meeting. Title is weak evidence because ad-hoc Meet docs can be titled “Meeting started <time>”. Cross-read only after a few distinctive exchanges appear in both, and record an inferred match with a confidence in *Sources & reliability*. Each tool drops different audio, so weigh content found in only one capture instead of dismissing it. Identical garbles in both are correlated, not independent; a clean rendering in one is evidence against the other's garble.

## Extract, in priority order

1. **Decisions** — what, who decided, **why**, rejected alternatives and why they lost, and how firm (settled / provisional / leaning).
2. **Action items / commitments** — owner, the thing, deadline, blocker. Split firm commitments from "someone should probably"; a "later / on request" bucket holds explicitly deferred items.
3. **Open questions** — raised but unresolved, and who owns resolving them.
4. **Durable context** — constraints and "why now" that everyone in the room knew but nothing written records; what a newcomer can't reconstruct.
5. **Disagreements** — resolved, deferred, or quietly papered over, positions attached to people.
6. **Concrete handles** — names, dates, numbers, repo/ticket IDs, links, term definitions: the grounding that keeps a reader from inventing specifics.
7. **Deltas** — "we used to plan X, now it's Y."

## Nuance-preservation rules

- **Modality:** keep "might / leaning / floated tentatively" distinct from "will / decided." Never flatten a hedge into a commitment.
- **Attribution:** keep opinions attached to individuals. Never promote one person's view to "the team decided."
- **Conditionality:** "X if Y" stays conditional.
- **Rejected paths** stay, with their reasons (prevents re-litigation).
- **Unresolved tension:** the *fact* of disagreement is signal even with no decision. Don't smooth it into false closure.
- **Selective verbatim:** quote the few load-bearing lines exactly (commitments, contentious phrasing, a number, a stated preference).
- **Sentiment that predicts behavior:** reluctance, frustration, enthusiasm.
- **Mark inferences, and put a High / Med / Low confidence on every uncertainty, open question and unresolved garble.** Otherwise a future session treats your guess as ground truth and compounds it.

## Drop

Pleasantries, scheduling micro-logistics, tool-settings troubleshooting, redundant restatements, and the meandering *path* of the conversation: keep the conclusion, not the play-by-play. Exception: a detour that contains a rejected alternative worth remembering.

## Output skeleton

Include only the sections that earn their place for this meeting's weight. For a meeting with distinct workstreams, organize the body as **topic-cohesive sections** that keep each decision next to its context and rationale, rather than scattering one topic across functional buckets; keep **Action items**, **Open questions** and **Sources & reliability** as pull-out lists so nothing is lost in the prose. Name every section by its content ("Expenses & Ramp", not "Logistics"). Per-meeting uncertainties live in *Sources & reliability*, never in a second per-meeting file.

```markdown
# <Meeting title> — <YYYY-MM-DD>

**Type:** <kind of meeting; recording tool>
**Attendees:** <name (role + the context a stranger needs), ...>
**Read this if:** <one line: who future-reads this and why>

## TL;DR
- <3–5 bullets: the decisions and the single most important takeaway>

## <Role, scope & success criteria>   ← onboarding/kickoff only
## <Topic sections>   ← topic-cohesive body (e.g. "Expenses & Ramp", "Infra");
                      ← each keeps its own decisions + context + rationale
## Action items   ← consolidated pull-out; bold the headline ones, owner + why
### <Owner A>
- [ ] **<imperative action>** — <why / dependency>
### <Owner B>
### Later / on request   ← explicitly deferred items

## Decisions & logistics settled
## Open questions / for later discussion
## How <key person> wants to work   ← the nuance section, when style was set
## Reference   ← context-heavy meetings: people / stack / products / org / funding

## Sources & reliability   ← per-meeting reliability + uncertainties, each with a confidence
- <transcript tool + how it's lossy; point to both corrections glossaries; flag any conclusion relying on an unreviewed (auto) row>
- **Resolved (High):** <confirmed garbles / facts>
- **Open:** <each with High/Med/Low + what would resolve it; say when the audio needs checking>
```

## Privacy

Transcripts carry personal data (emails, phone numbers, tokens). Never paste a secret's value (API token, password) into a note, even when the meetings tree is a private repository: record that a secret was shared, not the value.

## In a session

- Write the note to `meetings/notes/<transcript-basename>.note.md`.
- Then tell the user in chat, not only in the file: the judgment calls you made, your confidence on the shaky ones, the *Sources & reliability* open items, and anything they need to verify (including garbles to check against the audio).
- Newly resolved garbles: one the user confirms goes into the reviewed glossary with its source meeting and "confirmed by <name> <date>"; any other goes under a dated heading in the auto tier. An auto-tier row this meeting independently confirms (the correct form appears cleanly, or the same garble resolves the same way, in a meeting other than its source) moves to the reviewed glossary's "Promoted from the auto tier" section, marked "promoted <date>, backed by <meeting>"; one it clearly contradicts is deleted. Change existing reviewed rows only with the user's confirmation.
- A transcript that did not come through Granola can be dropped by hand into `meetings/transcripts/`, an optional inbox nothing in the pipeline reads or writes.
- Notes with a generator banner on line 1 and a `-not_` or `-gem_` basename (`…-not_<id>.note.md` or `…-gem_<id>.note.md`) were written unattended by this repository's `notes.sh`; nobody reviewed their judgment calls, which are only flagged in *Sources & reliability*, so weigh those flags. A verified, unedited Gemini-only note is deleted when a transcribed Granola twin's paired note is written or found current, or when its Gemini capture becomes empty. Editing a Granola note is safe: `notes.sh` keeps a hand-edited note until you name its mirror file with `--force`. A hand-edited Gemini-only note is also kept when a paired note is written or found current. Leave the banner line in place: a note without a readable banner pages the owner.

## Without a chat

When the procedure runs unattended (the transcript arrives with the prompt and no one reads the reply as a conversation):

- What a session would tell the user in chat goes into *Sources & reliability* instead, each judgment call with a High/Med/Low confidence.
- Edit no files, and propose no glossary rows unless the calling prompt asks for them: an unresolved garble and your best reconstruction go in *Sources & reliability*.
- The calling prompt says what to output.
