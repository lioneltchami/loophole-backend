from fastapi import FastAPI, Query, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
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

app = FastAPI(title="VidgetGo Backend", version="1.0.0")

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
def clear_cache_endpoint():
    success = clear_ytdlp_cache()
    if success:
        return {"status": "success", "message": "Backend cache cleared successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to clear yt-dlp cache")

def get_cookies_path() -> str:
    """
    Locates the cookies.txt file by checking:
    1. Environment variable 'COOKIES_FILE'
    2. Render's default secret file path '/etc/secrets/cookies.txt'
    3. Local directory './cookies.txt'
    Returns the path if it exists, otherwise None.
    """
    # 1. Environment Variable
    env_path = os.environ.get("COOKIES_FILE")
    if env_path and os.path.exists(env_path):
        return env_path
        
    # 2. Render Secret File default mount
    render_secret_path = "/etc/secrets/cookies.txt"
    if os.path.exists(render_secret_path):
        return render_secret_path
        
    # 3. Local Workspace
    local_path = "cookies.txt"
    if os.path.exists(local_path):
        return local_path
        
    return None

def get_writable_cookies_path() -> str:
    """
    Creates a writable copy of the cookies.txt file inside a temporary directory.
    This prevents OSError: [Errno 30] Read-only file system on platforms like Render.
    Also handles the 'IG_COOKIES' environment variable and formats it as a valid
    Netscape HTTP Cookie File if present.
    """
    # 1. Check if 'IG_COOKIES' environment variable is provided
    ig_cookies = os.environ.get("IG_COOKIES")
    if ig_cookies:
        try:
            temp_dir = tempfile.gettempdir()
            writable_path = os.path.join(temp_dir, "ytdlp_writable_cookies.txt")
            
            # Format and prepare cookie file content
            cookie_content = ig_cookies.strip()
            
            # Ensure the cookie content starts with the Netscape header line
            if not cookie_content.startswith("# Netscape HTTP Cookie File"):
                cookie_content = "# Netscape HTTP Cookie File\n" + cookie_content
                
            with open(writable_path, "w", encoding="utf-8") as f:
                f.write(cookie_content)
                
            os.chmod(writable_path, 0o666)
            return writable_path
        except Exception as e:
            print(f"Error writing IG_COOKIES to writable temp path: {e}")

    # 2. Fallback to reading cookies.txt from file paths
    source_path = get_cookies_path()
    if not source_path:
        return None
        
    try:
        temp_dir = tempfile.gettempdir()
        writable_path = os.path.join(temp_dir, "ytdlp_writable_cookies.txt")
        
        # Copy to the writable temp directory
        shutil.copy2(source_path, writable_path)
        
        # Ensure it has read/write permissions
        os.chmod(writable_path, 0o666)
        
        return writable_path
    except Exception as e:
        print(f"Error copying cookies to writable path: {e}")
        # Fallback to source path and hope for the best
        return source_path

def extract_with_ytdlp(url: str, user_agent: str = None) -> dict:
    """
    Runs yt-dlp with custom mobile User-Agent spoofing and optional cookie auth to bypass Meta blocks.
    Forces extraction of pre-merged best mobile-friendly MP4 formats to eliminate ffmpeg requirement.
    Uses programmatic yt-dlp to enforce strict 10s socket timeout.
    """
    if not user_agent:
        user_agent = "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
    
    ydl_opts = {
        'socket_timeout': 10,
        'format': 'best[ext=mp4]/best',
        'noplaylist': True,
        'user_agent': user_agent,
        'quiet': True,
        'no_warnings': True,
    }
    
    headers = {}
    if "instagram.com" in url or "threads.net" in url:
        headers['Referer'] = 'https://www.instagram.com/'
    elif "tiktok.com" in url:
        headers['Referer'] = 'https://www.tiktok.com/'
    elif "x.com" in url or "twitter.com" in url:
        headers['Referer'] = 'https://x.com/'
        
    if headers:
        ydl_opts['http_headers'] = headers
        
    cookies_path = get_writable_cookies_path()
    if cookies_path:
        ydl_opts['cookiefile'] = cookies_path
        
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def extract_media_generic(url: str, user_agent: str = None) -> dict:
    """
    Runs yt-dlp without the strict video format filter and allows playlists/carousels.
    Used for extracting photos, carousels, or fallback media.
    Uses programmatic yt-dlp to enforce strict 10s socket timeout.
    """
    if not user_agent:
        user_agent = "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
        
    ydl_opts = {
        'socket_timeout': 10,
        'user_agent': user_agent,
        'quiet': True,
        'no_warnings': True,
    }
    
    headers = {}
    if "instagram.com" in url or "threads.net" in url:
        headers['Referer'] = 'https://www.instagram.com/'
    elif "tiktok.com" in url:
        headers['Referer'] = 'https://www.tiktok.com/'
    elif "x.com" in url or "twitter.com" in url:
        headers['Referer'] = 'https://x.com/'
        
    if headers:
        ydl_opts['http_headers'] = headers
        
    cookies_path = get_writable_cookies_path()
    if cookies_path:
        ydl_opts['cookiefile'] = cookies_path
        
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def fallback_instagram_scrape(url: str) -> dict:
    """
    Fallback extractor using browser impersonation with an iPhone Safari User-Agent
    if standard Chrome-spoofed extraction is throttled or blocked.
    """
    try:
        # Alternate high-quality iPhone Safari mobile UA
        iphone_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        return extract_with_ytdlp(url, user_agent=iphone_ua)
    except Exception as e:
        raise Exception(f"Fallback mobile scraper also failed: {str(e)}")

def fallback_instagram_scrape_generic(url: str) -> dict:
    """
    Fallback generic extractor using browser impersonation with an iPhone Safari User-Agent.
    """
    try:
        iphone_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        return extract_media_generic(url, user_agent=iphone_ua)
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
        
        info = None
        is_photo_fallback = False

        if info is None:
            # 1. Attempt primary video-focused extraction using modern Android Chrome mobile spoof
            try:
                info = extract_with_ytdlp(url_decoded)
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
                        info = fallback_instagram_scrape(url_decoded)
                    except Exception as fallback_error:
                        print(f"Fallback video extraction also failed: {fallback_error}. Trying generic media extraction...")
                        is_photo_fallback = True
                
                # If we determined we need photo/generic extraction
                if is_photo_fallback:
                    try:
                        info = extract_media_generic(url_decoded)
                    except Exception as generic_error:
                        print(f"Primary generic extraction failed: {generic_error}. Trying fallback generic...")
                        try:
                            clear_ytdlp_cache()
                            info = fallback_instagram_scrape_generic(url_decoded)
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
            
        if "empty media response" in error_msg:
            detail_msg = "Instagram blocked the request (Empty Media Response). Please update or refresh the 'cookies.txt' file in your Render secrets."
            raise HTTPException(status_code=400, detail=detail_msg)
            
        raise HTTPException(status_code=500, detail=f"Failed to pull video: {error_msg}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
