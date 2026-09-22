#!/usr/bin/env python3
import json, os, re, urllib.parse, urllib.request, datetime

key = os.environ["RENDER_API_KEY"]
sid = os.environ["RENDER_WEB_SERVICE_ID"]
headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

def get_json(url):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

mode = get_json(f"https://api.render.com/v1/services/{sid}/env-vars/SCRAPECREATORS_IG_MODE")
print("MODE", mode)
svc = get_json(f"https://api.render.com/v1/services/{sid}")
s = svc.get("service") or svc
owner = s.get("ownerId") or ""
print("service", s.get("name"), "owner", owner)

# Since flip (~18:25Z Sep 21) — use 30h window
start = (datetime.datetime.utcnow() - datetime.timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
print("startTime", start)

logs = []
cursor = None
for page in range(20):
    params = {"ownerId": owner, "resource": sid, "limit": "100", "startTime": start}
    if cursor:
        params["cursor"] = cursor
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"https://api.render.com/v1/logs?{q}", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    batch = d.get("logs") or []
    logs.extend(batch)
    print(f"page {page+1}: +{len(batch)} total={len(logs)} hasMore={d.get('hasMore')}")
    if not d.get("hasMore"):
        break
    # cursor variants
    cursor = d.get("nextCursor") or d.get("cursor") or (batch[-1].get("id") if batch else None)
    if not cursor:
        break

print("n_logs", len(logs))

sc_ok = sc_fail = yt_fail = primary_fail = fallback_fail = ig_blocked = embed = 0
samples = []
for e in logs:
    msg = e.get("message") or ""
    ts = e.get("timestamp") or ""
    if "[scrapecreators] ok" in msg:
        sc_ok += 1
        if len(samples) < 15:
            samples.append(("SC_OK", ts, msg[:350]))
    elif "ScrapeCreators" in msg and ("failed" in msg.lower() or "fail" in msg.lower()):
        sc_fail += 1
        samples.append(("SC_FAIL", ts, msg[:350]))
    elif "Instagram blocked yt-dlp. Trying ScrapeCreators" in msg:
        ig_blocked += 1
        samples.append(("IG_BLOCKED_TO_SC", ts, msg[:350]))
    elif "Primary video extraction failed" in msg:
        primary_fail += 1
    elif "Fallback video extraction also failed" in msg:
        fallback_fail += 1
    elif "Trying last-resort curl_cffi embed scraper" in msg or "Last resort embed" in msg:
        embed += 1
        samples.append(("EMBED", ts, msg[:350]))
    elif "ERROR: [Instagram]" in msg:
        yt_fail += 1

print("\n=== COUNTS since", start, "===")
print("scrapecreators ok:", sc_ok)
print("scrapecreators fail lines:", sc_fail)
print("IG blocked -> try SC:", ig_blocked)
print("primary yt-dlp fail lines:", primary_fail)
print("fallback scrape fail lines:", fallback_fail)
print("embed last-resort lines:", embed)
print("yt-dlp Instagram ERROR lines:", yt_fail)
print("\n=== SAMPLE MATCHES ===")
for kind, ts, msg in samples[:20]:
    print(f"[{kind}] {ts} {msg}")

# credits burned?
credits = []
for e in logs:
    msg = e.get("message") or ""
    if "[scrapecreators] ok" in msg:
        m = re.search(r"credits=(\d+).*cached=(\w+)", msg)
        if m:
            credits.append((int(m.group(1)), m.group(2), e.get("timestamp")))
if credits:
    burned = sum(c for c,_,_ in credits)
    cached = sum(1 for c,ch,_ in credits if ch.lower()=="true")
    print(f"\nSC credits reported sum={burned}  cached_hits={cached}/{len(credits)}")
