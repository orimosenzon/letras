# 🎤 Karaoke — YouTube to Synced Lyrics Player

Paste any YouTube link → get a real-time karaoke experience with word-level highlighting.

**[Live demo →](https://fun-production-d221.up.railway.app/)**

---

## What it does

1. You paste a YouTube URL (or search by song name)
2. The app fetches synced lyrics from the best available source
3. Your browser plays the audio and highlights the current lyric line in sync

No Whisper, no heavy ML — just smart use of existing data sources.

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
| YouTube audio & captions | yt-dlp |
| Synced lyrics fallback | LRClib API |
| Frontend player | HTML5 Audio API + vanilla JS |
| Deployment | Docker + Gunicorn |

---

## Run locally

```bash
# Clone and install
git clone https://github.com/orimosenzon/karaoke.git
cd karaoke
pip install -r requirements.txt

# Also needs ffmpeg and nodejs on your PATH
# On Ubuntu: sudo apt install ffmpeg nodejs

# Start
python app.py
# → http://localhost:5001
```

**Or with Docker:**

```bash
docker build -t karaoke .
docker run -p 8000:8000 karaoke
```

---

## Project structure

```
app.py          — Flask routes, job tracking
transcriber.py  — lyrics pipeline (captions → LRClib → cache)
templates/
  index.html    — player UI, search, real-time sync
static/         — cached audio + transcript JSON per song
Dockerfile
```

---

## License

GPL-3.0 © 2026 Ori Mosenzon
