from fastapi import FastAPI, Query, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import subprocess
import json
import time
import urllib.parse
import os
import shutil
import tempfile
import yt_dlp
import requests
from bs4 import BeautifulSoup
import re
import gc
import random
import asyncio
import threading
from contextvars import ContextVar
from typing import Optional

# Hot-reloaded Instagram cookies from ig_cookie_bot (survives until process restart).
_RUNTIME_IG_COOKIES = None
_RUNTIME_IG_COOKIES_AT: Optional[float] = None
_RUNTIME_IG_COOKIES_LOCK = threading.Lock()
_PROCESS_STARTED_AT = time.time()
CLIENT_API_KEY = (os.environ.get("LOOPHOLE_API_KEY") or "LOOPHOLE_SECURE_V1_TOKEN").strip()
OPS_SMOKE_HEADER = "x-loophole-ops-smoke"
OPS_ADMIN_HEADER = "x-loophole-ops-admin"
_OPS_SMOKE_REQUEST: ContextVar[bool] = ContextVar("ops_smoke_request", default=False)
_ig_smoke_disabled_logged = False
_BG_TASKS: set = set()


def _is_trusted_ops_smoke(request: Request, header_val: str) -> bool:
    """Ops smoke bypass only from loopback or cookie-bot admin key."""
    if header_val != "1":
        return False
    client_host = (request.client.host if request.client else "") or ""
    if client_host in ("127.0.0.1", "::1"):
        return True
    admin_expected = (os.environ.get("COOKIE_BOT_ADMIN_KEY") or "").strip()
    admin_provided = (request.headers.get(OPS_ADMIN_HEADER) or "").strip()
    return bool(admin_expected) and admin_provided == admin_expected


def _smoke_extract_body_ok(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "non-object JSON"
    if data.get("extractor") == "embed-nocookie":
        return False, "cookie-free embed path (cookies not verified)"
    if data.get("extractor") == "scrapecreators":
        urls = data.get("media_urls") or []
        if urls:
            return True, "ok"
        return False, "scrapecreators empty media_urls"
    urls = data.get("media_urls") or []
    formats = data.get("Formats") or []
    if urls or formats:
        return True, "ok"
    return False, "empty media_urls and Formats"


def _smoke_failure_should_alert(status_code: int, detail: str, body_reason: str) -> bool:
    if body_reason == "cookie-free embed path (cookies not verified)":
        return True
    if status_code == 200:
        return body_reason != "ok"
    lowered = (detail or "").lower()
    if any(
        phrase in lowered
        for phrase in (
            "unavailable",
            "private, deleted",
            "not downloadable",
            "stories cannot be downloaded",
        )
    ):
        return False
    if _is_ig_cookie_death_signal(detail):
        return True
    return status_code >= 500


def _safe_int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default

@asynccontextmanager
async def lifespan(app: FastAPI):
    for coro in (auto_update_ytdlp(), ig_smoke_watchdog()):
        task = asyncio.create_task(coro)
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
    yield

app = FastAPI(title="LoopHole Backend", version="1.0.0", lifespan=lifespan)


def parse_netscape_sessionid(cookie_text: str):
    """Return sessionid value from Netscape cookie text, or None."""
    for raw in (cookie_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, value = parts[5], parts[6]
        if name != "sessionid":
            continue
        value = (value or "").strip()
        if not value or value in ("0", "deleted", "null"):
            continue
        return value
    return None


def require_cookie_bot_admin(request: Request) -> None:
    """
    Cookie admin endpoints require COOKIE_BOT_ADMIN_KEY.
    Intentionally NOT the mobile client API key.
    """
    expected = (os.environ.get("COOKIE_BOT_ADMIN_KEY") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="COOKIE_BOT_ADMIN_KEY is not configured on this service",
        )
    provided = (request.headers.get("x-api-key") or "").strip()
    if provided != expected:
        raise HTTPException(status_code=403, detail="Unauthorized cookie-bot admin key")


def get_effective_ig_cookies():
    """Prefer bot hot-reload cookies, then IG_COOKIES env."""
    with _RUNTIME_IG_COOKIES_LOCK:
        if _RUNTIME_IG_COOKIES and _RUNTIME_IG_COOKIES.strip():
            return _RUNTIME_IG_COOKIES.strip()
    env_cookies = os.environ.get("IG_COOKIES")
    if env_cookies and env_cookies.strip():
        return env_cookies.strip()
    return None


def set_runtime_ig_cookies(cookie_content: str) -> None:
    global _RUNTIME_IG_COOKIES, _RUNTIME_IG_COOKIES_AT
    content = (cookie_content or "").strip()
    if content and not content.startswith("# Netscape HTTP Cookie File"):
        content = "# Netscape HTTP Cookie File\n" + content
    with _RUNTIME_IG_COOKIES_LOCK:
        _RUNTIME_IG_COOKIES = content
        _RUNTIME_IG_COOKIES_AT = time.time()


def _cookies_age_info() -> dict:
    """When effective IG cookies were last established (for ops status)."""
    with _RUNTIME_IG_COOKIES_LOCK:
        runtime_set = bool(_RUNTIME_IG_COOKIES and _RUNTIME_IG_COOKIES.strip())
        runtime_at = _RUNTIME_IG_COOKIES_AT
    env_set = bool(os.environ.get("IG_COOKIES", "").strip())
    now = time.time()
    if runtime_set and runtime_at:
        age = int(now - runtime_at)
        return {
            "cookies_effective_at": runtime_at,
            "cookies_age_sec": age,
            "cookies_age_hours": round(age / 3600, 2),
            "cookies_age_source": "runtime_hot_reload",
        }
    if env_set:
        age = int(now - _PROCESS_STARTED_AT)
        return {
            "cookies_effective_at": _PROCESS_STARTED_AT,
            "cookies_age_sec": age,
            "cookies_age_hours": round(age / 3600, 2),
            "cookies_age_source": "process_start_env",
        }
    return {
        "cookies_effective_at": None,
        "cookies_age_sec": None,
        "cookies_age_hours": None,
        "cookies_age_source": None,
    }

# Enable CORS for the Flutter client
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_ytdlp_base_cmd() -> list:
    """
    Checks if global 'yt-dlp' executable exists in the system path.
    Otherwise, falls back to running it via Python 'python -m yt_dlp'.
    This enables seamless compatibility between Render container environment
    and local development environments.
    """
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    return ["python", "-m", "yt_dlp"]

@app.api_route("/", methods=["GET", "HEAD"])
def health_check():
    return {"status": "alive", "message": "LoopHole Backend is running"}

def clear_ytdlp_cache():
    """
    Clears the yt-dlp cache to reset cookie and throttle states.
    """
    try:
        base_cmd = get_ytdlp_base_cmd()
        subprocess.run(base_cmd + ["--rm-cache-dir"], check=True, capture_output=True)
        return True
    except Exception as e:
        print(f"Error clearing yt-dlp cache: {e}")
        return False

@app.post("/clear-cache")
def clear_cache_endpoint(request: Request):
    require_cookie_bot_admin(request)

    success = clear_ytdlp_cache()
    if success:
        return {"status": "success", "message": "Backend cache cleared successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to clear yt-dlp cache")


@app.post("/admin/ig-cookies")
async def admin_set_ig_cookies(request: Request):
    """
    Hot-reload Instagram Netscape cookies from the cookie bot.
    Does not require a Render redeploy for the running instance.
    Auth: COOKIE_BOT_ADMIN_KEY (not the mobile client key).
    """
    require_cookie_bot_admin(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    cookies = (body or {}).get("cookies", "")
    if not cookies or not str(cookies).strip():
        raise HTTPException(status_code=400, detail="Missing cookies")

    cookie_text = str(cookies).strip()
    sessionid = parse_netscape_sessionid(cookie_text)
    if not sessionid:
        raise HTTPException(
            status_code=400,
            detail="Cookies must include a real Netscape sessionid cookie line",
        )

    set_runtime_ig_cookies(cookie_text)
    clear_ytdlp_cache()
    print("[admin] Runtime IG_COOKIES hot-reloaded by cookie bot")
    return {
        "status": "success",
        "message": "IG cookies hot-reloaded",
        "has_sessionid": True,
        "sessionid_len": len(sessionid),
    }


@app.get("/admin/ig-cookies")
def admin_get_ig_cookies(request: Request):
    """Export effective IG cookies so the bot can seed from live state."""
    require_cookie_bot_admin(request)
    with _RUNTIME_IG_COOKIES_LOCK:
        runtime_set = bool(_RUNTIME_IG_COOKIES and _RUNTIME_IG_COOKIES.strip())
    effective = get_effective_ig_cookies() or ""
    sessionid = parse_netscape_sessionid(effective)
    if runtime_set:
        source = "runtime"
    elif effective:
        source = "env"
    else:
        source = "none"
    return {
        "cookies": effective,
        "has_sessionid": bool(sessionid),
        "source": source,
    }


@app.get("/admin/ig-cookies/status")
def admin_ig_cookies_status(request: Request):
    require_cookie_bot_admin(request)

    with _RUNTIME_IG_COOKIES_LOCK:
        runtime_set = bool(_RUNTIME_IG_COOKIES and _RUNTIME_IG_COOKIES.strip())
    env_set = bool(os.environ.get("IG_COOKIES", "").strip())
    effective = get_effective_ig_cookies() or ""
    sessionid = parse_netscape_sessionid(effective)
    age = _cookies_age_info()
    return {
        "runtime_set": runtime_set,
        "env_set": env_set,
        "has_sessionid": bool(sessionid),
        "sessionid_len": len(sessionid) if sessionid else 0,
        "process_uptime_sec": int(time.time() - _PROCESS_STARTED_AT),
        **age,
    }

async def ig_smoke_watchdog():
    """
    Hourly in-process smoke extract on a known public reel.
    Catches cookie degradation between cookie-bot runs.
    """
    startup_delay = _safe_int_env("IG_SMOKE_STARTUP_DELAY_SEC", 45)
    interval = _safe_int_env("IG_SMOKE_INTERVAL_SEC", 3600)
    cooldown = _safe_int_env("IG_SMOKE_ALERT_COOLDOWN_SEC", 7200)
    await asyncio.sleep(max(15, startup_delay))

    last_alert_at = 0.0
    last_stale_cookie_alert_at = 0.0
    cookie_max_age = _safe_int_env("IG_COOKIE_MAX_AGE_SEC", 28800)
    global _ig_smoke_disabled_logged
    while True:
        age_info = _cookies_age_info()
        age_sec = age_info.get("cookies_age_sec")
        # Only alert on true hot-reload age — process_start_env is uptime,
        # not cookie birth time, and would false-positive after long uptime.
        if (
            age_info.get("cookies_age_source") == "runtime_hot_reload"
            and age_sec is not None
            and cookie_max_age > 0
            and age_sec >= cookie_max_age
        ):
            now = time.time()
            stale_cooldown = _safe_int_env("IG_COOKIE_STALE_ALERT_COOLDOWN_SEC", 21600)
            if now - last_stale_cookie_alert_at >= stale_cooldown:
                last_stale_cookie_alert_at = now
                server_name = (
                    os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "LoopHole Backend"
                )
                send_ops_alert(
                    f":hourglass: *LoopHole IG cookies may be stale*\n"
                    f"Server: `{server_name}`\n"
                    f"Age: `{age_info.get('cookies_age_hours')}h` "
                    f"(source: runtime_hot_reload)\n"
                    f"Threshold: `{cookie_max_age // 3600}h`\n"
                    f"Action: run cookie bot or refresh `IG_COOKIES`"
                )
                print(
                    f"[ig-smoke] cookies age {age_sec}s exceeds "
                    f"IG_COOKIE_MAX_AGE_SEC={cookie_max_age}"
                )

        smoke_url = (os.environ.get("COOKIE_BOT_SMOKE_URL") or "").strip()
        if smoke_url:
            port = (os.environ.get("PORT") or "3000").strip()
            try:
                resp = await asyncio.to_thread(
                    requests.get,
                    f"http://127.0.0.1:{port}/extract",
                    params={"url": smoke_url},
                    headers={
                        "x-api-key": CLIENT_API_KEY,
                        OPS_SMOKE_HEADER: "1",
                    },
                    timeout=90,
                )
                body_reason = "non-200"
                detail = ""
                if resp.status_code == 200:
                    try:
                        ok, body_reason = _smoke_extract_body_ok(resp.json())
                    except Exception:
                        ok, body_reason = False, "invalid JSON body"
                    if ok:
                        print(f"[ig-smoke] OK reel extract ({smoke_url[-32:]})")
                    else:
                        detail = body_reason
                        print(f"[ig-smoke] FAIL body check: {body_reason}")
                else:
                    try:
                        detail = (resp.json() or {}).get("detail", "")
                    except Exception:
                        detail = (resp.text or "")[:200]
                    print(
                        f"[ig-smoke] FAIL HTTP {resp.status_code} "
                        f"detail={str(detail)[:160]}"
                    )
                if resp.status_code != 200 or body_reason != "ok":
                    if _smoke_failure_should_alert(resp.status_code, str(detail), body_reason):
                        now = time.time()
                        if now - last_alert_at >= cooldown:
                            last_alert_at = now
                            server_name = (
                                os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "LoopHole Backend"
                            )
                            send_ops_alert(
                                f":warning: *LoopHole IG smoke extract failed*\n"
                                f"Server: `{server_name}`\n"
                                f"HTTP {resp.status_code} on hourly smoke reel\n"
                                f"Detail: `{str(detail or body_reason)[:200]}`\n"
                                f"Action: refresh IG cookies or run cookie bot"
                            )
            except Exception as e:
                print(f"[ig-smoke] watchdog error: {e}")
                now = time.time()
                if now - last_alert_at >= cooldown:
                    last_alert_at = now
                    server_name = (
                        os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "LoopHole Backend"
                    )
                    send_ops_alert(
                        f":warning: *LoopHole IG smoke watchdog error*\n"
                        f"Server: `{server_name}`\n"
                        f"Error: `{str(e)[:200]}`\n"
                        f"Action: check backend health / proxy / cookies"
                    )
        elif not _ig_smoke_disabled_logged:
            _ig_smoke_disabled_logged = True
            print("[ig-smoke] COOKIE_BOT_SMOKE_URL unset; hourly smoke disabled")

        await asyncio.sleep(max(300, interval))


async def auto_update_ytdlp():
    """
    Background loop that runs every 12 hours.
    It forces an upgrade of yt-dlp via pip and sends a Telegram alert if an update occurred.
    """
    while True:
        try:
            print("Running yt-dlp auto-updater check...")
            current_version = yt_dlp.version.__version__
            
            result = await asyncio.to_thread(
                subprocess.run,
                ["pip", "install", "-U", "yt-dlp"],
                capture_output=True, text=True
            )
            
            if "Successfully installed yt-dlp-" in result.stdout:
                server_name = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "LoopHole Backend"
                alert_msg = (
                    f"🔔 <b>LoopHole Auto-Updater</b>\n\n"
                    f"<b>Server:</b> <code>{server_name}</code>\n"
                    f"<code>yt-dlp</code> was successfully updated on the server to the latest version!\n"
                    f"<b>Previous Version:</b> {current_version}"
                )
                send_telegram_alert(alert_msg)
            else:
                print("yt-dlp is already up to date.")
        except Exception as e:
            print(f"Auto-updater failed: {e}")
            
        # Sleep for 12 hours (43200 seconds)
        await asyncio.sleep(43200)



@app.get("/diagnose-cookies")
def diagnose_cookies_endpoint(request: Request):
    """
    Scans and checks the validity of all cookies.txt files.
    Auth: COOKIE_BOT_ADMIN_KEY (ops only — not the mobile client key).
    """
    require_cookie_bot_admin(request)

    import glob
    cookie_patterns = ["*cookies*.txt", "cookies*.txt"]
    directories = ["/etc/secrets", "."]
    
    found_files = []
    for directory in directories:
        for pattern in cookie_patterns:
            found_files.extend(glob.glob(os.path.join(directory, pattern)))
            
    found_files = list(set(found_files))
    test_ig_url = "https://www.instagram.com/p/C-c5nKxS_Pq/"
    results = {}
    
    for cookie_file in found_files:
        filename = os.path.basename(cookie_file)
        temp_cookie_path = None
        
        try:
            temp_dir = tempfile.gettempdir()
            temp_cookie_path = os.path.join(temp_dir, f"diag_test_{filename}")
            shutil.copy2(cookie_file, temp_cookie_path)
            os.chmod(temp_cookie_path, 0o666)
        except Exception as copy_err:
            results[filename] = f"FAILED: Copy to temp failed: {copy_err}"
            continue

        ydl_opts = {
            'socket_timeout': 10,
            'quiet': True,
            'no_warnings': True,
            'cookiefile': temp_cookie_path,
            'user_agent': "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(test_ig_url, download=False)
                title = info.get('title', 'Unknown')
                results[filename] = f"WORKING! Title: {title[:20]}"
        except Exception as e:
            err = str(e).lower()
            if "login_via" in err or "login required" in err or "private" in err:
                results[filename] = "EXPIRED/INVALID (Instagram rejected login)"
            elif "unexpected_eof" in err or "ssl" in err:
                results[filename] = "BLOCKED at network layer (unexpected EOF/SSL drop)"
            else:
                results[filename] = f"FAILED: {str(e)[:50]}"
        finally:
            if temp_cookie_path and os.path.exists(temp_cookie_path):
                try:
                    os.remove(temp_cookie_path)
                except Exception as remove_err:
                    print(f"Error removing diagnostic temp cookie: {remove_err}")
                
    return {
        "status": "completed",
        "cookies_detected": len(found_files),
        "results": results
    }

def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in (url or "").lower()


def collect_tiktok_cookies(ydl) -> str:
    """
    TikTok signs its CDN playback URLs against the cookies of the session that
    requested them (see the tk=tt_chain_token parameter), so a client fetching
    the bare URL gets HTTP 403. Returning the jar lets the app replay the
    session that the signature was issued for.
    """
    try:
        pairs = []
        for cookie in ydl.cookiejar:
            domain = (cookie.domain or "").lstrip(".")
            if domain.endswith("tiktok.com") and cookie.value:
                pairs.append(f"{cookie.name}={cookie.value}")
        return "; ".join(pairs)
    except Exception as e:
        print(f"Could not collect TikTok cookies: {e}")
        return ""


def get_cookies_path(url: str = "") -> str:
    """
    Locates available cookies.txt files based on the requested platform.
    """
    url_lower = url.lower()
    
    # 1. Determine which cookie filenames to look for based on platform
    if "facebook.com" in url_lower or "fb.watch" in url_lower or "fb.gg" in url_lower:
        filenames = ["facebook_cookies.txt"]
    elif "pinterest.com" in url_lower or "pin.it" in url_lower:
        filenames = ["pinterest_cookies.txt"]
    elif "instagram.com" in url_lower:
        filenames = ["instagram_cookies.txt", "cookies.txt", "cookies2.txt", "cookies3.txt", "cookies4.txt", "cookies5.txt"]
    else:
        return None # No cookies for YouTube, TikTok, etc.

    paths = []
    
    # 2. Check environment variable path (legacy override)
    env_path = os.environ.get("COOKIES_FILE")
    if env_path and os.path.exists(env_path):
        paths.append(env_path)
        
    # 3. Check filenames in secrets and local folder
    directories = ["/etc/secrets", "."]
    
    for directory in directories:
        for filename in filenames:
            full_path = os.path.join(directory, filename)
            if os.path.exists(full_path):
                paths.append(full_path)
                
    # Deduplicate and return one randomly chosen path
    unique_paths = list(set(paths))
    if unique_paths:
        chosen = random.choice(unique_paths)
        print(f"Rotating Cookies: Selected session from {chosen} for URL {url}")
        return chosen
        
    return None

def get_writable_cookies_path(url: str = "") -> str:
    """
    Creates a writable copy of the cookies.txt file inside a temporary directory.
    """
    url_lower = url.lower()
    import uuid
    unique_id = uuid.uuid4().hex
    
    # 1. Runtime hot-reload / IG_COOKIES env — Instagram only
    if "instagram.com" in url_lower:
        ig_cookies = get_effective_ig_cookies()
        if ig_cookies:
            try:
                temp_dir = tempfile.gettempdir()
                writable_path = os.path.join(temp_dir, f"ytdlp_writable_cookies_{unique_id}.txt")
                
                cookie_content = ig_cookies.strip()
                if not cookie_content.startswith("# Netscape HTTP Cookie File"):
                    cookie_content = "# Netscape HTTP Cookie File\n" + cookie_content
                    
                with open(writable_path, "w", encoding="utf-8") as f:
                    f.write(cookie_content)
                    
                os.chmod(writable_path, 0o666)
                return writable_path
            except Exception as e:
                print(f"Error writing IG_COOKIES to writable temp path: {e}")

    # 2. Fallback to reading cookies from file paths
    source_path = get_cookies_path(url)
    if not source_path:
        return None
        
    try:
        temp_dir = tempfile.gettempdir()
        filename_base = os.path.basename(source_path)
        writable_path = os.path.join(temp_dir, f"writable_{unique_id}_{filename_base}")
        
        # Copy to the writable temp directory
        shutil.copy2(source_path, writable_path)
        os.chmod(writable_path, 0o666)
        
        return writable_path
    except Exception as e:
        print(f"Error copying cookies to writable path: {e}")
        return source_path



def extract_with_ytdlp(url: str, user_agent: str = None, use_cookies: bool = True) -> dict:
    """
    Runs yt-dlp with custom mobile User-Agent spoofing and optional cookie auth to bypass Meta blocks.
    Forces extraction of pre-merged best mobile-friendly MP4 formats to eliminate ffmpeg requirement.
    Uses programmatic yt-dlp to enforce strict 10s socket timeout.
    """
    if not user_agent:
        user_agent = os.environ.get("IG_USER_AGENT") or "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
    
    ydl_opts = {
        'socket_timeout': 10,
        'format': 'best[ext=mp4]/best',
        'noplaylist': True,
        'user_agent': user_agent,
        'quiet': True,
        'no_warnings': True,
    }
    
    proxy_url = os.environ.get("PROXY_URL")
    if proxy_url and "instagram.com" in url:
        ydl_opts['proxy'] = proxy_url
    
    headers = {}
    if "instagram.com" in url or "threads.net" in url:
        headers['Referer'] = 'https://www.instagram.com/'
    elif "tiktok.com" in url:
        headers['Referer'] = 'https://www.tiktok.com/'
    elif "x.com" in url or "twitter.com" in url:
        headers['Referer'] = 'https://x.com/'
        
    if headers:
        ydl_opts['http_headers'] = headers
        
    cookies_path = None
    if use_cookies:
        cookies_path = get_writable_cookies_path(url)
        if cookies_path:
            ydl_opts['cookiefile'] = cookies_path
        
    def _run(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and is_tiktok_url(url):
                cookie_header = collect_tiktok_cookies(ydl)
                if cookie_header:
                    info["_download_cookie"] = cookie_header
                info["_download_user_agent"] = user_agent
            return info

    try:
        return _run(ydl_opts)
    except Exception as e:
        error_msg = str(e).lower()
        if "proxy" in ydl_opts and ("502" in error_msg or "proxy" in error_msg or "ssl" in error_msg or "eof" in error_msg):
            ydl_opts.pop("proxy", None)
            return _run(ydl_opts)
        else:
            raise e
    finally:
        if cookies_path and os.path.exists(cookies_path):
            try:
                os.remove(cookies_path)
            except:
                pass

def extract_media_generic(url: str, user_agent: str = None, use_cookies: bool = True) -> dict:
    """
    Runs yt-dlp without the strict video format filter and allows playlists/carousels.
    Used for extracting photos, carousels, or fallback media.
    Uses programmatic yt-dlp to enforce strict 10s socket timeout.
    """
    if not user_agent:
        user_agent = os.environ.get("IG_USER_AGENT") or "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
        
    ydl_opts = {
        'socket_timeout': 10,
        'user_agent': user_agent,
        'quiet': True,
        'no_warnings': True,
    }
    
    proxy_url = os.environ.get("PROXY_URL")
    if proxy_url and "instagram.com" in url:
        ydl_opts['proxy'] = proxy_url
    
    headers = {}
    if "instagram.com" in url or "threads.net" in url:
        headers['Referer'] = 'https://www.instagram.com/'
    elif "tiktok.com" in url:
        headers['Referer'] = 'https://www.tiktok.com/'
    elif "x.com" in url or "twitter.com" in url:
        headers['Referer'] = 'https://x.com/'
        
    if headers:
        ydl_opts['http_headers'] = headers
        
    cookies_path = None
    if use_cookies:
        cookies_path = get_writable_cookies_path(url)
        if cookies_path:
            ydl_opts['cookiefile'] = cookies_path
        
    def _run(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and is_tiktok_url(url):
                cookie_header = collect_tiktok_cookies(ydl)
                if cookie_header:
                    info["_download_cookie"] = cookie_header
                info["_download_user_agent"] = user_agent
            return info

    try:
        return _run(ydl_opts)
    except Exception as e:
        error_msg = str(e).lower()
        if "proxy" in ydl_opts and ("502" in error_msg or "proxy" in error_msg or "ssl" in error_msg or "eof" in error_msg):
            ydl_opts.pop("proxy", None)
            return _run(ydl_opts)
        else:
            raise e
    finally:
        if cookies_path and os.path.exists(cookies_path):
            try:
                os.remove(cookies_path)
            except:
                pass

def send_ops_alert(message: str):
    """Post ops alerts to Slack (preferred). Telegram is optional legacy."""
    webhook = (os.environ.get("SLACK_WEBHOOK_URL") or "").strip()
    if webhook:
        try:
            resp = requests.post(webhook, json={"text": message}, timeout=5)
            if resp.status_code < 400:
                return
            print(f"Slack alert failed HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"Failed to send Slack alert: {e}")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        if not webhook:
            print(f"[ops-alert] no SLACK_WEBHOOK_URL or Telegram configured; dropped: {message[:120]}")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")


def send_telegram_alert(message: str):
    # Back-compat alias used by existing call sites.
    send_ops_alert(message)


# --- IG extract failure rate alerting (live traffic) ---
_IG_ALERT_WINDOW_SEC = _safe_int_env("IG_ALERT_WINDOW_SEC", 600)
_IG_ALERT_THRESHOLD = _safe_int_env("IG_ALERT_THRESHOLD", 3)
_IG_ALERT_COOLDOWN_SEC = _safe_int_env("IG_ALERT_COOLDOWN_SEC", 1800)
_ig_failure_urls: list[tuple[str, float]] = []
_ig_last_alert_time: float = 0.0
_IG_ALERT_LOCK = threading.Lock()

_IG_COOKIE_DEATH_PHRASES = (
    "failed to parse json",
    "cookies have expired",
    "empty media response",
    "instagram blocked the request",
    "login required",
    "rate-limit reached",
    "login_via",
    "instagram sent an empty media response",
)

_IG_USER_ERROR_EXCLUDES = (
    "stories cannot be downloaded",
    "not downloadable",
    "photos/images, not a video",
    "please copy a link to a specific video",
    "login/error link",
)


def _normalize_ig_url(url: str) -> str:
    return (url or "").split("?")[0].rstrip("/").lower()


def _is_downloadable_ig_url(url: str) -> bool:
    u = (url or "").lower()
    if "instagram.com" not in u:
        return False
    return any(marker in u for marker in ("/p/", "/reel/", "/reels/", "/tv/"))


def _is_ig_cookie_death_signal(text: str) -> bool:
    d = (text or "").lower()
    if any(phrase in d for phrase in _IG_USER_ERROR_EXCLUDES):
        return False
    return any(phrase in d for phrase in _IG_COOKIE_DEATH_PHRASES)


def record_ig_extract_failure(url: str, signal: str) -> None:
    """Slack once when distinct IG reel/post URLs fail with cookie-death signals."""
    if _OPS_SMOKE_REQUEST.get():
        return
    if not _is_downloadable_ig_url(url):
        return
    if not _is_ig_cookie_death_signal(signal):
        return

    url_key = _normalize_ig_url(url)
    now = time.time()
    global _ig_last_alert_time
    count = 0
    sample = (signal or "").replace("\n", " ")[:200]
    with _IG_ALERT_LOCK:
        _ig_failure_urls[:] = [
            (u, t)
            for u, t in _ig_failure_urls
            if now - t < _IG_ALERT_WINDOW_SEC
        ]
        seen = {u for u, _ in _ig_failure_urls}
        if url_key in seen:
            return
        _ig_failure_urls.append((url_key, now))
        count = len(_ig_failure_urls)

        if count < _IG_ALERT_THRESHOLD:
            return
        if now - _ig_last_alert_time < _IG_ALERT_COOLDOWN_SEC:
            return

        _ig_last_alert_time = now
        _ig_failure_urls.clear()

    server_name = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "LoopHole Backend"
    window_min = max(1, _IG_ALERT_WINDOW_SEC // 60)
    msg = (
        f":rotating_light: *LoopHole IG extract failures*\n"
        f"Server: `{server_name}`\n"
        f"{count} distinct reel/post URLs failed in {window_min} min "
        f"(threshold {_IG_ALERT_THRESHOLD})\n"
        f"Sample: `{sample}`\n"
        f"Action: refresh IG cookies (cookie bot or `POST /admin/ig-cookies`)"
    )
    send_ops_alert(msg)

def fallback_instagram_scrape(url: str, use_cookies: bool = True) -> dict:
    """
    Fallback extractor using browser impersonation with an iPhone Safari User-Agent
    if standard Chrome-spoofed extraction is throttled or blocked.
    """
    try:
        # Alternate high-quality iPhone Safari mobile UA, fall back to IG_USER_AGENT if cookies are active
        ua = os.environ.get("IG_USER_AGENT") or "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        return extract_with_ytdlp(url, user_agent=ua, use_cookies=use_cookies)
    except Exception as e:
        raise Exception(f"Fallback mobile scraper also failed: {str(e)}")

def fallback_instagram_scrape_generic(url: str, use_cookies: bool = True) -> dict:
    """
    Fallback generic extractor using browser impersonation with an iPhone Safari User-Agent.
    """
    try:
        ua = os.environ.get("IG_USER_AGENT") or "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        return extract_media_generic(url, user_agent=ua, use_cookies=use_cookies)
    except Exception as e:
        raise Exception(f"Fallback mobile generic scraper also failed: {str(e)}")


SCRAPECREATORS_API_URL = "https://api.scrapecreators.com/v1/instagram/post"


def _scrapecreators_api_key() -> str:
    return (os.environ.get("SCRAPECREATORS_API_KEY") or "").strip()


def _scrapecreators_ig_mode() -> str:
    """fallback (default) | primary"""
    return (os.environ.get("SCRAPECREATORS_IG_MODE") or "fallback").strip().lower()


def _scrapecreators_configured() -> bool:
    return bool(_scrapecreators_api_key())


def _scrapecreators_download_media() -> bool:
    return (os.environ.get("SCRAPECREATORS_DOWNLOAD_MEDIA") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _parse_scrapecreators_media(media: dict) -> tuple[str, list[str], str]:
    """Return (media_type, urls, thumbnail) from xdt_shortcode_media."""
    if not isinstance(media, dict):
        raise ValueError("missing xdt_shortcode_media")

    thumbnail = (
        media.get("thumbnail_src")
        or media.get("display_url")
        or ""
    )
    typename = str(media.get("__typename") or "")
    urls: list[str] = []

    if typename == "XDTGraphSidecar" or media.get("edge_sidecar_to_children"):
        media_type = "carousel"
        edges = (media.get("edge_sidecar_to_children") or {}).get("edges") or []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            if node.get("is_video") and node.get("video_url"):
                urls.append(str(node["video_url"]))
            elif node.get("display_url"):
                urls.append(str(node["display_url"]))
    elif media.get("is_video") or typename == "XDTGraphVideo" or media.get("video_url"):
        media_type = "video"
        if media.get("video_url"):
            urls.append(str(media["video_url"]))
        elif media.get("display_url"):
            urls.append(str(media["display_url"]))
    else:
        media_type = "photo"
        if media.get("display_url"):
            urls.append(str(media["display_url"]))

    urls = [u for u in urls if u.startswith("http")]
    if not urls:
        raise ValueError("ScrapeCreators response had no media URLs")
    if not thumbnail and urls:
        thumbnail = urls[0]
    return media_type, urls, thumbnail


def _scrapecreators_title(media: dict) -> str:
    edges = (media.get("edge_media_to_caption") or {}).get("edges") or []
    if edges:
        text = ((edges[0] or {}).get("node") or {}).get("text") or ""
        text = str(text).strip()
        if text:
            return text[:200]
    owner = media.get("owner") or {}
    username = str(owner.get("username") or "").strip()
    if username:
        return f"@{username} on Instagram"
    return "Instagram Post"


def _scrapecreators_user_error(payload: dict) -> HTTPException | None:
    """Map ScrapeCreators error fields to a client-facing response."""
    if not isinstance(payload, dict):
        return None
    err = str(payload.get("error") or "").strip().lower()
    msg = str(payload.get("message") or "").strip()
    if err in ("not_found", "private", "login_required", "invalid_url"):
        return HTTPException(
            status_code=400,
            detail=msg or "This Instagram post is unavailable. It may be private, deleted, or restricted.",
        )
    if err == "forbidden" or "age restricted" in msg.lower():
        return HTTPException(
            status_code=400,
            detail=msg or "This Instagram post is age-restricted and cannot be downloaded.",
        )
    return None


def extract_instagram_scrapecreators(url: str) -> dict:
    """
    Instagram extract via ScrapeCreators API (paid fallback / optional primary).
    Default: 1 credit/request (IG CDN URLs). download_media=true costs 10 credits.
    """
    api_key = _scrapecreators_api_key()
    if not api_key:
        raise ValueError("SCRAPECREATORS_API_KEY not configured")

    params = {
        "url": url,
        "include_play_count": "false",
    }
    if _scrapecreators_download_media():
        params["download_media"] = "true"
    cache_age = (os.environ.get("SCRAPECREATORS_CACHE_MAX_AGE") or "7d").strip()
    if cache_age:
        params["cache_max_age"] = cache_age

    resp = requests.get(
        SCRAPECREATORS_API_URL,
        params=params,
        headers={"x-api-key": api_key},
        timeout=45,
    )

    payload = None
    try:
        payload = resp.json() if resp.content else None
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        user_err = _scrapecreators_user_error(payload)
        if user_err:
            print(
                f"[scrapecreators] blocked http={resp.status_code} "
                f"error={payload.get('error')} — {str(payload.get('message') or '')[:120]}"
            )
            raise user_err
        if resp.status_code == 403:
            raise ValueError("ScrapeCreators API key rejected (403)")
    elif resp.status_code == 403:
        raise ValueError("ScrapeCreators API key rejected (403)")

    if resp.status_code >= 400:
        detail = (resp.text or "")[:240]
        raise ValueError(f"ScrapeCreators HTTP {resp.status_code}: {detail}")

    if not isinstance(payload, dict):
        raise ValueError("ScrapeCreators returned invalid JSON")
    if not payload.get("success"):
        raise ValueError(
            str(payload.get("message") or "").strip() or "ScrapeCreators returned success=false"
        )

    media = ((payload.get("data") or {}).get("xdt_shortcode_media")) or {}
    media_type, media_urls, thumbnail = _parse_scrapecreators_media(media)
    title = _scrapecreators_title(media)
    credits = payload.get("credits_charged")
    cached = payload.get("cached")
    print(
        f"[scrapecreators] ok type={media_type} urls={len(media_urls)} "
        f"credits={credits} cached={cached}"
    )

    info: dict = {
        "title": title,
        "url": media_urls[0],
        "thumbnail": thumbnail,
        "_extractor": "scrapecreators",
        "_media_type": media_type,
    }
    if len(media_urls) > 1:
        entries = []
        if media_type == "carousel":
            edges = (media.get("edge_sidecar_to_children") or {}).get("edges") or []
            for edge in edges:
                node = (edge or {}).get("node") or {}
                if node.get("is_video") and node.get("video_url"):
                    entries.append({"url": str(node["video_url"]), "ext": "mp4"})
                elif node.get("display_url"):
                    entries.append({"url": str(node["display_url"]), "ext": "jpg"})
        if not entries:
            entries = [
                {"url": u, "ext": "mp4" if media_type == "video" else "jpg"}
                for u in media_urls
            ]
        info["entries"] = entries
    return info

def extract_instagram_media(url: str) -> dict:
    """
    Extracts Instagram photos, carousels, and videos using the PUBLIC
    embed page (/p/{shortcode}/embed/captioned/).
    
    This approach:
    - Works WITHOUT login or cookies (public posts only)
    - Uses curl_cffi Chrome TLS fingerprint to avoid bot detection
    - Parses CDN image/video URLs directly from the embed page HTML
    - Handles single photos, carousels, and videos
    """
    from curl_cffi import requests as cffi_requests
    import re, html as html_lib
    
    # 1. Extract shortcode
    match = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid Instagram post URL.")
    shortcode = match.group(1)
    
    # 2. Fetch the public embed page
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    
    proxy_url = os.environ.get("PROXY_URL")
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    
    try:
        resp = cffi_requests.get(
            embed_url,
            headers=headers,
            proxies=proxies,
            impersonate="chrome131",
            timeout=30, # Increased to 30s to accommodate mobile proxy latency
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Instagram is temporarily unavailable. Please try again later. (Error: {str(e)})")
    
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Instagram post not found or deleted.")
    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Instagram embed page returned HTTP {resp.status_code}.")
    
    text = resp.text
    
    media_urls = []
    media_type = "photo"
    thumbnail = ""
    
    # 3. Find the exact JSON payload in the embed HTML
    start_str = r'\"shortcode_media\":{'
    idx = text.find(start_str)
    if idx == -1:
        start_str = r'"shortcode_media":{'
        idx = text.find(start_str)

    if idx != -1:
        json_start = idx + len(start_str) - 1
        brace_count = 0
        in_string = False
        escape_next = False
        json_end = -1
        
        for i in range(json_start, len(text)):
            char = text[i]
            if escape_next:
                escape_next = False
                continue
            if char == '\\':
                escape_next = True
                continue
            if char == '"':
                in_string = not in_string
                continue
                
            if not in_string:
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = i + 1
                        break
                        
        if json_end != -1:
            raw_json = text[json_start:json_end]
            if '\\"' in raw_json:
                clean_json = raw_json.replace('\\"', '"').replace('\\\\', '\\').replace('\\/', '/')
            else:
                clean_json = raw_json
                
            try:
                import json
                media = json.loads(clean_json)
                typename = media.get("__typename", "")
                
                if typename == "GraphSidecar":
                    media_type = "carousel"
                    edges = media.get("edge_sidecar_to_children", {}).get("edges", [])
                    for edge in edges:
                        node = edge.get("node", {})
                        if node.get("is_video") and "video_url" in node:
                            media_urls.append(node["video_url"])
                        elif "display_url" in node:
                            media_urls.append(node["display_url"])
                elif typename == "GraphVideo" or media.get("is_video"):
                    media_type = "video"
                    if "video_url" in media:
                        media_urls.append(media["video_url"])
                    elif "display_url" in media:
                        media_urls.append(media["display_url"])
                else:
                    media_type = "photo"
                    if "display_url" in media:
                        media_urls.append(media["display_url"])
                
                if "display_url" in media:
                    thumbnail = media["display_url"]
            except Exception:
                pass
    
    # 4. Fallback if JSON parsing fails
    if not media_urls:
        all_cdn = re.findall(r'https://[^\s"\'<>]+\.(?:fbcdn|cdninstagram)\.net/[^\s"\'<>]+', text)
        all_cdn = [html_lib.unescape(u) for u in all_cdn]
        
        def quality_score(u: str) -> int:
            if any(x in u for x in ['p1080x1080', 'p720x720', 'e35', 'dst-jpg_e35']):
                return 3
            if any(x in u for x in ['p480x480', 'e15_s640', 'dst-jpg_e15&']):
                return 2
            if any(x in u for x in ['p240x240', 's150x150', 'p320x320', 'e15_p', 's100x100']):
                return -1
            return 1
            
        post_media = [u for u in all_cdn if quality_score(u) >= 1]
                
        video_urls = re.findall(r'src="(https://[^"]+\.mp4[^"]*)"', text)
        video_urls = [html_lib.unescape(v) for v in video_urls]
        
        if video_urls:
            # It's a video!
            media_type = "video"
            media_urls = [video_urls[0]]
            thumbnail = post_media[0] if post_media else video_urls[0]
        else:
            if any(x in url for x in ["/reel/", "/reels/", "/tv/"]):
                raise Exception("Instagram embed page did not contain the video URL (only thumbnail).")
            # It's a photo/carousel
            media_type = "photo"
            if post_media:
                media_urls = [post_media[0]]
                thumbnail = post_media[0]
            else:
                og_match = re.search(r'property="og:image"\s+content="([^"]+)"', text)
                if og_match:
                    media_urls = [html_lib.unescape(og_match.group(1))]
                    thumbnail = media_urls[0]
                else:
                    raise HTTPException(status_code=400, detail="Could not extract media. The post may be private.")

    return {
        "media_type": media_type,
        "media_urls": media_urls,
        "Video Title": "Instagram Post",
        "Thumbnail URL": thumbnail,
        "extractor": "embed-nocookie",
    }


# --- ERROR-ONLY CACHE ---
# Remembers failed extractions for 3 minutes.
# Prevents proxy bandwidth waste when users retry the same broken/private link.
# Safe: if cache lookup fails for any reason, code falls through to normal extraction.
ERROR_CACHE = {}
ERROR_CACHE_TTL = 180  # 3 minutes
_ERROR_CACHE_MAX = _safe_int_env("ERROR_CACHE_MAX_ENTRIES", 2000)


def _prune_error_cache(now: Optional[float] = None) -> None:
    """Drop expired entries; cap size to avoid unbounded growth on long-lived workers."""
    now = now if now is not None else time.time()
    expired = [k for k, v in ERROR_CACHE.items() if now >= v.get("expiry", 0)]
    for key in expired:
        ERROR_CACHE.pop(key, None)
    max_entries = max(100, _ERROR_CACHE_MAX)
    if len(ERROR_CACHE) <= max_entries:
        return
    # ponytail: O(n log n) sort on overflow only; upgrade path = OrderedDict LRU
    for key, _ in sorted(
        ERROR_CACHE.items(), key=lambda kv: kv[1].get("expiry", 0)
    )[: len(ERROR_CACHE) - max_entries]:
        ERROR_CACHE.pop(key, None)


def _error_cache_store(key: str, status: int, detail: str) -> None:
    now = time.time()
    _prune_error_cache(now)
    ERROR_CACHE[key] = {
        "status": status,
        "detail": detail,
        "expiry": now + ERROR_CACHE_TTL,
    }

@app.get("/extract")
def extract_video(
    request: Request,
    url: str = Query(..., description="The video/photo URL to extract metadata from"),
    x_api_key: str = Header(None, description="Secure API key for client authentication"),
):
    ops_smoke = _is_trusted_ops_smoke(
        request, request.headers.get(OPS_SMOKE_HEADER, "").strip()
    )
    if x_api_key != CLIENT_API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized client signature")
        
    if not url:
        raise HTTPException(status_code=400, detail="URL query parameter is required")

    smoke_token = _OPS_SMOKE_REQUEST.set(ops_smoke)
    from_error_cache = False
    try:
        url_decoded = urllib.parse.unquote(url)
        url_lower = url_decoded.lower()

        # --- ERROR CACHE CHECK (key from original URL, before share-link unwrap) ---
        _cache_key = url_decoded.split('?')[0].rstrip('/')
        if not ops_smoke:
            _cached = ERROR_CACHE.get(_cache_key)
            if _cached and time.time() < _cached["expiry"]:
                print(f"[ERROR CACHE HIT] Returning cached error for: {_cache_key[-50:]}")
                from_error_cache = True
                raise HTTPException(status_code=_cached["status"], detail=_cached["detail"])
            elif _cached:
                ERROR_CACHE.pop(_cache_key, None)


        # Friendly check for Pinterest login/error redirects
        if "pinterest.com" in url_lower and "show_error=true" in url_lower:
            raise HTTPException(
                status_code=400,
                detail="Oops! This looks like a Pinterest login/error link. 📌 Please copy the link using the 'Share' button in the Pinterest app instead!"
            )

        # Check if user copied the generic homepage by mistake
        base_urls = [
            "https://pinterest.com", "https://www.pinterest.com",
            "https://instagram.com", "https://www.instagram.com",
            "https://facebook.com", "https://www.facebook.com",
            "https://tiktok.com", "https://www.tiktok.com",
            "https://youtube.com", "https://www.youtube.com",
            "http://pinterest.com", "http://www.pinterest.com",
            "http://instagram.com", "http://www.instagram.com",
            "http://facebook.com", "http://www.facebook.com",
            "http://tiktok.com", "http://www.tiktok.com",
            "http://youtube.com", "http://www.youtube.com"
        ]
        clean_url = url_lower.strip().rstrip('/')
        if clean_url in base_urls:
            raise HTTPException(
                status_code=400,
                detail="Please copy a link to a specific video or post, not the homepage!"
            )
            
        # --- REJECT unsupported Instagram URL types immediately ---
        # These are listing/browse pages, not downloadable content.
        # Catching them early prevents noisy multi-fallback log spam.
        if "instagram.com" in url_lower:
            unsupported_ig_paths = [
                "/reels/audio/",  # Audio trending page (not a video)
                "/explore/",      # Explore browse page
                "/hashtag/",      # Hashtag browse page
                "/stories/highlights/",  # Story highlights browser
                "/stories/",      # Individual stories (require login/follow, never downloadable publicly)
            ]
            # Also reject plain profile pages: instagram.com/username/ with no /p/ /reel/ etc
            is_profile_only = not any(x in url_lower for x in ["/p/", "/reel/", "/reels/", "/tv/"])
            is_unsupported_path = any(p in url_lower for p in unsupported_ig_paths)
            
            # Catch profile pages (e.g. instagram.com/cristiano/)
            if is_unsupported_path or is_profile_only:
                if "/stories/" in url_lower:
                    raise HTTPException(
                        status_code=400,
                        detail="Instagram Stories cannot be downloaded. 📖 Please share a link to a Reel or Post instead!"
                    )
                raise HTTPException(
                    status_code=400,
                    detail="This Instagram link is not downloadable. Please share a link to a specific Post or Reel — not an audio page, explore page, or profile."
                )
            

        
        # --- TikTok Share Link Unwrapper ---
        # The TikTok app share sheet emits vm./vt. shorteners; yt-dlp handles the
        # canonical /video/ form far more reliably, so resolve the redirect first.
        if any(short in url_lower for short in ["vm.tiktok.com", "vt.tiktok.com", "tiktok.com/t/"]):
            try:
                print(f"TikTok short link detected. Attempting to unwrap: {url_decoded}")
                r = requests.head(
                    url_decoded,
                    allow_redirects=True,
                    timeout=10,
                    headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"},
                )
                if r.url and "tiktok.com" in r.url.lower():
                    url_decoded = r.url
                    url_lower = url_decoded.lower()
                    print(f"Unwrapped TikTok link to: {url_decoded}")
            except Exception as e:
                print(f"Failed to unwrap TikTok short link: {e}. Proceeding with original URL.")

        # --- REJECT TikTok URLs that are not a single post ---
        # Profile, tag, music and live pages are browse surfaces, not downloads.
        if is_tiktok_url(url_lower) and not any(
            short in url_lower for short in ["vm.tiktok.com", "vt.tiktok.com", "tiktok.com/t/"]
        ):
            is_single_post = any(p in url_lower for p in ["/video/", "/photo/", "/v/", "/embed/"])
            if "/live" in url_lower:
                raise HTTPException(
                    status_code=400,
                    detail="LIVE streams cannot be downloaded. 📺 Please share a link to a posted video instead!"
                )
            if not is_single_post:
                raise HTTPException(
                    status_code=400,
                    detail="This link is not downloadable. Please share a link to a specific video — not a profile, hashtag, or sound page."
                )

        # --- PINTEREST: try yt-dlp, fallback later ---
        if "pinterest.com" in url_lower or "pin.it" in url_lower:
            pass  # Falls through to yt-dlp below
        
        info = None
        is_photo_fallback = False

        is_hybrid_platform = any(domain in url_lower for domain in ["facebook.com", "fb.watch", "fb.gg", "pinterest.com", "pin.it"])
        is_instagram = "instagram.com" in url_lower

        if (
            is_instagram
            and _scrapecreators_configured()
            and _scrapecreators_ig_mode() == "primary"
        ):
            try:
                info = extract_instagram_scrapecreators(url_decoded)
            except HTTPException:
                raise
            except Exception as sc_primary_err:
                print(f"ScrapeCreators primary failed: {sc_primary_err}. Trying yt-dlp...")
        
        # --- Facebook Share Link Unwrapper ---
        # Automatically resolve short share links (e.g. /share/v/) to their true /reel/ or /watch/ URLs
        # before passing them to yt-dlp, bypassing the "Cannot parse data" errors entirely.
        if "facebook.com/share/" in url_lower:
            try:
                print(f"Facebook share link detected. Attempting to unwrap: {url_decoded}")
                r = requests.head(url_decoded, allow_redirects=True, timeout=10)
                url_decoded = r.url
                url_lower = url_decoded.lower()
                print(f"Unwrapped Facebook link to: {url_decoded}")
            except Exception as e:
                print(f"Failed to unwrap Facebook share link: {e}. Proceeding with original URL.")
                
        # --- Facebook Story / Private Link Fast-Fail ---
        if any(fb in url_lower for fb in ["facebook.com", "fb.watch", "fb.gg"]):
            if "login.php" in url_lower or "/stories/" in url_lower:
                raise HTTPException(
                    status_code=400,
                    detail="This Facebook link is a Story or a private post that requires login. 📖 Please share a link to a public Video or Reel instead!"
                )
                
        # 1. Primary extraction with or without cookies
        if info is None:
            try:
                if is_hybrid_platform:
                    try:
                        info = extract_with_ytdlp(url_decoded, use_cookies=False)
                    except Exception as e:
                        print(f"Extraction without cookies failed for {url_decoded}: {e}. Retrying with cookies...")
                        info = extract_with_ytdlp(url_decoded, use_cookies=True)
                else:
                    info = extract_with_ytdlp(url_decoded, use_cookies=True)
            except Exception as primary_error:
                primary_msg = str(primary_error)
                print(f"Primary video extraction failed: {primary_msg}. Checking fallback...")
                
                is_private = any(phrase in primary_msg.lower() for phrase in [
                    "this content isn't available to everyone",
                    "this content is only available for registered users",
                    "login required",
                    "private account",
                    "private video",
                    "requires authentication",
                    "login_via",
                    "/login/"
                ])
                

                if is_private:
                    if _is_downloadable_ig_url(url_decoded):
                        record_ig_extract_failure(url_decoded, primary_msg)
                    raise HTTPException(
                        status_code=403,
                        detail="This content is private or age-restricted."
                    )

                # --- TikTok fast-fail ---
                # Every fallback below is an Instagram scraper; running them on a
                # TikTok URL only burns time (and proxy bandwidth) before failing.
                tiktok_handled = False
                if is_tiktok_url(url_decoded):
                    tiktok_handled = True
                    lowered = primary_msg.lower()
                    # Match on error phrasing only — a bare "404" would also hit
                    # video IDs that happen to contain those digits.
                    if any(term in lowered for term in ["unable to find video in feed", "video not available", "404: not found", "http error 404", "does not exist", "has been removed"]):
                        raise HTTPException(
                            status_code=400,
                            detail="This video is unavailable. It may be deleted, private, or restricted in your region. 🚫"
                        )
                    if any(term in lowered for term in ["ip address is blocked", "geo-restrict", "geo restrict", "not available in your region", "unavailable in your region"]):
                        raise HTTPException(
                            status_code=400,
                            detail="This video is blocked in our server's region. 🌍"
                        )
                    # One retry with a clean cache: TikTok's web challenge is flaky
                    # and usually clears on a second pass. Slideshow posts have no
                    # mp4 at all, so those retry without the video-only filter.
                    wants_photo = any(term in lowered for term in ["no video", "no formats", "playlist", "expecting value", "extra data"])
                    try:
                        clear_ytdlp_cache()
                        if wants_photo:
                            info = extract_media_generic(url_decoded, use_cookies=False)
                        else:
                            info = extract_with_ytdlp(url_decoded, use_cookies=False)
                    except Exception as tiktok_retry_error:
                        print(f"TikTok retry failed: {tiktok_retry_error}")
                        info = None
                    if not info:
                        raise HTTPException(
                            status_code=400,
                            detail="Could not read this video. Please make sure the link is public and try again."
                        )

                
                # Check for Facebook video format that yt-dlp cannot currently parse
                # This happens with old-style Facebook /videos/ posts (not Reels)
                if "cannot parse data" in primary_msg.lower() and any(fb in url_decoded.lower() for fb in ["facebook.com", "fb.watch", "fb.gg"]):
                    raise HTTPException(
                        status_code=400,
                        detail="Sorry, this specific Facebook video format is currently unsupported. Try sharing a Facebook Reel instead."
                    )


                # Fast-fail for Instagram "empty media response" — all fallbacks also fail for this error,
                # so we save 2 proxy hits by stopping here immediately.
                # This happens when a reel is private, deleted, or geo-restricted.
                if "empty media response" in primary_msg.lower() and "instagram.com" in url_decoded.lower():
                    if _is_downloadable_ig_url(url_decoded):
                        record_ig_extract_failure(url_decoded, primary_msg)
                    raise HTTPException(
                        status_code=400,
                        detail="This Instagram post is unavailable. It may be private, deleted, or restricted in your region. 🚫"
                    )

                # Check if this error indicates there is no video in the post (meaning it's a photo or carousel)
                if any(term in primary_msg.lower() for term in ["no video", "no formats", "playlist", "expecting value", "extra data"]):
                    is_photo_fallback = True
                
                # If not explicitly a photo fallback, try video fallback first
                if not is_photo_fallback and not tiktok_handled:
                    try:
                        clear_ytdlp_cache()
                        info = fallback_instagram_scrape(url_decoded, use_cookies=True)
                    except Exception as fallback_error:
                        fallback_msg = str(fallback_error).lower()
                        print(f"Fallback video extraction also failed: {fallback_msg}. Checking Instagram blocks...")
                        
                        # Instagram specific blocks checked ONLY after fallback fails
                        if "instagram.com" in url_decoded.lower() and any(err in fallback_msg for err in ["401: unauthorized", "404: not found", "unreachable", "redirect to login", "this content is unreachable", "empty media response", "400: bad request"]):
                            if _scrapecreators_configured() and _scrapecreators_ig_mode() != "off":
                                print("Instagram blocked yt-dlp. Trying ScrapeCreators...")
                                try:
                                    info = extract_instagram_scrapecreators(url_decoded)
                                except HTTPException:
                                    raise
                                except Exception as sc_err:
                                    print(f"ScrapeCreators fallback failed: {sc_err}")
                                    info = None
                            if not info:
                                print("Trying last-resort curl_cffi embed scraper...")
                                try:
                                    info = extract_instagram_media(url_decoded)
                                    return info
                                except Exception as embed_err:
                                    print(f"Last resort embed scraper failed: {embed_err}")
                                    raise HTTPException(
                                        status_code=400,
                                        detail="Instagram download failed. The post is either private, deleted, or our server cookies have expired."
                                    )
                        
                        print(f"No specific Instagram block found. Trying generic media extraction...")
                        is_photo_fallback = True
                
                # If we determined we need photo/generic extraction
                if is_photo_fallback and not tiktok_handled:
                    if "pinterest.com" in url_decoded.lower() or "pin.it" in url_decoded.lower():
                        # Fail fast for Pinterest instead of wasting 30 seconds on generic extractors
                        error_str = primary_msg.lower()
                        if "pinterestcollection" in error_str or "404" in error_str:
                            raise HTTPException(
                                status_code=400,
                                detail="This link is for a Pinterest Board or Collection. 📌 Please copy the link to a single video Pin instead!"
                            )
                        else:
                            raise HTTPException(
                                status_code=400,
                                detail="This Pinterest link appears to be an image. 📌 You can download photos directly in Pinterest by tapping the three dots and choosing 'Download image'."
                            )
                            
                    try:
                        info = extract_media_generic(url_decoded, use_cookies=True)
                    except Exception as generic_error:
                        print(f"Primary generic extraction failed: {generic_error}. Trying fallback generic...")
                        try:
                            clear_ytdlp_cache()
                            info = fallback_instagram_scrape_generic(url_decoded, use_cookies=True)
                        except Exception as fallback_gen_error:
                            if (
                                "instagram.com" in url_decoded.lower()
                                and _scrapecreators_configured()
                                and _scrapecreators_ig_mode() != "off"
                            ):
                                try:
                                    info = extract_instagram_scrapecreators(url_decoded)
                                except HTTPException:
                                    raise
                                except Exception as sc_err:
                                    raise HTTPException(
                                        status_code=400,
                                        detail=f"Failed to extract photo/carousel: {str(fallback_gen_error)}"
                                    ) from sc_err
                            else:
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"Failed to extract photo/carousel: {str(fallback_gen_error)}"
                                )
        if not info:
            raise Exception("Failed to extract media info from any source")
            
        # Parse media type and urls
        media_type = info.get("_media_type") or "video"
        media_urls = []
        
        if "entries" in info:
            # Playlist / Carousel of items
            entries = info["entries"]
            media_urls = [entry.get("url") for entry in entries if entry.get("url")]
            
            # Check if entries are photos (don't downgrade carousel type)
            if media_type != "carousel":
                first_entry_ext = entries[0].get("ext", "") if entries else ""
                if first_entry_ext in ["jpg", "jpeg", "png", "webp"]:
                    media_type = "photo"
        else:
            # Single item
            url_val = info.get("url")
            if url_val:
                media_urls = [url_val]
            ext = info.get("ext", "")
            if ext in ["jpg", "jpeg", "png", "webp"]:
                media_type = "photo"
                
        formats = []
        if media_type == "photo":
            for url_val in media_urls:
                formats.append({
                    "Extension": "jpg",
                    "Has Audio": False,
                    "Resolution": "High Res",
                    "Direct Download Link": url_val
                })
        else:
            # Video formatting (original format block for backwards compatibility)
            best_url = info.get("url")
            if best_url:
                resolution = info.get("resolution") or info.get("height") or info.get("format_note") or "best"
                if isinstance(resolution, int):
                    resolution = f"{resolution}p"
                    
                formats.append({
                    "Extension": info.get("ext") or "mp4",
                    "Has Audio": True,
                    "Resolution": str(resolution),
                    "Direct Download Link": best_url
                })
            else:
                raw_formats = info.get("formats", [])
                for f in raw_formats:
                    direct_link = f.get("url", "")
                    if not direct_link:
                        continue
                    
                    resolution = f.get("resolution") or f.get("format_note") or f.get("height") or "best"
                    if isinstance(resolution, int):
                        resolution = f"{resolution}p"
                        
                    formats.append({
                        "Extension": f.get("ext") or "mp4",
                        "Has Audio": True,
                        "Resolution": str(resolution),
                        "Direct Download Link": direct_link
                    })
                    
            if not formats and media_urls:
                # Fallback if no format parsed but we have media_urls
                formats.append({
                    "Extension": "mp4",
                    "Has Audio": True,
                    "Resolution": "best",
                    "Direct Download Link": media_urls[0]
                })

        # Build response with both old keys (backwards compatibility) and new keys (media_type, media_urls)
        response_data = {
            "Video Title": info.get("title") or "Social Media Post",
            "Thumbnail URL": info.get("thumbnail") or (info.get("thumbnails", [{}])[0].get("url") if info.get("thumbnails") else ""),
            "Formats": formats,
            "media_type": media_type,
            "media_urls": media_urls,
            "extractor": info.get("_extractor") or "ytdlp",
        }

        # TikTok CDN links are only served to the session that requested them,
        # so the client has to replay these headers or it gets HTTP 403.
        # Prefer a clean 400 over a 200 that always fails on download.
        if is_tiktok_url(url_decoded):
            cookie_header = (info.get("_download_cookie") or "").strip()
            if not cookie_header:
                print(
                    "TikTok extract succeeded but session cookies were empty — "
                    "refusing to return undownloadable CDN URL"
                )
                raise HTTPException(
                    status_code=400,
                    detail="Could not prepare this video for download. Please try again in a moment."
                )
            download_headers = {
                "Referer": "https://www.tiktok.com/",
                "Cookie": cookie_header,
            }
            user_agent = info.get("_download_user_agent")
            if user_agent:
                download_headers["User-Agent"] = user_agent
            response_data["download_headers"] = download_headers

        return response_data
        
    except HTTPException as he:
        # Cache every error response to block proxy-burning retries for 3 minutes
        if not ops_smoke and not from_error_cache:
            try:
                _error_cache_store(
                    _cache_key,
                    he.status_code,
                    he.detail if isinstance(he.detail, str) else str(he.detail),
                )
            except Exception:
                pass
            record_ig_extract_failure(
                url_decoded,
                he.detail if isinstance(he.detail, str) else str(he.detail),
            )
        raise he
    except Exception as e:
        error_msg = str(e)
        
        # Check for specific private / age-restricted substrings
        is_private = any(phrase in error_msg.lower() for phrase in [
            "this content isn't available to everyone",
            "this content is only available for registered users",
            "login required",
            "private account",
            "private video",
            "requires authentication",
            "login_via",
            "/login/"
        ])
        
        if is_private:
            if _is_downloadable_ig_url(url_decoded):
                record_ig_extract_failure(url_decoded, error_msg)
            raise HTTPException(
                status_code=403, 
                detail="This content is private or age-restricted."
            )
            
        server_name = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "LoopHole Backend"
            
        if "empty media response" in error_msg:
            record_ig_extract_failure(url_decoded, error_msg)
            if not ops_smoke:
                alert_msg = (
                    f"🚨 <b>LoopHole Alert</b>\n\n"
                    f"<b>Server:</b> <code>{server_name}</code>\n"
                    f"<b>Error:</b> Instagram Cookies Blocked / Expired 🍪\n"
                    f"<b>Action Required:</b> Please generate fresh cookies and update cookies.txt."
                )
                send_telegram_alert(alert_msg)
            
            detail_msg = "Instagram blocked the request (Empty Media Response). Please update or refresh the 'cookies.txt' file in your Render secrets."
            raise HTTPException(status_code=400, detail=detail_msg)
            
        # Catch Instagram photo/carousel downloads for older clients
        if "instagram.com" in url.lower() and "no video formats found" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail="This Instagram post contains photos/images, not a video. 📸 Currently, only videos are supported for download!"
            )
            
        # Proxy connection failures
        if not ops_smoke and any(phrase in error_msg.lower() for phrase in ["proxyerror", "tunnel connection failed", "unable to connect to proxy", "proxy error"]):
            alert_msg = (
                f"⚠️ <b>LoopHole Alert</b>\n\n"
                f"<b>Server:</b> <code>{server_name}</code>\n"
                f"<b>Error:</b> Proxy Connection Failed\n"
                f"<b>Details:</b> <code>{error_msg}</code>\n"
                f"<b>Action Required:</b> Check Smartproxy dashboard bandwidth or trial limits."
            )
            send_telegram_alert(alert_msg)
        elif not ops_smoke:
            # Other general 500 server crashes
            if _is_downloadable_ig_url(url_decoded):
                record_ig_extract_failure(url_decoded, error_msg)
            alert_msg = (
                f"🔥 <b>LoopHole Alert</b>\n\n"
                f"<b>Server:</b> <code>{server_name}</code>\n"
                f"<b>Error:</b> General Extraction Failure\n"
                f"<b>Target URL:</b> {url}\n"
                f"<b>Details:</b> <code>{error_msg[:300]}...</code>"
            )
            send_telegram_alert(alert_msg)
            
        final_detail = f"Failed to pull video: {error_msg}"
        if not ops_smoke:
            try:
                _error_cache_store(_cache_key, 500, final_detail)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=final_detail)
    finally:
        _OPS_SMOKE_REQUEST.reset(smoke_token)
        gc.collect()


def _self_check() -> None:
    """ponytail: runnable guard for ops-smoke + env parsing; upgrade path = pytest."""
    assert _safe_int_env("__MISSING_INT__", 99) == 99
    assert _safe_int_env("__MISSING_INT__", 99) == 99
    os.environ["__TEST_BAD_INT__"] = "nope"
    assert _safe_int_env("__TEST_BAD_INT__", 7) == 7
    del os.environ["__TEST_BAD_INT__"]

    assert not _is_ig_cookie_death_signal("Instagram Stories cannot be downloaded")
    assert _is_ig_cookie_death_signal("instagram sent an empty media response")

    before = len(_ig_failure_urls)
    token = _OPS_SMOKE_REQUEST.set(True)
    try:
        record_ig_extract_failure(
            "https://www.instagram.com/reel/ABC123/",
            "empty media response",
        )
    finally:
        _OPS_SMOKE_REQUEST.reset(token)
    assert len(_ig_failure_urls) == before

    ERROR_CACHE.clear()
    cap = max(100, _ERROR_CACHE_MAX)
    now = time.time()
    # Expired prune path
    for i in range(10):
        ERROR_CACHE[f"exp-{i}"] = {
            "status": 400,
            "detail": "x",
            "expiry": now - 1,
        }
    ERROR_CACHE["keep"] = {"status": 400, "detail": "x", "expiry": now + 60}
    _prune_error_cache(now)
    assert "keep" in ERROR_CACHE and "exp-0" not in ERROR_CACHE
    ERROR_CACHE.clear()
    # Overflow cap path (all live entries)
    for i in range(cap + 5):
        ERROR_CACHE[f"url-{i}"] = {
            "status": 400,
            "detail": "x",
            "expiry": now + 60 + i,
        }
    _prune_error_cache(now)
    assert len(ERROR_CACHE) == cap
    ERROR_CACHE.clear()


if __name__ == "__main__":
    _self_check()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
