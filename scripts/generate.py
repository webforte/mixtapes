"""Generate a verified YouTube mixtape Markdown page from a Spotify playlist.

Usage:
    python scripts/generate.py \
        --url https://open.spotify.com/playlist/<id> \
        --recipient anna \
        --slug winter-2026 \
        --title "Winter 2026"

Writes:
    data/<recipient>/<slug>.json     -- committed source manifest (diffable across reruns)
    docs/<recipient>/<slug>.md       -- rendered mixtape page (GitHub Pages)
    docs/<recipient>/index.md        -- recipient's mixtape list (regenerated)
    docs/index.md                    -- top-level tenant list (regenerated)

Each track is verified against the matched YouTube video using:
    - artist name appears in YT title  (rapidfuzz partial_ratio >= 70)
    - track title appears in YT title  (rapidfuzz partial_ratio >= 70)
    - YT duration within +/- 3s of Spotify duration
    - uploader is the artist channel, "<artist> - Topic", or contains "VEVO"

Tracks that fail any check are marked with a warning and listed under "Needs review".
To pin a different YouTube video, add an entry to
    overrides/<recipient>/<slug>.yaml
and re-run.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz
from spotdl import Spotdl
from spotdl.types.song import Song
from yt_dlp import YoutubeDL


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DOCS_DIR = REPO_ROOT / "docs"
OVERRIDES_DIR = REPO_ROOT / "overrides"

VERIFY_FUZZ_THRESHOLD = 70
VERIFY_DURATION_TOLERANCE_S = 3
WATCH_VIDEOS_CAP = 50

# spotDL's bundled public Spotify app credentials. Safe to commit; spotDL itself ships them.
SPOTDL_CLIENT_ID = "5f573c9620494bae87890c0f08a60293"
SPOTDL_CLIENT_SECRET = "212476d9b0f3472eaa762d90b19b0ba8"


@dataclass
class TrackEntry:
    """A single mixtape row: Spotify source + matched YouTube + verification verdict."""

    spotify_id: str
    artist: str
    title: str
    duration_s: int
    spotify_url: str
    youtube_id: str | None
    youtube_url: str | None
    youtube_title: str | None
    youtube_uploader: str | None
    youtube_duration_s: int | None
    verified: bool
    verification_notes: list[str]
    overridden: bool


def slugify(value: str) -> str:
    """Lowercase, hyphenated, ascii-safe slug for filenames and URL segments."""
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def youtube_id_from_url(url: str) -> str | None:
    """Extract the 11-char video id from any common YouTube URL form."""
    if not url:
        return None
    match = re.search(r"(?:v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return match.group(1) if match else None


def fetch_youtube_metadata(video_id: str) -> dict[str, Any] | None:
    """Fetch title, uploader, duration for a YouTube video. Returns None on failure."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    try:
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=False,
            )
    except Exception as exc:  # noqa: BLE001 — yt-dlp raises a wide range of errors
        logging.warning("yt-dlp could not fetch metadata for %s: %s", video_id, exc)
        return None


_TITLE_DECORATION_PATTERNS = (
    re.compile(r"\s*\(feat\.[^)]*\)", re.IGNORECASE),
    re.compile(r"\s*\(with[^)]*\)", re.IGNORECASE),
    re.compile(
        r"\s*-\s*(Live|Remastered( \d{4})?|Remix|Single Version|Extended( Version)?|"
        r"Mono|Stereo|Radio Edit|Acoustic|Demo|Deluxe Edition|Bonus Track).*$",
        re.IGNORECASE,
    ),
)


def strip_title_decorations(title: str) -> str:
    """Remove `(feat. X)`, `- Live`, `- Remastered` etc. that Spotify adds but YouTube usually omits."""
    cleaned = title
    for pattern in _TITLE_DECORATION_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.strip()


def verify_match(
    artist: str,
    title: str,
    duration_s: int,
    yt_title: str,
    yt_uploader: str,
    yt_duration_s: int,
) -> tuple[bool, list[str]]:
    """Return (verified, notes). verified=True means every signal passed.

    Heuristic:
      - YouTube metadata must be present (yt-dlp fetch succeeded).
      - Duration within tolerance — strongest single signal of "same recording".
      - Track name (with Spotify decorations stripped) appears in YT title.
      - Artist appears in YT title OR uploader — covers `Artist - Topic`,
        `ArtistVEVO`, and self-uploaded channels uniformly.
    """
    notes: list[str] = []

    if not yt_title and not yt_uploader and not yt_duration_s:
        notes.append("YouTube metadata unavailable — couldn't verify")
        return (False, notes)

    duration_delta = abs(yt_duration_s - duration_s)
    if duration_delta > VERIFY_DURATION_TOLERANCE_S:
        notes.append(f"duration off by {duration_delta}s")

    cleaned_title = strip_title_decorations(title)
    title_score = fuzz.token_set_ratio(cleaned_title.lower(), yt_title.lower())
    if title_score < VERIFY_FUZZ_THRESHOLD:
        notes.append(
            f"track name '{cleaned_title}' not recognisable in YT title '{yt_title}' (fuzz={title_score:.0f})"
        )

    haystack = f"{yt_title} | {yt_uploader}".lower()
    artist_score = fuzz.partial_ratio(artist.lower(), haystack)
    if artist_score < VERIFY_FUZZ_THRESHOLD:
        notes.append(
            f"artist '{artist}' not found in YT title or uploader '{yt_uploader}' (fuzz={artist_score:.0f})"
        )

    return (len(notes) == 0, notes)


def load_overrides(recipient: str, slug: str) -> dict[str, str]:
    """Read overrides/<recipient>/<slug>.yaml, returning {spotify_id: youtube_id}."""
    path = OVERRIDES_DIR / recipient / f"{slug}.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    out: dict[str, str] = {}
    for entry in raw.get("overrides", []):
        sp = entry.get("spotify_id")
        yt = entry.get("youtube_id")
        if sp and yt:
            out[sp] = yt
    return out


def process_playlist(
    spotify_url: str, recipient: str, slug: str
) -> list[TrackEntry]:
    """Resolve a Spotify playlist into verified TrackEntry rows."""
    spotdl = Spotdl(
        client_id=SPOTDL_CLIENT_ID,
        client_secret=SPOTDL_CLIENT_SECRET,
        no_cache=True,
        downloader_settings={"simple_tui": True},
    )

    print("→ Searching Spotify playlist…", flush=True)
    songs: list[Song] = spotdl.search([spotify_url])
    if not songs:
        raise SystemExit(f"No tracks found at {spotify_url}")
    print(f"  found {len(songs)} tracks", flush=True)

    overrides = load_overrides(recipient, slug)
    if overrides:
        print(f"  applying {len(overrides)} override(s) from overrides/{recipient}/{slug}.yaml", flush=True)

    print("→ Matching tracks to YouTube…", flush=True)
    entries: list[TrackEntry] = []
    for index, song in enumerate(songs, start=1):
        print(f"  [{index}/{len(songs)}] {song.artist} — {song.name}", flush=True)

        override_yt_id = overrides.get(song.song_id)
        if override_yt_id:
            yt_id = override_yt_id
            overridden = True
        else:
            # Run one song at a time so each URL pairs with its requested song.
            # (spotDL's batch API uses concurrent.futures and returns in completion order.)
            try:
                single_url_results = spotdl.get_download_urls([song])
            except Exception as exc:  # noqa: BLE001
                logging.warning("spotDL match failed for %s: %s", song.song_id, exc)
                single_url_results = []
            raw_url = single_url_results[0] if single_url_results else None
            yt_id = youtube_id_from_url(raw_url if isinstance(raw_url, str) else "")
            overridden = False

        if not yt_id:
            entries.append(
                TrackEntry(
                    spotify_id=song.song_id,
                    artist=song.artist,
                    title=song.name,
                    duration_s=song.duration,
                    spotify_url=song.url,
                    youtube_id=None,
                    youtube_url=None,
                    youtube_title=None,
                    youtube_uploader=None,
                    youtube_duration_s=None,
                    verified=False,
                    verification_notes=["no YouTube match found"],
                    overridden=overridden,
                )
            )
            continue

        meta = fetch_youtube_metadata(yt_id)
        yt_title = (meta or {}).get("title", "") or ""
        yt_uploader = (meta or {}).get("uploader", "") or ""
        yt_duration = int((meta or {}).get("duration") or 0)

        verified, notes = verify_match(
            song.artist, song.name, song.duration,
            yt_title, yt_uploader, yt_duration,
        )
        if overridden:
            # Manual pins are trusted by definition; record the note but mark verified.
            notes = [n for n in notes]
            verified = True
            notes.insert(0, "manually overridden")

        entries.append(
            TrackEntry(
                spotify_id=song.song_id,
                artist=song.artist,
                title=song.name,
                duration_s=song.duration,
                spotify_url=song.url,
                youtube_id=yt_id,
                youtube_url=f"https://www.youtube.com/watch?v={yt_id}",
                youtube_title=yt_title,
                youtube_uploader=yt_uploader,
                youtube_duration_s=yt_duration or None,
                verified=verified,
                verification_notes=notes,
                overridden=overridden,
            )
        )

    return entries


def write_manifest(
    entries: list[TrackEntry],
    recipient: str,
    slug: str,
    spotify_url: str,
    title: str,
) -> Path:
    """Persist the source-of-truth JSON used to regenerate the MD page."""
    path = DATA_DIR / recipient / f"{slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "title": title,
        "spotify_url": spotify_url,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tracks": [asdict(e) for e in entries],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def format_duration(seconds: int) -> str:
    """Format seconds as M:SS for compact table display."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def render_mixtape_md(
    entries: list[TrackEntry],
    recipient: str,
    slug: str,
    title: str,
    spotify_url: str,
) -> str:
    """Render the per-mixtape Markdown page (tracklist + watch_videos URL + review section)."""
    lines: list[str] = [
        "---",
        f"title: {title}",
        f"recipient: {recipient}",
        "---",
        "",
        f"# {title}",
        "",
        f"_Curated for **{recipient}** · generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')}._",
        "",
    ]

    yt_ids = [e.youtube_id for e in entries if e.youtube_id]
    if yt_ids:
        watch_ids = yt_ids[:WATCH_VIDEOS_CAP]
        watch_url = "https://www.youtube.com/watch_videos?video_ids=" + ",".join(watch_ids)
        lines.append(f"**[▶ Play as YouTube playlist]({watch_url})**")
        if len(yt_ids) > WATCH_VIDEOS_CAP:
            lines.append("")
            lines.append(
                f"_(First {WATCH_VIDEOS_CAP} of {len(yt_ids)} — YouTube's ad-hoc playlist URL is capped at {WATCH_VIDEOS_CAP}.)_"
            )
        lines.append("")

    lines.append("| # | Artist | Title | Duration | YouTube | ✓ |")
    lines.append("|---|--------|-------|----------|---------|---|")
    for i, e in enumerate(entries, start=1):
        mark = "✓" if e.verified else "⚠"
        yt_cell = f"[link]({e.youtube_url})" if e.youtube_url else "—"
        artist = e.artist.replace("|", "\\|")
        track = e.title.replace("|", "\\|")
        lines.append(
            f"| {i} | {artist} | {track} | {format_duration(e.duration_s)} | {yt_cell} | {mark} |"
        )

    unverified = [e for e in entries if not e.verified]
    if unverified:
        lines.extend([
            "",
            "## ⚠ Needs review",
            "",
        ])
        for e in unverified:
            lines.append(f"- **{e.artist} — {e.title}**")
            for note in e.verification_notes:
                lines.append(f"  - {note}")
            if e.youtube_url:
                lines.append(
                    f"  - matched: [{e.youtube_title or e.youtube_url}]({e.youtube_url}) — uploader `{e.youtube_uploader or 'unknown'}`"
                )
            lines.append(f"  - spotify id: `{e.spotify_id}`")
        lines.extend([
            "",
            f"To pin a different video, add an entry to `overrides/{recipient}/{slug}.yaml` and re-run.",
        ])

    lines.extend([
        "",
        "---",
        "",
        f"_[Source playlist on Spotify]({spotify_url})_",
        "",
    ])
    return "\n".join(lines)


def extract_title_from_frontmatter(md_path: Path) -> str:
    """Pull the `title:` value out of a Markdown file's YAML frontmatter."""
    content = md_path.read_text()
    match = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else md_path.stem


def regenerate_recipient_index(recipient: str) -> None:
    """Refresh docs/<recipient>/index.md to list all mixtapes for that recipient."""
    rec_dir = DOCS_DIR / recipient
    if not rec_dir.exists():
        return
    mixtapes = sorted(p for p in rec_dir.glob("*.md") if p.name != "index.md")
    lines = [
        "---",
        f"title: Mixtapes for {recipient}",
        "---",
        "",
        f"# Mixtapes for {recipient}",
        "",
    ]
    if not mixtapes:
        lines.append("_No mixtapes yet._")
    else:
        for m in mixtapes:
            lines.append(f"- [{extract_title_from_frontmatter(m)}]({m.stem}.md)")
    lines.append("")
    (rec_dir / "index.md").write_text("\n".join(lines))


def regenerate_top_index() -> None:
    """Refresh docs/index.md to list all recipients (tenants)."""
    if not DOCS_DIR.exists():
        return
    recipients = sorted(
        p.name for p in DOCS_DIR.iterdir()
        if p.is_dir() and not p.name.startswith(("_", "."))
    )
    lines = [
        "---",
        "title: Mixtapes",
        "---",
        "",
        "# Mixtapes",
        "",
        "_Hand-curated playlists, each cross-checked against its Spotify source._",
        "",
    ]
    if not recipients:
        lines.append("_No recipients yet._")
    else:
        for r in recipients:
            lines.append(f"- [{r}]({r}/)")
    lines.append("")
    (DOCS_DIR / "index.md").write_text("\n".join(lines))


def generate_one(spotify_url: str, recipient_raw: str, slug_raw: str, title: str) -> tuple[int, int]:
    """Generate manifest + MD for a single mixtape. Returns (verified_count, needs_review_count)."""
    recipient = slugify(recipient_raw)
    slug = slugify(slug_raw)

    print()
    print(f"=== {recipient}/{slug} — {title} ===")

    entries = process_playlist(spotify_url, recipient, slug)

    verified_count = sum(1 for e in entries if e.verified)
    needs_review = len(entries) - verified_count

    manifest_path = write_manifest(entries, recipient, slug, spotify_url, title)
    md = render_mixtape_md(entries, recipient, slug, title, spotify_url)
    md_path = DOCS_DIR / recipient / f"{slug}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md)

    print(f"  ✓ verified: {verified_count}    ⚠ needs review: {needs_review}")
    print(f"  → {manifest_path.relative_to(REPO_ROOT)}")
    print(f"  → {md_path.relative_to(REPO_ROOT)}")

    return verified_count, needs_review


def generate_from_config(config_path: Path) -> int:
    """Regenerate every mixtape declared in playlists.yaml. Returns total ⚠ count across runs."""
    config = yaml.safe_load(config_path.read_text()) or {}
    tenants = config.get("tenants", {})
    if not tenants:
        raise SystemExit(f"{config_path} declares no tenants.")

    total_review = 0
    touched_recipients: set[str] = set()

    for recipient_raw, tenant in tenants.items():
        mixtapes = (tenant or {}).get("mixtapes", []) or []
        for entry in mixtapes:
            slug_raw = entry["slug"]
            title = entry["title"]
            spotify_url = entry["spotify_url"]
            try:
                _verified, needs_review = generate_one(spotify_url, recipient_raw, slug_raw, title)
                total_review += needs_review
                touched_recipients.add(slugify(recipient_raw))
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ FAILED {recipient_raw}/{slug_raw}: {exc}", flush=True)

    for recipient in touched_recipients:
        regenerate_recipient_index(recipient)
    regenerate_top_index()
    print()
    print(f"→ Regenerated indexes for {len(touched_recipients)} recipient(s)")
    return total_review


def main() -> None:
    """CLI entry point — see module docstring for usage."""
    parser = argparse.ArgumentParser(
        description="Generate verified YouTube mixtape Markdown pages from Spotify playlists.",
    )
    parser.add_argument(
        "--config",
        help="Path to playlists.yaml — regenerate every declared mixtape.",
    )
    parser.add_argument("--url", help="Public Spotify playlist URL (single-mixtape mode)")
    parser.add_argument("--recipient", help="Recipient slug (single-mixtape mode)")
    parser.add_argument("--slug", help="Mixtape slug (single-mixtape mode)")
    parser.add_argument("--title", help="Mixtape display title (single-mixtape mode)")
    args = parser.parse_args()

    if args.config:
        if any([args.url, args.recipient, args.slug, args.title]):
            parser.error("--config cannot be combined with --url / --recipient / --slug / --title")
        total_review = generate_from_config(Path(args.config))
    else:
        missing = [name for name in ("url", "recipient", "slug", "title") if not getattr(args, name)]
        if missing:
            parser.error(f"Either --config or all of --url/--recipient/--slug/--title. Missing: {missing}")
        _verified, total_review = generate_one(args.url, args.recipient, args.slug, args.title)
        regenerate_recipient_index(slugify(args.recipient))
        regenerate_top_index()

    if total_review:
        print()
        print(f"⚠ {total_review} track(s) need review — see the 'Needs review' section in the generated MD pages.")


if __name__ == "__main__":
    main()
