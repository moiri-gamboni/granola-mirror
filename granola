#!/usr/bin/env python3
"""granola — read Granola notes via the official public API (grn_ key).

Key is read from ~/.config/granola/api-key (0600), never passed on the CLI.
Reaches everything the key's scopes allow: your own notes, notes shared with
you, and (with the Public scope) workspace / team-space notes.

  granola folders                          list folders (id + name)
  granola notes [--folder ID] [--limit N]  list notes (newest first)
  granola get <note-id> [--transcript]     print one note as markdown
  granola sync DIR [--folder ID] [--since YYYY-MM-DD] [--transcript]
                                           mirror notes -> one markdown file each
"""
import sys, os, json, re, time, argparse, urllib.request, urllib.parse, urllib.error

API = "https://public-api.granola.ai/v1"
KEYFILE = os.path.expanduser("~/.config/granola/api-key")


def _key():
    try:
        return open(KEYFILE).read().strip()
    except FileNotFoundError:
        sys.exit(f"granola: no API key at {KEYFILE}")


_MIN_INTERVAL = 0.22   # ~4.5 req/s, under Granola's 5 req/s sustained cap (25 burst)
_last = [0.0]

def api(path, **params):
    url = f"{API}{path}"
    q = {k: v for k, v in params.items() if v is not None}
    if q:
        url += "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {_key()}", "Accept": "application/json"}
    )
    for attempt in range(6):
        wait = _MIN_INTERVAL - (time.monotonic() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 5:
                ra = e.headers.get("Retry-After", "")
                time.sleep(float(ra) if ra.replace(".", "", 1).isdigit() else 2 ** attempt)
                continue
            sys.exit(f"granola: HTTP {e.code} on {path}: {e.read().decode()[:300]}")
        except urllib.error.URLError as e:
            if attempt < 5:
                time.sleep(2 ** attempt)
                continue
            sys.exit(f"granola: network error on {path}: {e}")


def paginate(path, key, **params):
    cursor = None
    while True:
        d = api(path, cursor=cursor, **params)
        for it in d.get(key, []):
            yield it
        if not d.get("hasMore"):
            break
        cursor = d.get("cursor")


def note_markdown(n):
    o = n.get("owner") or {}
    att = ", ".join((x.get("name") or x.get("email") or "") for x in (n.get("attendees") or []))
    out = [f'<!-- granola updated_at: {n.get("updated_at","")} -->', f'# {n.get("title") or "(untitled)"}', ""]
    out.append(f'- **Date:** {(n.get("created_at") or "")[:10]}')
    if o:
        out.append(f'- **Owner:** {o.get("name","")} <{o.get("email","")}>')
    if att:
        out.append(f'- **Attendees:** {att}')
    if n.get("web_url"):
        out.append(f'- **Granola:** {n["web_url"]}')
    out += ["", (n.get("summary_markdown") or n.get("summary_text") or "_(no summary)_")]
    t = n.get("transcript")
    if t:
        out += ["", "## Transcript", "", t if isinstance(t, str) else json.dumps(t, indent=2)]
    return "\n".join(out).rstrip() + "\n"


def slug(s):
    return (re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "note")[:60]


def cmd_folders(a):
    for f in paginate("/folders", "folders"):
        print(f'{f["id"]}  {f["name"]}')


def cmd_notes(a):
    n = 0
    for note in paginate("/notes", "notes", folder_id=a.folder):
        o = note.get("owner") or {}
        print(f'{note["id"]}  {(note.get("created_at") or "")[:10]}  {note.get("title","")}  <{o.get("email","")}>')
        n += 1
        if a.limit and n >= a.limit:
            break


def cmd_get(a):
    note = api(f"/notes/{a.id}", include_transcript="true" if a.transcript else None)
    sys.stdout.write(note_markdown(note))


def cmd_sync(a):
    os.makedirs(a.dir, exist_ok=True)
    written = skipped = 0
    changed = []
    for note in paginate("/notes", "notes", folder_id=a.folder):
        if a.since and (note.get("created_at") or "")[:10] < a.since:
            continue
        fn = f'{(note.get("created_at") or "")[:10]}-{slug(note.get("title"))}-{note["id"]}.md'
        path = os.path.join(a.dir, fn)
        old = open(path).read() if os.path.exists(path) else ""
        # incremental: skip notes whose updated_at is unchanged
        if old and not a.force:
            m = re.search(r"granola updated_at: (\S+)", old)
            if m and m.group(1) == (note.get("updated_at") or ""):
                skipped += 1
                continue
        full = api(f'/notes/{note["id"]}')
        md = note_markdown(full).rstrip()
        # preserve a transcript section added by granola-transcripts
        idx = old.find("\n## Transcript\n")
        md += ("\n\n" + old[idx:].strip() + "\n") if idx != -1 else "\n"
        with open(path, "w") as fh:
            fh.write(md)
        written += 1
        changed.append(path)
    if a.changed_file:
        with open(a.changed_file, "w") as fh:
            fh.write("".join(p + "\n" for p in changed))
    print(f"granola: {written} written, {skipped} unchanged -> {a.dir}")


p = argparse.ArgumentParser(prog="granola", description="Read Granola notes via the public API.")
sub = p.add_subparsers(dest="cmd", required=True)
sub.add_parser("folders").set_defaults(fn=cmd_folders)
pn = sub.add_parser("notes"); pn.add_argument("--folder"); pn.add_argument("--limit", type=int, default=30); pn.set_defaults(fn=cmd_notes)
pg = sub.add_parser("get"); pg.add_argument("id"); pg.add_argument("--transcript", action="store_true"); pg.set_defaults(fn=cmd_get)
ps = sub.add_parser("sync"); ps.add_argument("dir"); ps.add_argument("--folder"); ps.add_argument("--since"); ps.add_argument("--force", action="store_true"); ps.add_argument("--changed-file"); ps.set_defaults(fn=cmd_sync)
args = p.parse_args()
args.fn(args)
