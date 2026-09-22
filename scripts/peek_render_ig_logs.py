#!/usr/bin/env python3
import json, os, re, urllib.parse, urllib.request, datetime

key = os.environ["RENDER_API_KEY"]
sid = os.environ["RENDER_WEB_SERVICE_ID"]
headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

def get(url):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

mode = get(f"https://api.render.com/v1/services/{sid}/env-vars/SCRAPECREATORS_IG_MODE")
print("MODE", mode)
svc = get(f"https://api.render.com/v1/services/{sid}")
s = svc.get("service") or svc
owner = s.get("ownerId") or ""
print("service", s.get("name"), "owner", owner)
deploys = get(f"https://api.render.com/v1/services/{sid}/deploys?limit=5")
for x in deploys[:5]:
    dep = x.get("deploy") or x
    print("deploy", dep.get("status"), dep.get("finishedAt") or dep.get("createdAt"), ((dep.get("commit") or {}).get("message") or "")[:70])

start = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
q = urllib.parse.urlencode({"ownerId": owner, "resource": sid, "limit": "100", "startTime": start})
req = urllib.request.Request(f"https://api.render.com/v1/logs?{q}", headers=headers)
with urllib.request.urlopen(req, timeout=60) as r:
    raw = r.read().decode()
print("raw_head", raw[:250])
d = json.loads(raw)
logs = d if isinstance(d, list) else (d.get("logs") or d.get("entries") or d.get("events") or [])
print("n_logs", len(logs))
pat = re.compile(r"scrapecreators|ScrapeCreators|Primary video|Fallback video|Instagram blocked|Trying Scrape|embed scraper|yt-dlp", re.I)
n = 0
for e in logs:
    msg = (e.get("message") if isinstance(e, dict) else str(e)) or ""
    ts = (e.get("timestamp") if isinstance(e, dict) else "") or ""
    if pat.search(msg):
        print(ts, msg[:420])
        n += 1
print("matched", n)
print("---LAST25---")
for e in logs[-25:]:
    msg = (e.get("message") if isinstance(e, dict) else str(e)) or ""
    ts = (e.get("timestamp") if isinstance(e, dict) else "") or ""
    print(ts, msg[:280])
