# Copyright (C) 2026 Ori Mosenzon and Claude (Anthropic AI)
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for details.

import os
import json
import time
import urllib.request
import urllib.parse
from flask import Flask, render_template, jsonify, request, send_file
from transcriber import process_url, url_id, STATIC_DIR, search_songs, translate_segments, fetch_wikipedia_summary, _google_translate, _check_lrclib
from docx_export import build_docx, safe_filename

app = Flask(__name__)

_jobs = {}


@app.route("/")
def index():
    return render_template("index.html", og=None)


def _find_song_by_yt_id(yt_id):
    """Search cached JSON files for one whose YouTube URL contains yt_id."""
    import re
    pattern = re.compile(r'[?&]v=' + re.escape(yt_id) + r'(&|$)')
    for fname in os.listdir(STATIC_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(STATIC_DIR, fname)) as f:
                data = json.load(f)
            if pattern.search(data.get("url", "")):
                return data
        except Exception:
            continue
    return None


@app.route("/song/<video_id>")
def song_page(video_id):
    og = {"video_id": video_id, "url": request.url}
    data = _find_song_by_yt_id(video_id)
    if data:
        og["title"] = data.get("title", "Letras")
        og["description"] = "Letras by Ori Mosenzon"
        og["image"] = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    else:
        og["title"] = "Letras"
        og["description"] = "Letras by Ori Mosenzon"
        og["image"] = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    return render_template("index.html", og=og)


@app.route("/api/process", methods=["POST"])
def process():
    url = request.json.get("url", "").strip()
    title = request.json.get("title", "").strip()
    duration = request.json.get("duration")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    job_id = url_id(url)
    _jobs[job_id] = {"stage": "starting", "started_at": time.time()}

    def on_stage(stage):
        _jobs[job_id]["stage"] = stage
        _jobs[job_id]["elapsed"] = round(time.time() - _jobs[job_id]["started_at"], 1)

    try:
        data = process_url(url, title, on_stage, duration)
        _jobs[job_id]["stage"] = "done"
        return jsonify(data)
    except Exception as e:
        _jobs[job_id]["stage"] = "error"
        return jsonify({"error": str(e)}), 500


@app.route("/api/job_id", methods=["POST"])
def get_job_id():
    url = request.json.get("url", "").strip()
    return jsonify({"id": url_id(url)})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"stage": "unknown"})
    elapsed = round(time.time() - job["started_at"], 1)
    return jsonify({"stage": job["stage"], "elapsed": elapsed})


@app.route("/api/suggest")
def api_suggest():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"suggestions": []})
    try:
        url = (
            "https://suggestqueries.google.com/complete/search"
            f"?client=firefox&ds=yt&q={urllib.parse.quote(q)}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        suggestions = data[1] if len(data) > 1 else []
        return jsonify({"suggestions": suggestions[:8]})
    except Exception:
        return jsonify({"suggestions": []})


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    results = search_songs(q)
    return jsonify({"results": results})



@app.route("/api/lrc_check")
def api_lrc_check():
    title = request.args.get("title", "").strip()
    duration = request.args.get("duration", type=float)
    if not title:
        return jsonify({"status": "none"})
    try:
        return jsonify({"status": _check_lrclib(title, duration)})
    except Exception:
        return jsonify({"status": "none"})


def _load_song(song_id):
    """Return (data, cache_path) for a cached song, or (None, path) when it isn't cached."""
    cache_path = os.path.join(STATIC_DIR, f"{song_id}.json")
    if not os.path.exists(cache_path):
        return None, cache_path
    with open(cache_path) as f:
        return json.load(f), cache_path


def _translations_for(data, cache_path, target_lang):
    """Translated lines for a song, from cache or freshly translated (and then cached)."""
    cached = data.get("translations", {})
    if target_lang in cached:
        return cached[target_lang]
    translated = translate_segments(data["segments"], target_lang, data.get("lang"))
    data.setdefault("translations", {})[target_lang] = translated
    with open(cache_path, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    return translated


@app.route("/api/translate", methods=["POST"])
def translate():
    song_id = request.json.get("song_id", "").strip()
    target_lang = request.json.get("target_lang", "").strip()
    if not song_id or not target_lang:
        return jsonify({"error": "missing params"}), 400

    data, cache_path = _load_song(song_id)
    if data is None:
        return jsonify({"error": "song not found"}), 404

    try:
        return jsonify({"translations": _translations_for(data, cache_path, target_lang)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export/docx/<song_id>")
def export_docx(song_id):
    """Download the lyrics as a Word document, with the translation when a language is given."""
    target_lang = request.args.get("lang", "").strip()

    data, cache_path = _load_song(song_id)
    if data is None:
        return jsonify({"error": "song not found"}), 404
    if not data.get("segments"):
        return jsonify({"error": "no lyrics for this song"}), 404

    translations = None
    if target_lang and target_lang != data.get("lang"):
        try:
            translations = _translations_for(data, cache_path, target_lang)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        target_lang = ""

    buf = build_docx(data, translations, target_lang)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=safe_filename(data.get("title", ""), target_lang),
    )


@app.route("/api/wikipedia", methods=["POST"])
def wikipedia():
    song_title = request.json.get("song_title", "").strip()
    artist = request.json.get("artist", "").strip()
    lang = request.json.get("lang", "en").strip()
    if not song_title:
        return jsonify({"error": "missing song_title"}), 400
    try:
        result = fetch_wikipedia_summary(song_title, artist, lang)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/translate_text", methods=["POST"])
def translate_text():
    text = request.json.get("text", "").strip()
    source_lang = request.json.get("source_lang", "en").strip()
    target_lang = request.json.get("target_lang", "").strip()
    if not text or not target_lang:
        return jsonify({"error": "missing params"}), 400
    try:
        translated = _google_translate(text, source_lang, target_lang)
        return jsonify({"translated": translated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/credits")
def debug_credits():
    title = request.args.get("title", "").strip()
    if not title:
        return jsonify({"error": "no title"}), 400
    from transcriber import _fetch_credits, _parse_title_artist
    song_title, artist = _parse_title_artist(title)
    result = _fetch_credits(title)
    return jsonify({"input": title, "song_title": song_title, "artist": artist, "result": result})


if __name__ == "__main__":
    app.run(debug=True, port=5001, threaded=True)
