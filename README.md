# 🎤 Letras — YouTube Lyrics Player

Paste any YouTube link → get real-time synchronized lyrics with word-level highlighting.

**[Live demo →](https://fun-production-d221.up.railway.app/)**

---

## What it does

1. You paste a YouTube URL (or search by song name)
2. The app fetches synced lyrics from the best available source
3. Your browser plays the video and highlights the current lyric line in sync
4. Translate lyrics to any language on demand (free, no API key needed)

No Whisper, no heavy ML — just smart use of existing data sources.

**Keyboard, while a song plays:** `Space` play/pause · `↑ ↓ ← →` previous/next line · `f` fullscreen (video and lyrics together) · `m` mute · `Esc` leave Zen mode.

---

## How lyrics are sourced

The app tries sources in order, falling back gracefully:

```
YouTube auto-captions (word-level timestamps)
    ↓  not available?
LRClib.net (community-synced lyrics database)
    ↓  not found?
"No lyrics available" — honest about it
```

YouTube's auto-generated captions include per-word timestamps, which enables **word-level highlighting** — the same effect professional karaoke apps pay for.

---

## Search that doesn't waste your time

The search endpoint doesn't just return YouTube results — it filters them in parallel against LRClib before responding. If a song isn't in the database, it won't appear in results. No dead ends.

---

## Tech stack

| Layer | Tool |
|---|---|
| Backend | Python + Flask |
| YouTube search | YouTube Data API v3 (with `YOUTUBE_API_KEY`), yt-dlp as fallback |
| YouTube captions | yt-dlp |
| Synced lyrics fallback | LRClib API |
| Song credits | MusicBrainz API |
| Translation | MyMemory API (free) |
| Frontend player | YouTube IFrame API + vanilla JS |
| Deployment | Docker + Gunicorn |

---

## Run locally

```bash
# Clone and install
git clone https://github.com/orimosenzon/letras.git
cd letras
pip install -r requirements.txt

# Start
python app.py
# → http://localhost:5001  (use localhost, not 127.0.0.1 — YouTube refuses to embed from the latter)
```

Optional environment variables:

| Variable | Effect |
|---|---|
| `YOUTUBE_API_KEY` | Search through the YouTube Data API v3 instead of scraping with yt-dlp. Free key, restricted to that API; ~99 searches/day on the default quota, after which the app falls back to yt-dlp by itself. |
| `GENIUS_TOKEN` | Adds Genius as a plain-lyrics fallback. |

`tools/measure_gap.py` runs a fixed sample of 40 Hebrew songs through a server's full pipeline and reports how many end up synced / plain / without lyrics — run it after touching any lyrics source or the title parsing.

---

## Project structure

```
app.py          — Flask routes, job tracking
transcriber.py  — lyrics pipeline (captions → LRClib → cache) + translation
templates/
  index.html    — player UI, search, real-time sync, translation
static/         — cached lyrics JSON per song
Dockerfile
```

---

## License

GPL-3.0 © 2026 Ori Mosenzon
