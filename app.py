# Copyright (C) 2026 Ori Mosenzon and Claude (Anthropic AI)
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for details.

import os
import json
import time
from flask import Flask, render_template, jsonify, request
from transcriber import process_url, url_id, STATIC_DIR, search_songs, translate_segments, fetch_wikipedia_summary, _google_translate

app = Flask(__name__)

_jobs = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/process", methods=["POST"])
def process():
    url = request.json.get("url", "").strip()
    title = request.json.get("title", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    job_id = url_id(url)
    _jobs[job_id] = {"stage": "starting", "started_at": time.time()}

    def on_stage(stage):
        _jobs[job_id]["stage"] = stage
        _jobs[job_id]["elapsed"] = round(time.time() - _jobs[job_id]["started_at"], 1)

    try:
        data = process_url(url, title, on_stage)
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


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    results = search_songs(q)
    return jsonify({"results": results})



@app.route("/api/translate", methods=["POST"])
def translate():
    song_id = request.json.get("song_id", "").strip()
    target_lang = request.json.get("target_lang", "").strip()
    if not song_id or not target_lang:
        return jsonify({"error": "missing params"}), 400

    cache_path = os.path.join(STATIC_DIR, f"{song_id}.json")
    if not os.path.exists(cache_path):
        return jsonify({"error": "song not found"}), 404

    with open(cache_path) as f:
        data = json.load(f)

    translations = data.get("translations", {})
    if target_lang in translations:
        return jsonify({"translations": translations[target_lang]})

    try:
        translated = translate_segments(data["segments"], target_lang, data.get("lang"))
        data.setdefault("translations", {})[target_lang] = translated
        with open(cache_path, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        return jsonify({"translations": translated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
