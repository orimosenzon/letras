# Copyright (C) 2026 Ori Mosenzon and Claude (Anthropic AI)
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for details.
"""Measure the lyrics gap on a fixed sample of Hebrew songs.

For each query the script does what a user does: searches, takes the top song
result, and runs it through the full lyrics pipeline of the target server
(LRClib synced → YouTube captions → LRClib plain → Genius → lyrics.ovh).
It then tallies which source won, so the numbers reflect the real product
rather than one API in isolation.

Run against production (Genius is only configured there):
    python tools/measure_gap.py --base https://fun-production-d221.up.railway.app

Or against a local server:
    python tools/measure_gap.py --base http://localhost:5001

Results go to tools/gap_results.json (per song) and a summary is printed.
Songs are processed one at a time on purpose — LRClib is a free community
service and the server caps its own concurrency.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

# 40 Hebrew songs across eras and genres — classic singer-songwriters, 90s/2000s
# rock and pop, mizrahi, current pop, hip hop, and religious-folk. Written the way a
# user types them: artist then song, no punctuation.
SAMPLE = [
    # classics
    "שלמה ארצי תתארו לכם",
    "אריק איינשטיין עוף גוזל",
    "נעמי שמר ירושלים של זהב",
    "יהודית רביץ כמו צמח בר",
    "מתי כספי ברית עולם",
    "שלום חנוך מחכים למשיח",
    "אהוד בנאי עבודה עברית",
    "ריטה שביל הבריחה",
    "יהודה פוליקר הכל עובר חביבי",
    "חוה אלברשטיין לו יהי",
    # 90s-2000s rock and pop
    "אביב גפן עכשיו מעונן",
    "עידן רייכל בואי",
    "ברי סחרוף עבדים",
    "אתי אנקרי ראיתי אותך",
    "רמי קלינשטיין דרך ארוכה",
    "קורין אלאל אנטארקטיקה",
    "דנה ברגר מיליון דולר",
    "משינה רכבת לילה לקהיר",
    "אתניקס לוקח את הזמן",
    "היהודים אהבה בסופר",
    # mizrahi
    "אייל גולן מלכת היופי שלי",
    "שרית חדד כשהלב בוכה",
    "עומר אדם שני משוגעים",
    "זוהר ארגוב הפרח בגני",
    "דודו אהרון אין לי מקום",
    "אייל גולן יפה שלי",
    "פאר טסי דרך השלום",
    # current pop
    "נועה קירל פועמת",
    "סטטיק ובן אל טוב לי",
    "עדן חסון פעם בחיים",
    "אגם בוחבוט הצל",
    "עדן בן זקן שלי",
    "אנה זק מדהימה",
    "עומר אדם מודה אני",
    # hip hop
    "הדג נחש שירת הסטיקר",
    "רביד פלוטניק אל תיגע לי בפרפר",
    "טונה איך זה עובד",
    # religious-folk
    "ישי ריבו לשוב הביתה",
    "חנן בן ארי מלך העולם",
    "עמיר בניון ניצחת איתי הכל",
]

SYNCED = {"lrclib", "youtube_captions"}


def _get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "LetrasGapMeter/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post(url, body, timeout):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "LetrasGapMeter/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def measure_one(base, query):
    t0 = time.time()
    row = {"query": query}
    try:
        results = _get(f"{base}/api/search?q={urllib.parse.quote(query)}", 60).get("results", [])
    except Exception as e:
        row.update(error=f"search: {e}", elapsed=round(time.time() - t0, 1))
        return row
    # The first card the user would see that is an actual song (not a concert/compilation)
    pick = next((r for r in results if r.get("lrc_status") != "none"), None)
    if not pick:
        row.update(error="no song result", elapsed=round(time.time() - t0, 1))
        return row
    row.update(title=pick["title"], duration=pick.get("duration"), url=pick["url"])
    try:
        data = _post(f"{base}/api/process",
                     {"url": pick["url"], "title": pick["title"], "duration": pick.get("duration")}, 180)
    except Exception as e:
        row.update(error=f"process: {e}", elapsed=round(time.time() - t0, 1))
        return row
    row.update(source=data.get("source"), synced=bool(data.get("synced")),
               lines=len(data.get("segments") or []), lang=data.get("lang"),
               elapsed=round(time.time() - t0, 1))
    return row


def summarize(rows):
    total = len(rows)
    by_source = {}
    for r in rows:
        by_source[r.get("source") or r.get("error", "?")] = by_source.get(r.get("source") or r.get("error", "?"), 0) + 1
    synced = sum(1 for r in rows if r.get("synced"))
    plain = sum(1 for r in rows if r.get("source") and not r.get("synced") and r["source"] != "none")
    none = sum(1 for r in rows if r.get("source") == "none")
    errors = sum(1 for r in rows if r.get("error"))
    print(f"\n=== {total} songs ===")
    print(f"synced (line highlighting works): {synced}  ({100*synced/total:.0f}%)")
    print(f"plain text only:                  {plain}  ({100*plain/total:.0f}%)")
    print(f"no lyrics at all:                 {none}  ({100*none/total:.0f}%)")
    if errors:
        print(f"errors:                           {errors}")
    print("by source:", json.dumps(by_source, ensure_ascii=False))
    print("\nno lyrics:")
    for r in rows:
        if r.get("source") == "none":
            print(f"  - {r['query']}   ← {r.get('title', '')[:60]}")
    print("\nplain only:")
    for r in rows:
        if r.get("source") and not r.get("synced") and r["source"] != "none":
            print(f"  - [{r['source']}] {r['query']}   ← {r.get('title', '')[:60]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:5001")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "gap_results.json"))
    ap.add_argument("--limit", type=int, default=0, help="only the first N songs (for a quick check)")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    sample = SAMPLE[:args.limit] if args.limit else SAMPLE

    rows = []
    for i, q in enumerate(sample, 1):
        row = measure_one(base, q)
        rows.append(row)
        tag = row.get("source") or row.get("error")
        print(f"[{i:2}/{len(sample)}] {row['elapsed']:5.1f}s  {tag:<18} {q}  ← {row.get('title', '')[:55]}", flush=True)
        with open(args.out, "w") as f:
            json.dump({"base": base, "rows": rows}, f, ensure_ascii=False, indent=1)
    summarize(rows)


if __name__ == "__main__":
    main()
