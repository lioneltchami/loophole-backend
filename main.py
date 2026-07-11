from fastapi import FastAPI, Query, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import subprocess
import json
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(auto_update_ytdlp())
    yield

app = FastAPI(title="VidgetGo Backend", version="1.0.0", lifespan=lifespan)

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
    x_api_key = request.headers.get("x-api-key", "")
    if x_api_key != "LOOPHOLE_SECURE_V1_TOKEN":
        raise HTTPException(status_code=403, detail="Unauthorized client signature")
        
    success = clear_ytdlp_cache()
    if success:
        return {"status": "success", "message": "Backend cache cleared successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to clear yt-dlp cache")

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
    """
    x_api_key = request.headers.get("x-api-key", "")
    if x_api_key != "LOOPHOLE_SECURE_V1_TOKEN":
        raise HTTPException(status_code=403, detail="Unauthorized client signature")

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
    
    # 1. Check if 'IG_COOKIES' environment variable is provided, ONLY for Instagram
    if "instagram.com" in url_lower:
        ig_cookies = os.environ.get("IG_COOKIES")
        if ig_cookies:
            try:
                temp_dir = tempfile.gettempdir()
                writable_path = os.path.join(temp_dir, "ytdlp_writable_cookies.txt")
                
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
        writable_path = os.path.join(temp_dir, f"writable_{filename_base}")
        
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
        
    if use_cookies:
        cookies_path = get_writable_cookies_path(url)
        if cookies_path:
            ydl_opts['cookiefile'] = cookies_path
        
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        error_msg = str(e).lower()
        if "proxy" in ydl_opts and ("502" in error_msg or "proxy" in error_msg or "ssl" in error_msg or "eof" in error_msg):
            ydl_opts.pop("proxy", None)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        else:
            raise e

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
        
    if use_cookies:
        cookies_path = get_writable_cookies_path(url)
        if cookies_path:
            ydl_opts['cookiefile'] = cookies_path
        
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        error_msg = str(e).lower()
        if "proxy" in ydl_opts and ("502" in error_msg or "proxy" in error_msg or "ssl" in error_msg or "eof" in error_msg):
            ydl_opts.pop("proxy", None)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        else:
            raise e

def send_telegram_alert(message: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")

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

def scrape_instagram_fallback(url: str) -> dict:
    """
    Lightweight fallback HTML scraper for Instagram.
    Fetches the raw page and extracts the og:image meta tag.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    }
    response = requests.get(url, headers=headers, timeout=10)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch Instagram page: HTTP {response.status_code}")
        
    soup = BeautifulSoup(response.text, 'html.parser')
    meta_tag = soup.find('meta', property='og:image')
    if not meta_tag or not meta_tag.get('content'):
        raise Exception("Could not find og:image meta tag on Instagram page")
        
    img_url = meta_tag['content']
    
    return {
        "title": "Instagram Photo",
        "thumbnail": img_url,
        "url": img_url,
        "ext": "jpg",
    }



def _parse_ig_cookies_to_dict(cookie_env_var: str) -> dict:
    """
    Parse Netscape-format cookie string into a dict for curl_cffi session.
    """
    cookies = {}
    for line in cookie_env_var.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) >= 7:
            name = parts[5]
            value = parts[6]
            cookies[name] = value
    return cookies


def extract_instagram_media(url: str) -> dict:
    """
    Extracts Instagram photos, carousels, and videos using curl_cffi
    to impersonate Chrome's TLS fingerprint.
    
    This bypasses Instagram's bot detection on datacenter IPs, which
    blocks standard requests/instaloader/gallery-dl via graphql/query.
    
    Approach:
    1. Convert post URL to shortcode
    2. Hit Instagram's internal /api/v1/media/shortcode/{shortcode}/info/
       with Chrome TLS fingerprint + x-ig-app-id header + cookies
    3. Parse the JSON response to extract all image/video CDN URLs
    """
    from curl_cffi import requests as cffi_requests
    import re
    
    # 1. Extract shortcode from URL
    match = re.search(r'/p/([A-Za-z0-9_-]+)', url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid Instagram post URL.")
    shortcode = match.group(1)
    
    # 2. Build headers that mimic Instagram web app
    ig_app_id = os.environ.get("IG_APP_ID", "936619743392459")
    headers = {
        "x-ig-app-id": ig_app_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    
    # 3. Load cookies from environment variables
    cookies = {}
    ig_cookies_photo = os.environ.get("IG_COOKIES_PHOTO_1") or os.environ.get("IG_COOKIES_PHOTO_2") or os.environ.get("IG_COOKIES")
    if ig_cookies_photo:
        cookies = _parse_ig_cookies_to_dict(ig_cookies_photo)
    
    # 4. Set up proxy
    proxy_url = os.environ.get("PROXY_URL")
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    
    # 5. Hit Instagram's internal media info endpoint with Chrome TLS fingerprint
    api_url = f"https://i.instagram.com/api/v1/media/shortcode/{shortcode}/info/"
    
    try:
        resp = cffi_requests.get(
            api_url,
            headers=headers,
            cookies=cookies,
            proxies=proxies,
            impersonate="chrome131",
            timeout=15,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Network request failed: {str(e)}")
    
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Post not found or deleted.")
    if resp.status_code == 401 or resp.status_code == 403:
        raise HTTPException(status_code=403, detail="Instagram is blocking this request. Please update your cookies (IG_COOKIES_PHOTO_1).")
    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Instagram returned HTTP {resp.status_code}.")
    
    # 6. Parse the JSON response
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=500, detail="Instagram returned an invalid response.")
    
    items = data.get("items") or []
    if not items:
        raise HTTPException(status_code=400, detail="No media found in this post.")
    
    item = items[0]
    media_urls = []
    owner = item.get("user", {}).get("username", "unknown")
    thumbnail = ""
    
    media_type = item.get("media_type")  # 1=photo, 2=video, 8=carousel
    
    def best_image_url(candidates: list) -> str:
        """Pick the highest-resolution image from candidates list."""
        if not candidates:
            return ""
        best = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
        return best.get("url", "")
    
    if media_type == 8:  # Carousel
        for node in item.get("carousel_media") or []:
            node_type = node.get("media_type")
            if node_type == 2:  # video
                video_versions = node.get("video_versions") or []
                if video_versions:
                    media_urls.append(video_versions[0]["url"])
            else:  # photo
                candidates = node.get("image_versions2", {}).get("candidates") or []
                img_url = best_image_url(candidates)
                if img_url:
                    media_urls.append(img_url)
    elif media_type == 2:  # Single video
        video_versions = item.get("video_versions") or []
        if video_versions:
            media_urls.append(video_versions[0]["url"])
    else:  # Single photo
        candidates = item.get("image_versions2", {}).get("candidates") or []
        img_url = best_image_url(candidates)
        if img_url:
            media_urls.append(img_url)
    
    # Thumbnail
    candidates = item.get("image_versions2", {}).get("candidates") or []
    thumbnail = best_image_url(candidates) if candidates else (media_urls[0] if media_urls else "")
    
    if not media_urls:
        raise HTTPException(status_code=400, detail="No downloadable media found in this post.")
    
    return {
        "media_type": "carousel",
        "media_urls": media_urls,
        "Video Title": f"Post by @{owner}",
        "Thumbnail URL": thumbnail,
    }


@app.get("/extract")
def extract_video(
    url: str = Query(..., description="The video/photo URL to extract metadata from"),
    x_api_key: str = Header(None, description="Secure API key for client authentication")
):
    if x_api_key != "LOOPHOLE_SECURE_V1_TOKEN":
        raise HTTPException(status_code=403, detail="Unauthorized client signature")
        
    if not url:
        raise HTTPException(status_code=400, detail="URL query parameter is required")
        
    try:
        url_decoded = urllib.parse.unquote(url)
        url_lower = url_decoded.lower()

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
            
        # --- INSTAGRAM /p/ : curl_cffi Chrome-impersonation extractor ---
        if "/p/" in url_lower and "instagram.com" in url_lower:
            return extract_instagram_media(url_decoded)
        
        # --- PINTEREST: try yt-dlp, fallback later ---
        if "pinterest.com" in url_lower or "pin.it" in url_lower:
            pass  # Falls through to yt-dlp below
        
        info = None
        is_photo_fallback = False

        is_hybrid_platform = any(domain in url_lower for domain in ["facebook.com", "fb.watch", "fb.gg", "pinterest.com", "pin.it"])

        # 1. Primary extraction with or without cookies
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
                
            # Check for specific private / age-restricted substrings immediately
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
                raise HTTPException(
                    status_code=403,
                    detail="This content is private or age-restricted."
                )
            
            # Check if this error indicates there is no video in the post (meaning it's a photo or carousel)
            if any(term in primary_msg.lower() for term in ["no video", "no formats", "playlist", "empty media response", "expecting value", "extra data"]):
                is_photo_fallback = True
            
            # If not explicitly a photo fallback, try video fallback first
            if not is_photo_fallback:
                try:
                    clear_ytdlp_cache()
                    info = fallback_instagram_scrape(url_decoded, use_cookies=True)
                except Exception as fallback_error:
                    print(f"Fallback video extraction also failed: {fallback_error}. Trying generic media extraction...")
                    is_photo_fallback = True
            
            # If we determined we need photo/generic extraction
            if is_photo_fallback:
                try:
                    info = extract_media_generic(url_decoded, use_cookies=True)
                except Exception as generic_error:
                    print(f"Primary generic extraction failed: {generic_error}. Trying fallback generic...")
                    try:
                        clear_ytdlp_cache()
                        info = fallback_instagram_scrape_generic(url_decoded, use_cookies=True)
                    except Exception as fallback_gen_error:
                        if "instagram.com" in url_decoded:
                            try:
                                print("yt-dlp photo fallback failed completely. Trying custom HTML scraper fallback...")
                                info = scrape_instagram_fallback(url_decoded)
                            except Exception as scraper_error:
                                print(f"Custom Instagram scraper fallback failed: {scraper_error}")
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"Failed to extract Instagram photo/carousel: {str(scraper_error)}"
                                )
                        else:
                            error_str = str(fallback_gen_error).lower()
                            is_pinterest = "pinterest.com" in url_decoded.lower() or "pin.it" in url_decoded.lower()
                            
                            if is_pinterest:
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
                            raise HTTPException(
                                status_code=400,
                                detail=f"Failed to extract photo/carousel: {str(fallback_gen_error)}"
                            )
        if not info:
            raise Exception("Failed to extract media info from any source")
            
        # Parse media type and urls
        media_type = "video"
        media_urls = []
        
        if "entries" in info:
            # Playlist / Carousel of items
            entries = info["entries"]
            media_urls = [entry.get("url") for entry in entries if entry.get("url")]
            
            # Check if entries are photos
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
            "media_urls": media_urls
        }
        
        return response_data
        
    except HTTPException as he:
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
            raise HTTPException(
                status_code=403, 
                detail="This content is private or age-restricted."
            )
            
        server_name = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "LoopHole Backend"
            
        if "empty media response" in error_msg:
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
        if any(phrase in error_msg.lower() for phrase in ["proxyerror", "tunnel connection failed", "unable to connect to proxy", "proxy error"]):
            alert_msg = (
                f"⚠️ <b>LoopHole Alert</b>\n\n"
                f"<b>Server:</b> <code>{server_name}</code>\n"
                f"<b>Error:</b> Proxy Connection Failed\n"
                f"<b>Details:</b> <code>{error_msg}</code>\n"
                f"<b>Action Required:</b> Check Smartproxy dashboard bandwidth or trial limits."
            )
            send_telegram_alert(alert_msg)
        else:
            # Other general 500 server crashes
            alert_msg = (
                f"🔥 <b>LoopHole Alert</b>\n\n"
                f"<b>Server:</b> <code>{server_name}</code>\n"
                f"<b>Error:</b> General Extraction Failure\n"
                f"<b>Target URL:</b> {url}\n"
                f"<b>Details:</b> <code>{error_msg[:300]}...</code>"
            )
            send_telegram_alert(alert_msg)
            
        raise HTTPException(status_code=500, detail=f"Failed to pull video: {error_msg}")
    finally:
        gc.collect()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
