# Letras — Project Knowledge

## Idea
A web app that turns any YouTube song into a synced lyrics experience:
1. User searches for a song (YouTube search)
2. App fetches synced lyrics (YouTube captions or LRClib)
3. Browser plays the YouTube video and highlights the current line/word in real time
4. User can translate lyrics to any language on demand

## Tech Stack
- Python + Flask (backend)
- YouTube Data API v3 for search when `YOUTUBE_API_KEY` is set (key lives in GCP project
  `letras-lyrics-2026` under orimosenzon@gmail.com, restricted to that API; default quota
  10,000 units/day ≈ 99 searches), yt-dlp as automatic fallback (quota, network, empty page)
- yt-dlp (YouTube search fallback + captions download)
- LRClib API (synced lyrics fallback)
- MusicBrainz API (song credits: lyricist, composer, arranger, performer)
- MyMemory API (free translation, no key required)
- YouTube IFrame API + JavaScript (synchronized frontend player)
- Deployed on Railway

## Key Design Decisions
- No audio download or Whisper — uses YouTube captions or LRClib for sync
- Lyrics + credits cached as JSON in `static/` (keyed by URL hash)
- sync offset (לכוונון עיתוי) saved per-song in `localStorage` (key: `letras_offset_<videoId>`)
- Language auto-detected from lyrics text (Unicode script ranges)
- Credits labels localized per language (he/en/ar/ru/fr/es/it/de/pt/ja/ko/zh)
- `credits_version: 2` — songs cached before the MusicBrainz search fix get re-fetched automatically
- Lyric lines and translations render with `dir="auto"` — Hebrew/Arabic right-aligned, Latin left,
  each translation in its own direction; the active-line accent uses `border-inline-start`
- Keyboard on the player: Space, arrows (line), `f` fullscreen (page, not just video), `m` mute,
  Esc leaves Zen. Translate + Share float bottom-left in Zen
- **Hebrew lyrics gap baseline (13/9/2026, `tools/measure_gap.py`, 40 songs via production):**
  82% synced, 2% plain, 15% none. LRClib delivered every hit; Genius and lyrics.ovh delivered 0.
  Of the 6 misses, 2 were our own title parsing (fixed same day), 1 a cover with a different
  artist, 3 true coverage gaps (new pop / one Poliker song). Decision: no new lyrics source yet

## File Structure
```
letras/
  app.py              # Flask server
  transcriber.py      # yt-dlp + LRClib + MusicBrainz + MyMemory logic
  templates/
    index.html        # Player UI (search → results → player screens)
  static/             # Transcript/lyrics/translation cache (JSON per song)
  tools/measure_gap.py  # 40-song Hebrew sample through a server's full pipeline → synced/plain/none
  requirements.txt
  memory/
    project-knowledge.md
```

## API Routes
- POST /api/process        — accepts { url, title }, returns full song data
- POST /api/job_id         — returns job ID for polling
- GET  /api/status/<id>    — returns processing stage + elapsed time
- GET  /api/search?q=...   — YouTube search filtered to songs with LRClib lyrics
- POST /api/translate      — accepts { song_id, target_lang }, returns translated segments (cached)
- GET  /api/debug/credits  — debug endpoint for MusicBrainz credit fetch

## Song Data Format (cached JSON)
```json
{
  "id": "...", "title": "...", "url": "...", "source": "lrclib|youtube_captions|cached",
  "segments": [{ "text": "...", "start": 0.0, "end": 3.5, "words": [...] }],
  "lyricist": "...", "composer": "...", "arranger": "...", "performer": "...",
  "lang": "he|en|es|...",
  "credits_version": 2,
  "translations": { "en": ["line1", "line2", ...], "he": [...] }
}
```

## Status
- [x] Project scaffolded and deployed on Railway
- [x] YouTube search + LRClib lyrics (with YouTube captions fallback)
- [x] Word-level sync highlighting
- [x] YouTube IFrame video player with sync offset controls
- [x] Sync offset saved/restored per song (localStorage)
- [x] Song credits: lyricist, composer, arranger, performer (MusicBrainz)
- [x] Localized credit labels (12 languages)
- [x] Line-by-line translation via MyMemory API (free, cached per language in JSON)
- [x] Renamed from "karaoke" to "Letras"
