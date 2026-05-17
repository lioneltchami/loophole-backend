from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import json
import urllib.parse
import os

app = FastAPI(title="VidgetGo Backend", version="1.0.0")

# Enable CORS for the Flutter client
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def clear_ytdlp_cache():
    """
    Clears the yt-dlp cache to reset cookie and throttle states.
    """
    try:
        subprocess.run(["yt-dlp", "--rm-cache-dir"], check=True, capture_output=True)
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

def extract_with_ytdlp(url: str, user_agent: str = None) -> dict:
    """
    Runs yt-dlp with custom mobile User-Agent spoofing and optional cookie auth to bypass Meta blocks.
    """
    if not user_agent:
        # High-end Android Chrome User-Agent mimicking a modern mobile device browser
        user_agent = "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
    
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        "--user-agent", user_agent,
        "--referer", "https://www.instagram.com/",
    ]
    
    cookies_path = get_cookies_path()
    if cookies_path:
        cmd.extend(["--cookies", cookies_path])
        print(f"Using cookies.txt from: {cookies_path}")
    else:
        print("Warning: No cookies.txt found. Attempting guest session extraction.")
        
    cmd.append(url)
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(result.stderr or "yt-dlp extraction failed")
        
    return json.loads(result.stdout)


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

@app.get("/extract")
def extract_video(url: str = Query(..., description="The video URL to extract metadata from")):
    if not url:
        raise HTTPException(status_code=400, detail="URL query parameter is required")
        
    try:
        url_decoded = urllib.parse.unquote(url)
        
        # 1. Attempt primary extraction using a modern Android Chrome mobile spoof
        try:
            info = extract_with_ytdlp(url_decoded)
        except Exception as primary_error:
            print(f"Primary Chrome spoof extraction blocked: {primary_error}. Triggering fallback iPhone Safari spoof...")
            
            # Clear yt-dlp cache to remove any throttled cookies/session state
            clear_ytdlp_cache()
            
            # 2. Fallback to iPhone Safari browser impersonation
            info = fallback_instagram_scrape(url_decoded)

        # Parse formats list to match downloader_service.dart expectations
        formats = []
        raw_formats = info.get("formats", [])
        
        if not raw_formats and info.get("url"):
            raw_formats = [{
                "url": info.get("url"),
                "ext": info.get("ext", "mp4"),
                "acodec": info.get("acodec", "mp4a"),
                "vcodec": info.get("vcodec", "avc1"),
                "resolution": info.get("resolution", "best"),
            }]

        for f in raw_formats:
            direct_link = f.get("url", "")
            if not direct_link:
                continue
                
            has_audio = f.get("acodec") != "none" and f.get("acodec") is not None
            has_video = f.get("vcodec") != "none"
            
            if direct_link:
                resolution = f.get("resolution") or f.get("format_note") or f.get("height") or "best"
                if isinstance(resolution, int):
                    resolution = f"{resolution}p"
                
                formats.append({
                    "Extension": f.get("ext") or "mp4",
                    "Has Audio": has_audio,
                    "Resolution": str(resolution),
                    "Direct Download Link": direct_link
                })
                
        # Build the exact contract required by parseBackendResponse in mobile client
        response_data = {
            "Video Title": info.get("title") or "Social Video",
            "Thumbnail URL": info.get("thumbnail") or (info.get("thumbnails", [{}])[0].get("url") if info.get("thumbnails") else ""),
            "Formats": formats
        }
        
        return response_data
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to pull video: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
