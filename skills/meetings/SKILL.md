---
name: meetings
description: Use when turning a meeting transcript (Granola, Otter, a hand-recorded file) into a structured meeting note, writing or updating a meeting note, or asked what was decided or committed in a call.
---

# Meeting transcript → note

A note has two readers: the workspace's owner and future LLM sessions that load it as context. Where the two diverge, write for the model; the human still gets a better note.

Paths are relative to the workspace, the git toplevel of the meetings mirror. The corrections glossaries are `workflows/meetings/transcript-corrections.md` (human-reviewed) and `workflows/meetings/transcript-corrections-auto.md` (unreviewed proposals, appended by the daily digest).

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
- **Cross-read any second capture of the same meeting.** A meeting recorded with another tool often also has a file in the Granola mirror (e.g. `meetings/granola/`; match by date and title): check before extracting and diff the two. Each tool drops different audio, so content in only one may be real — weigh it, don't dismiss it. Identical garbles in both are correlated (same audio), not independent; a *clean* rendering in one is evidence against the other's garble.

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
- Newly resolved garbles: one the user confirms goes into the reviewed glossary with its source meeting and "confirmed by <name> <date>"; any other goes under a dated heading in the auto tier. Never edit the reviewed glossary without their confirmation.
- A transcript that did not come through Granola can be dropped by hand into `meetings/transcripts/`, an optional inbox nothing in the pipeline reads or writes.
- Notes with a generator banner on line 1 and a Granola basename (`…-not_<id>.note.md`) were written unattended by this repository's `notes.sh`; nobody reviewed their judgment calls, which are only flagged in *Sources & reliability*, so weigh those flags. Editing such a note is safe: notes.sh keeps a hand-edited note instead of regenerating it, and `notes.sh <mirror-dir> <mirror-file> --force` is the only way to discard the edit. Leave the banner line in place: a note without a readable banner pages the owner.

## Without a chat

When the procedure runs unattended (the transcript arrives with the prompt and no one reads the reply as a conversation):

- What a session would tell the user in chat goes into *Sources & reliability* instead, each judgment call with a High/Med/Low confidence.
- Edit no files, and propose no glossary rows unless the calling prompt asks for them: an unresolved garble and your best reconstruction go in *Sources & reliability*.
- The calling prompt says what to output.
