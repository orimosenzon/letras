# Copyright (C) 2026 Ori Mosenzon and Claude (Anthropic AI)
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for details.

import os
import re
import json
import hashlib
import glob as glob_mod
import logging
import logging.handlers
import time
import threading
import urllib.request
import urllib.parse
import concurrent.futures
import yt_dlp

STATIC_DIR = os.environ.get("CACHE_DIR", os.path.join(os.path.dirname(__file__), "static"))
os.makedirs(STATIC_DIR, exist_ok=True)

LOG_PATH = os.environ.get("LOG_PATH", os.path.join(os.path.dirname(__file__), "letras.log"))
log = logging.getLogger("letras")
if not log.handlers:
    log.setLevel(logging.DEBUG)
    _fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname).1s %(message)s", "%H:%M:%S")
    _fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    _sh.setLevel(logging.INFO)
    log.addHandler(_sh)


def url_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def search_songs(query: str) -> list:
    """Search YouTube and return candidate songs immediately.

    Lyrics availability is NOT checked here (LRClib takes ~3s per query) —
    the frontend fetches /api/lrc_check per result and fills badges in as
    they resolve, so the results screen appears right after the YouTube search.
    """
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"js_runtimes": ["nodejs"]}},
    }
    t0 = time.time()
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch10:{query}", download=False)
    except Exception as e:
        log.error(f"youtube search FAILED {time.time()-t0:.1f}s [{query!r}]: {e}")
        return []
    log.info(f"youtube search {time.time()-t0:.1f}s [{query!r}] "
             f"{len(info.get('entries') or [])} entries")

    candidates = []
    for entry in (info.get("entries") or []):
        if not entry or not entry.get("id"):
            continue
        vid_id = entry["id"]
        duration = entry.get("duration")
        candidates.append({
            "title": entry.get("title", "Unknown"),
            "url": f"https://www.youtube.com/watch?v={vid_id}",
            "thumbnail": f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg",
            "duration": duration,
            # Videos longer than 15 min are concerts/compilations, not songs
            "lrc_status": "none" if duration and duration > 900 else "pending",
        })

    # Songs (checkable) first, long videos last
    candidates.sort(key=lambda c: c["lrc_status"] == "none")
    return candidates[:8]


def _check_lrclib(title: str, duration=None) -> str:
    """Check if LRClib has lyrics for this title. Returns 'synced', 'plain', or 'none'.

    Badge check only — capped at 2 query waves to keep LRClib load sane when
    8 cards check at once. The full sweep runs when the user picks the song.
    """
    results = _lrclib_search(title, duration, max_waves=2)
    if _pick_lrclib_result(results, title, duration, want_synced=True):
        return "synced"
    if _pick_lrclib_result(results, title, duration, want_synced=False):
        return "plain"
    return "none"


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


_LRCLIB_CACHE = {}
_LRCLIB_CACHE_LOCK = threading.Lock()
_LRCLIB_CACHE_TTL = 600  # seconds
# LRClib is a free community service and throttles bursts — cap our concurrency
_LRCLIB_SEM = threading.Semaphore(6)


def _lrclib_request(params: dict):
    """One LRClib search request with retries on transient failure. Memoized (10 min TTL)."""
    key = urllib.parse.urlencode(sorted(params.items()))
    now = time.time()
    with _LRCLIB_CACHE_LOCK:
        hit = _LRCLIB_CACHE.get(key)
        if hit and now - hit[0] < _LRCLIB_CACHE_TTL:
            log.debug(f"lrclib cache-hit [{key}] {len(hit[1])} results")
            return hit[1]

    url = f"https://lrclib.net/api/search?{key}"
    results, last_err = None, None
    t0 = time.time()
    for attempt in range(2):
        try:
            with _LRCLIB_SEM:
                req = urllib.request.Request(url, headers={"User-Agent": "LetrasApp/1.0"})
                with urllib.request.urlopen(req, timeout=12) as resp:
                    results = json.loads(resp.read())
            break
        except Exception as e:
            last_err = e
            log.debug(f"lrclib attempt {attempt+1} failed [{key}]: {e}")
            time.sleep(0.7)
    if results is None:
        log.warning(f"lrclib FAILED after retries {time.time()-t0:.1f}s [{key}]: {last_err}")
        return []
    synced_n = sum(1 for r in results if r.get("syncedLyrics"))
    log.debug(f"lrclib {time.time()-t0:.1f}s [{key}] {len(results)} results ({synced_n} synced)")
    with _LRCLIB_CACHE_LOCK:
        _LRCLIB_CACHE[key] = (now, results)
        if len(_LRCLIB_CACHE) > 2000:
            _LRCLIB_CACHE.clear()
    return results


_JUNK_WORDS = re.compile(
    r'\b(official|video|audio|lyrics?|lyric|hd|hq|4k|remaster(?:ed)?|version|'
    r'original|live|full|album|clip|visualizer|mv|19\d\d|20\d\d)\b',
    re.IGNORECASE,
)


def _strip_junk(s: str) -> str:
    """Remove YouTube-title junk words; returns original if stripping empties it."""
    if not s:
        return s
    stripped = _norm_ws(_JUNK_WORDS.sub(' ', s))
    return stripped or s


def _script_runs(text: str) -> list:
    """Split mixed-script text into contiguous same-script runs.

    'אייל גולן מלכת היופי שלי Eyal Golan' → ['אייל גולן מלכת היופי שלי', 'Eyal Golan'].
    Only returns runs when the text actually mixes Hebrew/Arabic with Latin.
    """
    is_rtl = lambda c: '֐' <= c <= 'ۿ'
    is_lat = lambda c: c.isascii() and c.isalpha()
    runs, cur, cur_kind = [], [], None
    for ch in text:
        kind = 'rtl' if is_rtl(ch) else ('lat' if is_lat(ch) else None)
        if kind is None:
            cur.append(ch)
            continue
        if cur_kind is None or kind == cur_kind:
            cur_kind = cur_kind or kind
            cur.append(ch)
        else:
            runs.append((_norm_ws(''.join(cur)), cur_kind))
            cur, cur_kind = [ch], kind
    if cur and cur_kind:
        runs.append((_norm_ws(''.join(cur)), cur_kind))
    kinds = {k for _, k in runs}
    if len(kinds) < 2:
        return []
    return [r for r, _ in runs if len(r.split()) >= 2]


def _lrclib_search(title: str, duration=None, max_waves=None) -> list:
    """Search LRClib with several query variants built from the YouTube title.

    Raw YouTube titles ("Artist - Song (Official Video) | Channel") return zero
    results from LRClib's fuzzy search, so we try progressively looser queries
    and merge results. Stops early once an acceptable synced match is found.
    """
    song, artist = _parse_title_artist(title)
    cleaned = _norm_ws(re.sub(r'[\(\)\[\]|]', ' ', re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', title)))
    song_c, artist_c = _strip_junk(song), _strip_junk(artist) if artist else None

    variants = []
    if artist_c:
        variants.append({"track_name": song_c, "artist_name": artist_c})
        variants.append({"q": _norm_ws(f"{song_c} {artist_c}")})
    variants.append({"q": song_c})
    # Bilingual/multi-part titles ("Lifney She'Yigamer - עידן רייכל - לפני שייגמר"):
    # each dash-separated segment is a candidate song name
    for seg in song.split(' - '):
        seg = _strip_junk(seg.strip())
        if seg and seg.lower() != song_c.lower():
            variants.append({"q": seg})
    # Dash-less mixed-script titles ("אייל גולן מלכת היופי שלי Eyal Golan"):
    # each same-script run is a candidate query
    if not artist:
        for run in _script_runs(cleaned):
            variants.append({"q": _strip_junk(run)})
    if artist_c:
        # YouTube titles can be "Song - Artist" as well as "Artist - Song"
        variants.append({"track_name": artist_c, "artist_name": song_c})
    variants.append({"q": _strip_junk(cleaned.replace(' - ', ' '))})
    variants.append({"q": cleaned})

    # Deduplicate variants, preserving order
    seen_v, uniq = set(), []
    for v in variants:
        key = tuple(sorted((k, val.lower()) for k, val in v.items() if val))
        if key and key not in seen_v:
            seen_v.add(key)
            uniq.append(v)

    # LRClib search takes ~3s per query, so run variants in parallel waves of 3
    # with an early stop between waves once an acceptable synced match is held
    t0 = time.time()
    merged, seen = [], set()
    waves_run = 0
    for i in range(0, len(uniq), 3):
        if max_waves and waves_run >= max_waves:
            break
        wave = uniq[i:i + 3]
        waves_run += 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave)) as ex:
            wave_results = list(ex.map(_lrclib_request, wave))
        for results in wave_results:
            for r in results:
                rid = r.get("id")
                if rid in seen:
                    continue
                seen.add(rid)
                merged.append(r)
        if _pick_lrclib_result(merged, title, duration, want_synced=True):
            break
    log.info(f"lrclib_search {time.time()-t0:.1f}s [{title[:50]!r}] "
             f"{waves_run} waves, {len(merged)} results "
             f"({sum(1 for r in merged if r.get('syncedLyrics'))} synced)")
    return merged


def _pick_lrclib_result(results: list, title: str, duration=None, want_synced=True):
    """Pick the LRClib result best matching the YouTube title (and duration, if known)."""
    field = "syncedLyrics" if want_synced else "plainLyrics"
    pool = [r for r in results if r.get(field)]
    # A wildly different duration means a different recording — sync would be useless.
    # Plain text doesn't care about timing, so no duration gate there.
    if duration and want_synced:
        pool = [r for r in pool
                if not r.get("duration") or abs(float(r["duration"]) - float(duration)) <= 120]
    if not pool:
        return None

    song, artist = _parse_title_artist(title)

    def _sim(a: str, b: str) -> float:
        a, b = _norm_ws(a).lower(), _norm_ws(b).lower()
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        if a in b or b in a:
            return 0.8
        aw, bw = set(a.split()), set(b.split())
        return len(aw & bw) / max(len(aw), len(bw)) if aw and bw else 0.0

    def score(r):
        """Returns (total_score, track_name_similarity)."""
        track, art = r.get("trackName", ""), r.get("artistName", "")
        # Try both assignments — _parse_title_artist may have song/artist swapped.
        # Track-name match weighs double: matching the artist alone must never win.
        # The swapped assignment counts only with real evidence of reversal
        # (result's artistName resembles our parsed "song") — otherwise a result
        # whose track name merely CONTAINS the artist (e.g. 'נעמי שמר / אנשים
        # טובים') would hijack the pick.
        a1 = 2 * _sim(song, track) + _sim(artist or "", art)
        a2 = 2 * _sim(artist or "", track) + _sim(song, art) if artist and _sim(song, art) >= 0.5 else -1.0
        s, track_sim = (a1, _sim(song, track)) if a1 >= a2 else (a2, _sim(artist or "", track))
        if duration and r.get("duration"):
            diff = abs(float(r["duration"]) - float(duration))
            if diff <= 10:
                s += 1.0 - diff / 15  # closer duration = better sync timing
            elif diff > 30:
                s -= 0.5
        # Prefer studio recordings unless the video itself is a live one
        if "live" in track.lower() and "live" not in title.lower():
            s -= 0.15
        return s, track_sim

    best = max(pool, key=lambda r: score(r)[0])
    # The result's track name must actually resemble the song —
    # otherwise a fuzzy search can attach a random song by the same artist
    best_score, best_track_sim = score(best)
    accepted = best_track_sim >= 0.3
    log.debug(f"pick[{'synced' if want_synced else 'plain'}] "
              f"{'OK' if accepted else 'REJECT'} score={best_score:.2f} track_sim={best_track_sim:.2f} "
              f"[{best.get('artistName','')!r} — {best.get('trackName','')!r} d={best.get('duration')}] "
              f"for [{title[:50]!r} d={duration}]")
    return best if accepted else None


SYNCED_SOURCES = {"youtube_captions", "lrclib"}


def process_url(url: str, title: str = "", on_stage=None, duration=None):
    t_start = time.time()

    def stage(s):
        log.info(f"process stage={s} +{time.time()-t_start:.1f}s [{title[:50]!r}]")
        if on_stage:
            on_stage(s)

    vid_id = url_id(url)
    transcript_path = os.path.join(STATIC_DIR, f"{vid_id}.json")

    if os.path.exists(transcript_path):
        with open(transcript_path) as f:
            cached = json.load(f)
        # If lyrics were never found, try again (new sources may be available now)
        if cached.get("source") == "none":
            log.info(f"process: retrying cached 'none' song [{title[:50]!r}]")
            os.remove(transcript_path)
        else:
            # Cached with plain lyrics only — try upgrading to synced (LRClib
            # grows over time, and the original fetch may have hit a transient failure)
            if cached.get("source") not in SYNCED_SOURCES:
                lrc = _try_lrclib(cached.get("title") or title, duration)
                if lrc:
                    log.info(f"process: upgraded cached plain→synced [{title[:50]!r}]")
                    cached["segments"] = lrc
                    cached["source"] = "lrclib"
                    cached["synced"] = True
                    # Old translations are aligned to the old segment list
                    cached.pop("translations", None)
                    with open(transcript_path, "w") as f:
                        json.dump(cached, f, ensure_ascii=False)
            stage("cached")
            # Backfill credits if missing or from old search logic (credits_version < 6)
            # v3: added score-based confidence threshold to avoid wrong performers
            # v4: fixed bilingual title parsing (strip Latin transliteration suffixes)
            # v5: try both Artist-Song and Song-Artist orderings, pick best title match
            # v6: prefer YouTube artist if MusicBrainz performer doesn't match it
            if cached.get("credits_version", 1) < 6:
                credits = _fetch_credits(cached.get("title", title))
                cached["lyricist"]  = credits.get("lyricist")
                cached["composer"]  = credits.get("composer")
                cached["arranger"]  = credits.get("arranger")
                cached["performer"] = credits.get("performer")
                cached["lang"] = _detect_language(_lyrics_text(cached.get("segments", [])))
                cached["credits_version"] = 6
                with open(transcript_path, "w") as f:
                    json.dump(cached, f, ensure_ascii=False)
            return cached

    # LRClib synced first (reliable, purpose-built for lyrics), then YouTube
    # captions (flaky on servers — bot detection), then plain-text fallbacks
    stage("lrclib")
    lrc = _try_lrclib(title, duration)
    if lrc:
        segments, source = lrc, "lrclib"
    else:
        stage("captions")
        captions = _try_youtube_captions(url, vid_id)
        log.info(f"pipeline youtube_captions={'found' if captions else 'miss'} [{title[:50]!r}]")
        if captions:
            segments, source = captions, "youtube_captions"
        else:
            stage("plain")
            plain_lrclib = _try_lrclib_plain(title, duration)
            log.info(f"pipeline lrclib_plain={'found' if plain_lrclib else 'miss'} [{title[:50]!r}]")
            plain_genius = _try_genius(title) if not plain_lrclib else None
            log.info(f"pipeline genius={'found' if plain_genius else ('skip' if plain_lrclib else 'miss')} [{title[:50]!r}]")
            plain_ovh = _try_lyrics_ovh(title) if not (plain_lrclib or plain_genius) else None
            log.info(f"pipeline lyrics_ovh={'found' if plain_ovh else ('skip' if (plain_lrclib or plain_genius) else 'miss')} [{title[:50]!r}]")
            plain = plain_lrclib or plain_genius or plain_ovh
            if plain:
                source = "genius" if plain is plain_genius else ("lyrics_ovh" if plain is plain_ovh else "lrclib_plain")
                segments = plain
            else:
                segments, source = [], "none"

    credits = _fetch_credits(title)
    lang = _detect_language(_lyrics_text(segments))
    data = {
        "id": vid_id, "title": title, "url": url,
        "segments": segments, "source": source,
        "synced": source in SYNCED_SOURCES,
        "lyricist":  credits.get("lyricist"),
        "composer":  credits.get("composer"),
        "arranger":  credits.get("arranger"),
        "performer": credits.get("performer"),
        "lang": lang,
        "credits_version": 6,
    }
    with open(transcript_path, "w") as f:
        json.dump(data, f, ensure_ascii=False)

    return data


def _try_youtube_captions(url: str, vid_id: str):
    """Download YouTube auto-captions (VTT) and parse them. Returns segments or None."""
    vtt_base = os.path.join(STATIC_DIR, vid_id)
    ydl_opts = {
        "writeautomaticsub": True,
        "writesubtitles": True,
        "subtitleslangs": ["he", "en", "en-orig", "es", "fr", "de", "ru", "ar", "pt", "it"],
        "subtitlesformat": "vtt",
        "skip_download": True,
        "outtmpl": vtt_base,
        "quiet": True,
        "extractor_args": {"youtube": {"js_runtimes": ["nodejs"]}},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception:
        return None

    # Find the downloaded VTT file
    vtt_files = glob_mod.glob(f"{vtt_base}*.vtt")
    if not vtt_files:
        return None

    try:
        segments = _parse_vtt(vtt_files[0])
        if segments:
            log.info(f"using youtube captions ({len(segments)} segments)")
            return segments
    except Exception as e:
        log.warning(f"VTT parse failed: {e}")
    return None


def _parse_vtt(path: str):
    """Parse YouTube VTT with embedded word timestamps."""
    with open(path, encoding="utf-8") as f:
        content = f.read()

    segments = []
    # Each cue block: timestamp line + content lines
    cue_pattern = re.compile(
        r'(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})[^\n]*\n(.*?)(?=\n\n|\Z)',
        re.DOTALL
    )

    seen_texts = set()

    for m in cue_pattern.finditer(content):
        seg_start = _ts(m.group(1))
        seg_end = _ts(m.group(2))
        raw = m.group(3).strip()

        # Remove positioning tags like <c>, keep text and timestamps
        # YouTube format: <00:00:01.000><c> word</c>
        text_only = re.sub(r'<[^>]+>', '', raw).strip()
        if not text_only or text_only in seen_texts:
            continue
        seen_texts.add(text_only)

        # Parse word-level timestamps
        words = _parse_vtt_words(raw, seg_start, seg_end)

        segments.append({
            "text": text_only,
            "start": seg_start,
            "end": seg_end,
            "words": words,
        })

    return segments


def _parse_vtt_words(raw: str, seg_start: float, seg_end: float):
    """Extract word-level timing from a YouTube VTT cue line."""
    # Pattern: optional <timestamp> followed by <c> word </c>
    # Example: <00:00:02.219><c> Hello</c><00:00:02.459><c> world</c>
    token_pattern = re.compile(r'(?:(\d{2}:\d{2}:\d{2}[.,]\d{3}))?\s*<c>(.*?)</c>', re.DOTALL)

    words = []
    tokens = token_pattern.findall(raw)

    for i, (ts_str, word_text) in enumerate(tokens):
        word = word_text.strip()
        if not word:
            continue
        start = _ts(ts_str) if ts_str else seg_start
        # End is the next token's start, or seg_end for the last
        if i + 1 < len(tokens) and tokens[i + 1][0]:
            end = _ts(tokens[i + 1][0])
        else:
            end = seg_end
        words.append({"word": word, "start": start, "end": end})

    return words


def _ts(s: str) -> float:
    """Parse HH:MM:SS.mmm or HH:MM:SS,mmm to seconds."""
    s = s.replace(',', '.')
    parts = s.split(':')
    h, m, sec = int(parts[0]), int(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + sec


def _song_artist_attempts(title: str):
    """Candidate (song, artist) pairs for plain-lyrics APIs, best guesses first."""
    song, artist = _parse_title_artist(title)
    attempts = [(song, artist)]
    if artist:
        attempts.append((artist, song))  # YouTube titles can be "Song - Artist" too
    stripped = _norm_ws(_JUNK_WORDS.sub(' ', song))
    if stripped and stripped.lower() != song.lower():
        attempts.append((stripped, artist))
    # Multi-part bilingual songs: each dash segment is a candidate song name
    for seg in song.split(' - '):
        seg = seg.strip()
        if seg and seg.lower() != song.lower():
            attempts.append((seg, artist))
    seen, uniq = set(), []
    for s, a in attempts:
        key = (s.lower(), (a or "").lower())
        if s and key not in seen:
            seen.add(key)
            uniq.append((s, a))
    return uniq


def _try_genius(title: str) -> list:
    """Fetch plain lyrics from Genius API. Requires GENIUS_TOKEN env var."""
    token = os.environ.get("GENIUS_TOKEN")
    if not token:
        log.info("genius skipped: GENIUS_TOKEN not set")
        return None
    try:
        import lyricsgenius
        genius = lyricsgenius.Genius(
            token,
            skip_non_songs=True,
            excluded_terms=["(Remix)", "(Live)", "(Acoustic)", "(Instrumental)"],
            remove_section_headers=True,
            verbose=False,
            timeout=10,
        )
        for song_title, artist in _song_artist_attempts(title)[:3]:
            t0 = time.time()
            try:
                song = genius.search_song(song_title, artist or "")
            except Exception as e:
                log.warning(f"genius query failed [{song_title!r}/{artist!r}]: {e}")
                continue
            log.debug(f"genius {time.time()-t0:.1f}s [{song_title!r}/{artist!r}] "
                      f"{'hit' if song and song.lyrics else 'miss'}")
            if not song or not song.lyrics:
                continue
            lines = [l.strip() for l in song.lyrics.split("\n") if l.strip()]
            # Genius prepends title in first line like "Song Title Lyrics\n" — skip it
            if lines and lines[0].lower().endswith("lyrics"):
                lines = lines[1:]
            if lines:
                log.info(f"using genius lyrics ({len(lines)} lines) [{title[:50]!r}]")
                return [{"text": line, "start": None, "end": None, "words": []} for line in lines]
    except Exception as e:
        log.warning(f"genius fetch failed [{title[:50]!r}]: {e}")
    return None


def _try_lyrics_ovh(title: str) -> list:
    """Fetch plain lyrics from lyrics.ovh (free, no auth)."""
    for song_title, artist in _song_artist_attempts(title)[:3]:
        if not artist:
            continue
        url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(song_title)}"
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LetrasApp/1.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read())
            lyrics = data.get("lyrics", "").strip()
            log.debug(f"lyrics.ovh {time.time()-t0:.1f}s [{song_title!r}/{artist!r}] "
                      f"{'hit' if lyrics else 'miss'}")
            if lyrics:
                lines = [l.strip() for l in lyrics.split("\n") if l.strip()]
                if lines:
                    log.info(f"using lyrics.ovh ({len(lines)} lines) [{title[:50]!r}]")
                    return [{"text": line, "start": None, "end": None, "words": []} for line in lines]
        except Exception as e:
            log.debug(f"lyrics.ovh miss [{song_title!r}/{artist!r}]: {e}")
    return None


def _try_lrclib_plain(title: str, duration=None):
    """Fetch unsynced plain text lyrics from LRClib as static segments."""
    results = _lrclib_search(title, duration)
    best = _pick_lrclib_result(results, title, duration, want_synced=False)
    if best:
        lines = [l.strip() for l in best["plainLyrics"].split("\n") if l.strip()]
        if lines:
            log.info(f"using lrclib plain lyrics ({len(lines)} lines) [{title[:50]!r}]")
            return [{"text": line, "start": None, "end": None, "words": []} for line in lines]
    return None


def _try_lrclib(title: str, duration=None):
    """Search LRClib.net for synced lyrics best matching the title (and duration)."""
    results = _lrclib_search(title, duration)
    best = _pick_lrclib_result(results, title, duration, want_synced=True)
    if best:
        segments = _parse_lrc(best["syncedLyrics"])
        if segments:
            log.info(f"using lrclib synced ({len(segments)} lines) "
                     f"[{best.get('artistName')} — {best.get('trackName')}] for [{title[:50]!r}]")
            return segments
    return None


def _parse_lrc(lrc: str):
    """Parse LRC format [mm:ss.xx] text into segments."""
    pattern = re.compile(r'\[(\d{2}):(\d{2}\.\d+)\](.*)')
    lines = []
    for m in pattern.finditer(lrc):
        start = int(m.group(1)) * 60 + float(m.group(2))
        text = m.group(3).strip()
        if text:
            lines.append((start, text))

    segments = []
    for i, (start, text) in enumerate(lines):
        end = lines[i + 1][0] if i + 1 < len(lines) else start + 5.0
        segments.append({
            "text": text,
            "start": round(start, 3),
            "end": round(end, 3),
            "words": [],
        })
    return segments


def _lyrics_text(segments: list) -> str:
    """Concatenate all segment texts for language detection."""
    return " ".join(s.get("text", "") for s in segments[:20])


def _detect_language(text: str) -> str:
    """Detect language from lyrics text using Unicode script ranges and Latin heuristics."""
    if not text:
        return "en"
    counts = {
        "he": sum(1 for c in text if "\u0590" <= c <= "\u05FF"),
        "ar": sum(1 for c in text if "\u0600" <= c <= "\u06FF"),
        "ru": sum(1 for c in text if "\u0400" <= c <= "\u04FF"),
        "ja": sum(1 for c in text if "\u3040" <= c <= "\u30FF" or "\u4E00" <= c <= "\u9FFF"),
        "ko": sum(1 for c in text if "\uAC00" <= c <= "\uD7AF"),
        "zh": sum(1 for c in text if "\u4E00" <= c <= "\u9FFF"),
    }
    threshold = len(text) * 0.08
    best = max(counts, key=counts.get)
    if counts[best] > threshold:
        return best

    # Latin-script language detection via unique characters and common words
    lower = text.lower()
    words = set(re.findall(r"[a-záéíóúüñàâçèêëîïôùûœæ']+", lower))
    scores = {
        "es": (
            sum(1 for c in text if c in "áéíóúüñ¿¡") * 3 +
            len(words & {"el", "la", "los", "las", "de", "que", "y", "en", "un", "una",
                         "es", "se", "no", "con", "por", "su", "me", "mi", "te", "lo"})
        ),
        "pt": (
            sum(1 for c in text if c in "ãõáéíóúâêôçà") * 3 +
            len(words & {"o", "a", "os", "as", "de", "que", "e", "em", "um", "uma",
                         "não", "com", "por", "seu", "sua", "me", "te", "se", "ao"})
        ),
        "fr": (
            sum(1 for c in text if c in "àâçèéêëîïôùûœæ") * 3 +
            len(words & {"le", "la", "les", "de", "que", "et", "en", "un", "une",
                         "je", "tu", "il", "elle", "nous", "vous", "ils", "pas", "est"})
        ),
        "it": (
            sum(1 for c in text if c in "àèéìíîòóùú") * 3 +
            len(words & {"il", "lo", "la", "i", "gli", "le", "di", "che", "e", "in",
                         "un", "una", "non", "mi", "ti", "si", "ho", "sei", "sono"})
        ),
        "de": (
            sum(1 for c in text if c in "äöüß") * 4 +
            len(words & {"ich", "du", "er", "sie", "es", "wir", "die", "der", "das",
                         "und", "in", "ist", "nicht", "ein", "eine", "mit", "auf"})
        ),
    }
    best_latin = max(scores, key=scores.get)
    if scores[best_latin] >= 4:
        return best_latin
    return "en"


def _parse_title_artist(title: str):
    """Split a YouTube title into (song_title, artist_or_None), stripping suffixes like (Official Video).

    Handles bilingual YouTube titles like 'ריטה - מחכה - Rita - Mehake' by stripping
    the Latin transliteration suffix from Hebrew song titles (and vice versa).
    """
    cleaned = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', title).strip()
    # Titles like "חלק מהזמן | Idan Amedi" or "Idan Amedi | עידן עמדי - אלייך":
    # keep the pipe-separated part that carries "Artist - Song", else the longest part
    if '|' in cleaned:
        parts = [p.strip() for p in cleaned.split('|') if p.strip()]
        with_dash = [p for p in parts if ' - ' in p]
        cleaned = with_dash[0] if with_dash else max(parts, key=len, default=cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Split on a dash with space on at least one side ("ארצי- אף" / "ארצי -אף" / "א - ב").
    # A dash with no surrounding space is a hyphenated word (Jay-Z) — don't split.
    dash_split = re.split(r'\s+[-–—]\s*|\s*[-–—]\s+', cleaned, maxsplit=1)
    if len(dash_split) == 2 and dash_split[0].strip() and dash_split[1].strip():
        song = dash_split[1].strip()
        artist = dash_split[0].strip()
        # Strip transliteration suffixes: if song still contains " - ", drop any
        # segments that are purely Latin when the primary segment is non-Latin (Hebrew),
        # e.g. "מחכה - Rita - Mehake" → "מחכה"
        if ' - ' in song:
            is_non_latin = lambda s: any(ord(c) > 127 for c in s)
            segments = [s.strip() for s in song.split(' - ')]
            if is_non_latin(segments[0]):
                kept = [s for s in segments if is_non_latin(s)]
                song = ' - '.join(kept) if kept else segments[0]
        return song, artist
    # Handle format: Artist "Song Title" extra info  (e.g. live on Ed Sullivan)
    quote_match = re.search(r'["\u201c]([^"\u201d]+)["\u201d]', cleaned)
    if quote_match:
        song = quote_match.group(1).strip()
        before = cleaned[:quote_match.start()].strip().rstrip('-,').strip()
        artist = before if before else None
        return song, artist
    return cleaned or title, None


def _mb_search_recording(song_title: str, artist: str, headers: dict):
    """Search MusicBrainz for a recording. Returns (score, recordings list)."""
    mb_query = f'recording:"{song_title}"'
    if artist:
        mb_query += f' artist:"{artist}"'
    query = urllib.parse.urlencode({"query": mb_query, "fmt": "json", "limit": "5"})
    req = urllib.request.Request(
        f"https://musicbrainz.org/ws/2/recording?{query}", headers=headers
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
        recordings = json.loads(resp.read()).get("recordings", [])
    top_score = int(recordings[0].get("score", 0)) if recordings else 0
    return top_score, recordings


def _fetch_credits(title: str) -> dict:
    """Fetch lyricist and composer from MusicBrainz. Returns dict with 'lyricist' and/or 'composer'."""
    headers = {"User-Agent": "LetrasApp/1.0 (open-source letras project)"}
    song_title, artist = _parse_title_artist(title)
    try:
        # Step 1: search recording — try both "Artist - Song" and "Song - Artist"
        # orderings and pick whichever returns a top result whose title more closely
        # matches the queried song name. MusicBrainz scores are often equal (100) for
        # both, so title similarity is a better tiebreaker.
        def _title_sim(queried: str, returned: str) -> float:
            q, r = queried.lower().strip(), returned.lower().strip()
            if q == r:
                return 1.0
            if q in r or r in q:
                return 0.8
            q_words, r_words = set(q.split()), set(r.split())
            if q_words and r_words:
                return len(q_words & r_words) / max(len(q_words), len(r_words))
            return 0.0

        orig_artist = artist  # save before possible swap
        score, recordings = _mb_search_recording(song_title, artist, headers)
        sim = _title_sim(song_title, recordings[0]["title"]) if recordings else 0.0
        if artist:
            rev_score, rev_recordings = _mb_search_recording(artist, song_title, headers)
            rev_sim = _title_sim(artist, rev_recordings[0]["title"]) if rev_recordings else 0.0
            if rev_sim > sim or (rev_sim == sim and rev_score > score):
                score, recordings = rev_score, rev_recordings
                song_title, artist = artist, song_title  # keep consistent for min_score logic

        if not recordings:
            return {}
        rec = recordings[0]
        mbid = rec["id"]

        # Extract performer from artist-credit in search result,
        # but only if MusicBrainz is confident this is the right recording.
        # Without an artist in the query the top result may be a random cover.
        min_score = 90 if not artist else 70
        artist_credits = rec.get("artist-credit", [])
        performer_names = [a["artist"]["name"] for a in artist_credits if isinstance(a, dict) and "artist" in a]
        performer = ", ".join(performer_names) if performer_names and score >= min_score else None

        # If orig_artist from the YouTube title doesn't match MusicBrainz performer at all,
        # prefer the YouTube artist — it's authoritative for the specific recording being played.
        if orig_artist and performer:
            orig_low = orig_artist.lower()
            mb_low = performer.lower()
            if orig_low not in mb_low and mb_low not in orig_low:
                performer = orig_artist

        # Step 2: recording → work-rels + artist-rels (for arranger at recording level)
        req = urllib.request.Request(
            f"https://musicbrainz.org/ws/2/recording/{mbid}?inc=work-rels+artist-rels&fmt=json", headers=headers
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            rec_data = json.loads(resp.read())

        arrangers = []
        for rel in rec_data.get("relations", []):
            if rel.get("target-type") == "artist":
                role = rel.get("type", "").lower()
                name = rel.get("artist", {}).get("name", "")
                if role == "arranger" and name:
                    arrangers.append(name)

        work_mbid = next(
            (r["work"]["id"] for r in rec_data.get("relations", []) if r.get("target-type") == "work"),
            None,
        )
        if not work_mbid:
            return {"lyricist": None, "composer": None, "arranger": ", ".join(arrangers) or None, "performer": performer}

        # Step 3: work → artist relations (composer / lyricist / writer / arranger)
        req = urllib.request.Request(
            f"https://musicbrainz.org/ws/2/work/{work_mbid}?inc=artist-rels&fmt=json", headers=headers
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            work_data = json.loads(resp.read())

        lyricists, composers = [], []
        for rel in work_data.get("relations", []):
            role = rel.get("type", "").lower()
            name = rel.get("artist", {}).get("name", "")
            if not name:
                continue
            if role == "lyricist":
                lyricists.append(name)
            elif role == "composer":
                composers.append(name)
            elif role == "writer":
                lyricists.append(name)
                composers.append(name)
            elif role == "arranger":
                arrangers.append(name)

        return {
            "lyricist":  ", ".join(lyricists) or None,
            "composer":  ", ".join(composers) or None,
            "arranger":  ", ".join(arrangers) or None,
            "performer": performer,
        }
    except Exception as e:
        log.warning(f"credits fetch failed [{title[:50]!r}]: {e}")
        return {}


LANG_NAMES = {
    "en": "English", "he": "Hebrew", "es": "Spanish", "fr": "French",
    "de": "German", "ru": "Russian", "ar": "Arabic", "pt": "Portuguese",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "it": "Italian",
}


def _google_translate(text: str, source: str, target: str) -> str:
    """Translate using Google Translate unofficial endpoint (free, no key required)."""
    params = urllib.parse.urlencode({
        "client": "gtx",
        "sl": source,
        "tl": target,
        "dt": "t",
        "q": text,
    })
    req = urllib.request.Request(
        f"https://translate.googleapis.com/translate_a/single?{params}",
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read())
    # Response format: [[[translated, original, ...], ...], ...]
    translated = "".join(chunk[0] for chunk in data[0] if chunk[0])
    if not translated:
        raise ValueError("Empty response from Google Translate")
    return translated


def fetch_wikipedia_summary(song_title: str, artist: str = "", lang: str = "en") -> dict:
    """Search Wikipedia for a song article and return its summary."""
    wiki_lang = lang if lang else "en"

    def _search(query: str):
        encoded_query = urllib.parse.quote(query)
        url = (
            f"https://{wiki_lang}.wikipedia.org/w/api.php"
            f"?action=query&list=search&srsearch={encoded_query}"
            f"&srnamespace=0&srlimit=5&format=json"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Letras/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read()).get("query", {}).get("search", [])

    def _fetch_summary(title: str):
        encoded = urllib.parse.quote(title.replace(" ", "_"))
        url = f"https://{wiki_lang}.wikipedia.org/api/rest_v1/page/summary/{encoded}"
        req = urllib.request.Request(url, headers={"User-Agent": "Letras/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())

    def _pick_best(results):
        title_lower = song_title.lower()
        best = results[0]
        for r in results:
            if r["title"].lower() == title_lower:
                return r
            if title_lower in r["title"].lower() and title_lower not in best["title"].lower():
                best = r
        return best

    def _try_query(query: str):
        """Search, pick best result, fetch summary. Returns (best, summary_data) or None if disambiguation."""
        results = _search(query)
        if not results:
            return None
        best = _pick_best(results)
        summary = _fetch_summary(best["title"])
        if summary.get("type") == "disambiguation":
            return None
        return best, summary

    # Build fallback queries. When artist is empty we're searching for a person (credit name).
    base = f"{song_title} {artist}".strip()
    if artist:
        # Searching for a song — try with explicit disambiguation suffix
        queries = [base, f"{song_title} song", f"{song_title} canción"]
    else:
        # Searching for a person (credit name)
        queries = [base, f"{song_title} singer", f"{song_title} musician", f"{song_title} cantante"]

    result = None
    for query in queries:
        result = _try_query(query)
        if result:
            break

    if not result:
        return {"found": False}

    best, summary_data = result
    page_url = summary_data.get("content_urls", {}).get("desktop", {}).get("page", "")
    short_summary = summary_data.get("extract", "")

    # Fetch full article text via MediaWiki API
    full_content = short_summary
    try:
        params = urllib.parse.urlencode({
            "action": "query",
            "prop": "extracts",
            "explaintext": "true",
            "exsectionformat": "plain",
            "titles": best["title"],
            "format": "json",
        })
        req2 = urllib.request.Request(
            f"https://{wiki_lang}.wikipedia.org/w/api.php?{params}",
            headers={"User-Agent": "Letras/1.0"}
        )
        with urllib.request.urlopen(req2, timeout=8) as resp2:
            pages = json.loads(resp2.read()).get("query", {}).get("pages", {})
            if pages:
                page = next(iter(pages.values()))
                text = page.get("extract", "")
                if text:
                    full_content = text[:8000]  # cap to avoid huge payloads
    except Exception:
        pass  # fall back to short summary

    return {
        "found": True,
        "title": summary_data.get("title", ""),
        "summary": short_summary,
        "content": full_content,
        "url": page_url,
        "image": summary_data.get("thumbnail", {}).get("source") or None,
    }


def translate_segments(segments: list, target_lang: str, source_lang: str = None) -> list:
    """Translate lyrics segments using Google Translate (free, no key required)."""
    src = source_lang or "en"
    texts = [seg["text"].strip() for seg in segments]

    errors = []

    def translate_one(text):
        if not text:
            return ""
        try:
            result = _google_translate(text, src, target_lang)
            log.debug(f"translate ok '{text[:30]}'")
            return result
        except Exception as e:
            log.warning(f"translate FAILED '{text[:30]}': {e}")
            errors.append(str(e))
            return text

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(translate_one, texts))
    if errors:
        raise RuntimeError(f"Translation failed ({len(errors)} errors): {errors[0]}")
    return results


