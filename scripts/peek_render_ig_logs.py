#!/usr/bin/env python3
import json, os, re, urllib.parse, urllib.request, datetime

key = os.environ["RENDER_API_KEY"]
sid = os.environ["RENDER_WEB_SERVICE_ID"]
headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

def get_json(url):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def fetch_logs(owner, text=None, start=None, limit=100):
    params = {
        "ownerId": owner,
        "resource": sid,
        "limit": str(limit),
        "direction": "backward",
        "type": "app",
    }
    if start:
        params["startTime"] = start
    if text:
        params["text"] = text
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"https://api.render.com/v1/logs?{q}", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    return d.get("logs") or [], d.get("hasMore")

mode = get_json(f"https://api.render.com/v1/services/{sid}/env-vars/SCRAPECREATORS_IG_MODE")
print("MODE", mode)
svc = get_json(f"https://api.render.com/v1/services/{sid}")
s = svc.get("service") or svc
owner = s.get("ownerId") or ""
print("service", s.get("name"), "owner", owner)

# Flip was 2026-09-21T18:25Z
start = "2026-09-21T18:25:00Z"
print("window_start", start)

queries = {
    "scrapecreators_ok": "scrapecreators] ok",
    "trying_sc": "Trying ScrapeCreators",
    "sc_primary_failed": "ScrapeCreators primary failed",
    "primary_video_failed": "Primary video extraction failed",
    "fallback_video_failed": "Fallback video extraction also failed",
    "embed": "last-resort curl_cffi",
    "ig_error": "ERROR: [Instagram]",
}

for name, text in queries.items():
    logs, more = fetch_logs(owner, text=text, start=start, limit=100)
    print(f"\n=== {name} n={len(logs)} hasMore={more} ===")
    # summarize credits if SC ok
    if name == "scrapecreators_ok":
        burned = 0
        cached = 0
        for e in logs:
            msg = e.get("message") or ""
            m = re.search(r"credits=(\d+).*cached=(\w+)", msg)
            if m:
                burned += int(m.group(1))
                if m.group(2).lower() == "true":
                    cached += 1
            print((e.get("timestamp") or "")[:19], msg[:220].replace("\n"," "))
        print(f"credits_sum={burned} cached_hits={cached}/{len(logs)}")
    else:
        for e in logs[:8]:
            print((e.get("timestamp") or "")[:19], (e.get("message") or "")[:220].replace("\n"," "))
        if len(logs) > 8:
            print(f"... +{len(logs)-8} more")
