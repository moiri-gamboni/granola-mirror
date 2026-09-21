---
name: meetings
description: Turn a raw meeting transcript (Granola, Otter, etc.) into a structured note in the workspace (the git toplevel of the meetings mirror) — decisions, action items, open questions and durable context, written so a future LLM session can load the note as context. Use when extracting or summarizing a meeting transcript, writing or updating a meeting note, processing a transcript placed by hand in the optional meetings/transcripts/ inbox (for a meeting that did not come through Granola), or asked what was decided or committed in a call. Covers the extraction priorities, the nuance-preservation rules, the output skeleton and the reliability conventions.
---

# Workflow: Meeting transcript → note

Reusable procedure for turning a raw transcript (Granola, Otter, etc.) into a
structured note. Optimized for **two readers at once**: the workspace's human
owner *and* future LLM sessions that will load this note as context. Where the
two diverge, write for the model and the human gets a strictly better artifact.

Paths are relative to the workspace, the git toplevel of the mirror. The two
corrections glossaries live at `workflows/meetings/transcript-corrections.md`
(human-reviewed) and `workflows/meetings/transcript-corrections-auto.md`
(unreviewed, appended by the daily digest) — they are data this procedure reads
and writes, and they do not move into this skill.

## How to invoke

> Skim `workflows/meetings/transcript-corrections.md` (reviewed) and
> `workflows/meetings/transcript-corrections-auto.md` (unreviewed proposals — its header
> says how to treat them) for known garbles, then read
> `<path/to/transcript>` and apply this procedure.
> Write the note to `meetings/notes/<transcript-basename>.note.md`.

Then surface the **judgment calls** and the **Sources & reliability** open items
to the user in chat — don't just leave them flagged in the file.

## Core principles

1. **Self-containment.** The future LLM has zero ambient context. Resolve every
   pronoun to a name, define jargon inline, name attendees with their role. This
   is what forces quality, and it also serves forgetful future-you.
2. **Lossy on the conversation, lossless on the commitments.** Compress
   discussion freely; never compress an owner, date, number, decision, or a
   decision's *rationale*. Those are cheap to keep and expensive to lose.
3. **Preserve the nuance that prevents over-acting on under-justified
   conclusions** (see rules below). That is the specific nuance worth the words.
4. **Scale to consequence.** A standup gets a TL;DR plus action items. A meeting
   that defines a whole project earns the full skeleton. The skeleton is a menu,
   not a checklist.
5. **No transcript is ground truth — the audio is.** Your transcript is lossy: it
   garbles low-frequency tokens (proper nouns, acronyms) into nearby common words
   and can drop whole exchanges. Resolve garbles from context or the corrections
   glossaries (reviewed + auto tier) where you can; otherwise flag them with a
   confidence level. You can't hear the audio yourself — if an unresolved garble
   matters, tell the user to check it.
6. **Cross-read any second capture of the same meeting.** A meeting often also
   has a Granola note in the `granola/` mirror (match by date/title) with its own
   AI summary + transcript. Check for one before extracting and diff the two
   captures: each tool drops different audio, so content present in only one may
   be real rather than hallucinated — weigh it, don't dismiss it. Same audio means
   identical garbles are correlated, not independent — but a *clean* rendering in
   one capture is evidence against the other's garble. Attendee lists in Granola
   headers come from calendar invites and overcount actual joiners.

## Extract, in priority order

1. **Decisions** — what, who decided, **why**, rejected alternatives + why they
   lost, and how firm (settled / provisional / leaning).
2. **Action items / commitments** — owner, the thing, deadline, blocker. Split
   firm commitments from "someone should probably." Add a "later / on request"
   bucket for explicitly deferred items.
3. **Open questions** — raised but unresolved, and who owns resolving them.
4. **Durable context** — constraints and "why now" that everyone in the room
   knew but that exists nowhere in writing. What a newcomer can't reconstruct.
5. **Disagreements** — resolved, deferred, or quietly papered over. Keep
   positions attached to people.
6. **Concrete handles** — names, dates, numbers, repo/ticket IDs, links, term
   definitions. Grounding the model needs to avoid inventing specifics.
7. **Deltas** — "we used to plan X, now it's Y." Higher signal than steady state.

## Nuance-preservation rules (the differentiator)

- **Modality:** keep "might / leaning / floated tentatively" distinct from
  "will / decided." Never flatten a hedge into a commitment.
- **Attribution:** keep opinions attached to individuals. Never promote one
  person's view to "the team decided."
- **Conditionality:** "X if Y" stays conditional.
- **Rejected paths** stay, with their reasons (prevents re-litigation).
- **Unresolved tension:** the *fact* of disagreement is signal even with no
  decision. Don't smooth it into false closure.
- **Selective verbatim:** quote the few load-bearing lines exactly (commitments,
  contentious phrasing, a number, a stated preference). A few quotes beat lossy
  paraphrase.
- **Sentiment that predicts behavior:** reluctance, frustration, enthusiasm.
- **Don't silently "fix" a garbled name into the famous thing it rhymes with.**
  Reconstruct from context if you can, but flag confidence and log the original
  garble. A garbled token isn't automatically the well-known entity it resembles
  (a transcript's "Azure" was a small fiscal sponsor whose name merely sounded
  alike, not the cloud platform; "MADS" in a research meeting was a fellowship
  programme's acronym, not a product).
- **Auto-tier corrections are never applied silently either.** A row from
  `transcript-corrections-auto.md` beats the raw garble, but keep the original
  garble visible at point of use (`Yusuf [transcript: "use if", auto Med]`) and
  flag any conclusion that relies on one (see the auto file's header for the
  severity rules). Reviewed-tier rows may be applied silently.
- **Mark inferences, and put a confidence level on every uncertainty.** Tag
  anything you synthesize that wasn't said; give each uncertainty, open question,
  and unresolved garble a **High / Med / Low**. Otherwise a future session treats
  your guess as ground truth and compounds.

## Drop

Pleasantries, scheduling micro-logistics, tool-settings troubleshooting,
redundant restatements, and the meandering *path* of the conversation. Keep the
conclusion, not the play-by-play. Exception: keep a detour if it contains a
rejected alternative worth remembering.

## Output skeleton

Include only the sections that earn their place for this meeting's weight.
**Body organization:** for a meeting with distinct workstreams, prefer
**topic-cohesive sections** (e.g. "Expenses & Ramp", "Infrastructure") that keep
each decision next to its context and rationale, rather than scattering one topic
across separate functional buckets. Keep the cross-cutting lists (**Action
items**, **Open questions**, **Sources & reliability**) as pull-out safety nets so
nothing is lost in the prose. Give every section a content-encoding name
("Expenses & Ramp", not "Logistics").

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
- **Open:** <each with High/Med/Low + what would resolve it; tell the user to check audio if it matters>
```

After writing, surface to the human (in chat, not the file): the **judgment
calls** you made, your **confidence** on the shaky ones, and anything that needs
their verification. Don't bury those in the note as if settled.

## Conventions

- **Location:** notes live in `meetings/notes/`. A transcript that came through Granola
  lives in the `granola/` mirror. `meetings/transcripts/` is an optional, human-curated
  inbox for a transcript that did not come through Granola (a manually recorded
  meeting): drop it there by hand and invoke this skill on it. Nothing in this
  repository's pipeline reads or writes that directory.
- **Naming:** `<transcript-basename>.note.md`.
- **Auto notes:** `notes.sh` in this repository runs this same procedure
  unattended, writing a note into `meetings/notes/` for every Granola-mirrored
  transcript shortly after the transcript lands. They are recognizable by the
  generator banner on line 1 and the granola basename (`…-not_<id>.note.md`); their
  judgment calls are flagged in *Sources & reliability* rather than surfaced in
  chat, so weigh those flags where an in-session note would have had a human
  looking at them. Improving such a note in-session is safe: notes.sh detects a
  hand edit (the body no longer matches the hash in the banner) and **holds** the
  note — it is never overwritten by a later regeneration. Deleting the banner line
  freezes it outright; `notes.sh --force <file>` is the one sanctioned way to
  discard a hand edit and regenerate.
- **Corrections glossaries (global, reusable, two tiers):** consult
  `workflows/meetings/transcript-corrections.md` (human-reviewed) and
  `workflows/meetings/transcript-corrections-auto.md` (unreviewed proposals) before
  extracting to resolve known garbles. Newly resolved garbles: if the user
  confirms in-session, add to the reviewed file (source-meeting ref + "confirmed
  by <name> <date>"); otherwise append under a dated heading in the auto tier —
  never edit the reviewed file without their confirmation. Per-meeting
  uncertainties live in the note's *Sources & reliability* section — don't
  create a second per-meeting file.
- **Privacy:** transcripts carry personal data (emails, phone numbers, tokens).
  Even when the meetings tree is tracked in a private repository, do not paste
  raw secrets (API tokens, passwords) into the note even though the transcript
  may mention them. Reference that a secret was shared, not its value.
