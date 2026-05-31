# mixtapes

Hand-curated YouTube mixtapes generated from Spotify playlists. Each mixtape is published as a Markdown page on GitHub Pages and includes a clickable YouTube playlist link.

## How it works

1. Curate a playlist in Spotify (any account, just make the playlist public).
2. Run `generate.py` with the Spotify URL.
3. The script:
   - Fetches the playlist metadata via spotDL.
   - Matches each track to a YouTube video.
   - Verifies the match (artist/title in YT title, duration ±3s, official-looking channel).
   - Applies any manual overrides.
   - Writes a per-mixtape Markdown page and refreshes the index pages.
4. Commit + push. GitHub Pages serves `docs/`.

## Usage

```bash
.venv/bin/python scripts/generate.py \
  --url https://open.spotify.com/playlist/XXXXXXXXXXXXXXXXXXXXXX \
  --recipient anna \
  --slug winter-2026 \
  --title "Winter 2026"
```

Output:
- `data/<recipient>/<slug>.json` — committed source manifest (diffable across reruns)
- `docs/<recipient>/<slug>.md` — the published mixtape page
- `docs/<recipient>/index.md` — list of mixtapes for that recipient
- `docs/index.md` — list of recipients

## Verification

A track is marked `✓` only if **all** of:

- Artist name appears in YouTube title (fuzzy match ≥70/100)
- Track title appears in YouTube title (fuzzy match ≥70/100)
- YT duration within ±3s of Spotify duration
- Uploader is the artist's channel, `<artist> - Topic`, or contains "VEVO"

Anything else gets `⚠` and a per-track reason in a "Needs review" section.

## Overrides

To pin a specific YouTube video for a track (e.g. spotDL matched a live version), create:

```yaml
# overrides/<recipient>/<slug>.yaml
overrides:
  - spotify_id: "4cOdK2wGLETKBW3PvgPWqT"
    youtube_id: "dQw4w9WgXcQ"
    reason: "spotDL picked a live version"
```

Then re-run `generate.py` with the same arguments. Overrides are trusted and always mark `✓`.

## Caveats

- The `watch_videos?video_ids=…` URL gives an **ad-hoc** YouTube playlist (no account needed, but session-bound and capped at 50 tracks). For >50 tracks or a persisted playlist on a YouTube account, this would need the YouTube Data API + OAuth — not currently implemented.
- spotDL ↔ YouTube matching is approximate. Spot-check a few tracks before sharing the link.
- Personal-use territory. Don't share generated links publicly.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```
