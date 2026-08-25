#!/usr/bin/env python3
"""
LoopHole Instagram cookie refresh bot.

1) Keep-alive: warm an existing IG session via curl_cffi + proxy, harvest cookies.
2) Relogin (optional): if session is dead and IG_USERNAME/IG_PASSWORD are set, try Playwright.
3) Push: hot-reload live backend, then persist IG_COOKIES on the web service via Render API.

Intended to run as a Render Cron Job every 12h.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from http.cookiejar import Cookie, MozillaCookieJar
from typing import Optional
from urllib.parse import urlparse

import requests

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None


def make_cookie(
    name: str,
    value: str,
    domain: str,
    path: str = "/",
    secure: bool = True,
    expires: Optional[int] = None,
) -> Cookie:
    domain = domain or ".instagram.com"
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path=path or "/",
        path_specified=True,
        secure=secure,
        expires=expires,
        discard=False,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None},
        rfc2109=False,
    )


API_KEY = os.environ.get("LOOPHOLE_API_KEY", "LOOPHOLE_SECURE_V1_TOKEN")
BACKEND_URL = os.environ.get("BACKEND_URL", "https://loophole-backend-1xo4.onrender.com").rstrip("/")
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
IG_COOKIES = os.environ.get("IG_COOKIES", "").strip()
IG_USERNAME = os.environ.get("IG_USERNAME", "").strip()
IG_PASSWORD = os.environ.get("IG_PASSWORD", "").strip()
IG_USER_AGENT = os.environ.get(
    "IG_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "").strip()
RENDER_WEB_SERVICE_ID = os.environ.get(
    "RENDER_WEB_SERVICE_ID", "srv-d9hjl8715fvs73eo0meg"
).strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("COOKIE_BOT_DRY_RUN", "").lower() in ("1", "true", "yes")


def log(msg: str) -> None:
    print(f"[ig-cookie-bot] {msg}", flush=True)


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram not configured; skipping alert")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception as e:
        log(f"Telegram send failed: {e}")


def ensure_netscape_header(content: str) -> str:
    content = (content or "").strip()
    if not content:
        return ""
    if not content.startswith("# Netscape HTTP Cookie File"):
        content = "# Netscape HTTP Cookie File\n" + content
    return content


def write_temp_netscape(content: str, path: str) -> str:
    content = ensure_netscape_header(content)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
        if not content.endswith("\n"):
            f.write("\n")
    return path


def jar_to_netscape(jar: MozillaCookieJar) -> str:
    lines = ["# Netscape HTTP Cookie File", "# LoopHole IG cookie bot export", ""]
    for c in jar:
        domain = c.domain or ""
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.path or "/"
        secure = "TRUE" if c.secure else "FALSE"
        expires = str(int(c.expires)) if c.expires else "0"
        lines.append(
            "\t".join([domain, include_sub, path, secure, expires, c.name, c.value])
        )
    return "\n".join(lines) + "\n"


def parse_proxy(proxy_url: str) -> Optional[dict]:
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return None
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if parsed.port:
        server = f"{server}:{parsed.port}"
    out = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


def session_looks_logged_in(netscape: str) -> bool:
    for line in netscape.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, value = parts[5], parts[6]
        if name == "sessionid" and value and value not in ("0", "deleted"):
            return True
    return False


def keep_alive_with_curl(seed_cookies: str) -> tuple[str, bool, str]:
    """
    Warm Instagram with existing cookies. Returns (netscape, logged_in, detail).
    """
    if cffi_requests is None:
        return seed_cookies, session_looks_logged_in(seed_cookies), "curl_cffi missing"

    cookie_path = "/tmp/ig_bot_seed_cookies.txt"
    write_temp_netscape(seed_cookies, cookie_path)

    jar = MozillaCookieJar(cookie_path)
    jar.load(ignore_discard=True, ignore_expires=True)

    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    session = cffi_requests.Session(impersonate="chrome124")

    # Load jar into session
    for c in jar:
        session.cookies.set(
            c.name,
            c.value,
            domain=c.domain,
            path=c.path or "/",
        )

    headers = {
        "User-Agent": IG_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    urls = [
        "https://www.instagram.com/",
        "https://www.instagram.com/accounts/edit/",
    ]
    last_status = None
    for url in urls:
        try:
            resp = session.get(url, headers=headers, proxies=proxies, timeout=45, allow_redirects=True)
            last_status = resp.status_code
            log(f"Keep-alive GET {url} -> {resp.status_code}")
            time.sleep(1.5)
        except Exception as e:
            log(f"Keep-alive request failed for {url}: {e}")

    # Merge session cookies back into Mozilla jar
    try:
        for c in session.cookies:
            domain = getattr(c, "domain", None) or ".instagram.com"
            path = getattr(c, "path", None) or "/"
            secure = bool(getattr(c, "secure", True))
            expires = getattr(c, "expires", None)
            jar.set_cookie(
                make_cookie(
                    name=c.name,
                    value=c.value,
                    domain=domain,
                    path=path,
                    secure=secure,
                    expires=int(expires) if expires else None,
                )
            )
    except Exception as e:
        log(f"Cookie merge warning: {e}")

    netscape = jar_to_netscape(jar)
    logged_in = session_looks_logged_in(netscape)
    detail = f"keep-alive status={last_status} logged_in={logged_in}"
    return ensure_netscape_header(netscape), logged_in, detail


def relogin_with_playwright() -> tuple[Optional[str], str]:
    """Best-effort IG login. Returns (netscape_or_none, detail)."""
    if not IG_USERNAME or not IG_PASSWORD:
        return None, "IG_USERNAME/IG_PASSWORD not set"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright not installed"

    proxy = parse_proxy(PROXY_URL)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=IG_USER_AGENT,
                viewport={"width": 1280, "height": 720},
                proxy=proxy,
            )
            page = context.new_page()
            page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)

            # Cookie / consent banners — best effort dismiss
            for label in ("Allow all cookies", "Accept", "Allow essential and optional cookies"):
                try:
                    page.get_by_role("button", name=label).click(timeout=2000)
                    time.sleep(1)
                except Exception:
                    pass

            user_sel = 'input[name="username"]'
            pass_sel = 'input[name="password"]'
            page.wait_for_selector(user_sel, timeout=30000)
            page.fill(user_sel, IG_USERNAME)
            page.fill(pass_sel, IG_PASSWORD)
            page.get_by_role("button", name="Log in").click()
            time.sleep(8)

            # Dismiss post-login prompts
            for text in ("Not Now", "Not now", "Save info"):
                try:
                    page.get_by_role("button", name=text).click(timeout=3000)
                    time.sleep(1)
                except Exception:
                    pass

            page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)

            cookies = context.cookies()
            browser.close()

        # Build Netscape from Playwright cookies
        lines = ["# Netscape HTTP Cookie File", "# LoopHole Playwright login export", ""]
        has_session = False
        for c in cookies:
            domain = c.get("domain") or ""
            if "instagram" not in domain:
                continue
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path") or "/"
            secure = "TRUE" if c.get("secure") else "FALSE"
            expires = str(int(c.get("expires") or 0))
            name = c.get("name") or ""
            value = c.get("value") or ""
            if name == "sessionid" and value:
                has_session = True
            lines.append(
                "\t".join([domain, include_sub, path, secure, expires, name, value])
            )

        if not has_session:
            return None, "Playwright login finished but sessionid missing (checkpoint/2FA/captcha?)"

        return "\n".join(lines) + "\n", "playwright login ok"
    except Exception as e:
        return None, f"playwright login failed: {e}"


def hot_reload_backend(netscape: str) -> bool:
    if DRY_RUN:
        log("DRY_RUN: skip hot-reload")
        return True
    try:
        resp = requests.post(
            f"{BACKEND_URL}/admin/ig-cookies",
            headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
            json={"cookies": netscape},
            timeout=30,
        )
        log(f"Hot-reload -> {resp.status_code}: {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        log(f"Hot-reload failed: {e}")
        return False


def persist_to_render(netscape: str) -> bool:
    if DRY_RUN:
        log("DRY_RUN: skip Render env update")
        return True
    if not RENDER_API_KEY:
        log("RENDER_API_KEY missing; skip persist")
        return False
    try:
        resp = requests.put(
            f"https://api.render.com/v1/services/{RENDER_WEB_SERVICE_ID}/env-vars/IG_COOKIES",
            headers={
                "Authorization": f"Bearer {RENDER_API_KEY}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"value": netscape},
            timeout=60,
        )
        log(f"Render persist IG_COOKIES -> {resp.status_code}")
        if resp.status_code >= 400:
            log(resp.text[:300])
        return resp.status_code < 400
    except Exception as e:
        log(f"Render persist failed: {e}")
        return False


def main() -> int:
    log("Starting Instagram cookie refresh")
    if not PROXY_URL:
        log("WARNING: PROXY_URL empty — IG may block datacenter IP")

    seed = ensure_netscape_header(IG_COOKIES)
    netscape = seed
    logged_in = session_looks_logged_in(seed)
    source = "seed"

    if seed:
        try:
            netscape, logged_in, detail = keep_alive_with_curl(seed)
            source = "keep-alive"
            log(detail)
        except Exception as e:
            log(f"Keep-alive crashed: {e}")
            traceback.print_exc()
            logged_in = session_looks_logged_in(seed)
            netscape = seed
    else:
        log("No seed IG_COOKIES in env")

    if not logged_in:
        log("Session dead — attempting Playwright relogin")
        fresh, detail = relogin_with_playwright()
        log(detail)
        if fresh and session_looks_logged_in(fresh):
            netscape = fresh
            logged_in = True
            source = "playwright-login"
        else:
            send_telegram(
                "🚨 <b>LoopHole Cookie Bot</b>\n\n"
                "Instagram session is dead and auto-login failed.\n"
                f"<b>Detail:</b> <code>{detail[:240]}</code>\n\n"
                "Action: export fresh Netscape cookies from a dummy IG account "
                "and update <code>IG_COOKIES</code> on the web service + cron."
            )
            return 2

    hot_ok = hot_reload_backend(netscape)
    persist_ok = persist_to_render(netscape)

    send_telegram(
        "🍪 <b>LoopHole Cookie Bot</b>\n\n"
        f"<b>Source:</b> {source}\n"
        f"<b>Hot-reload:</b> {'ok' if hot_ok else 'fail'}\n"
        f"<b>Render persist:</b> {'ok' if persist_ok else 'fail/skip'}\n"
        f"<b>sessionid:</b> present"
    )

    if not hot_ok and not persist_ok:
        return 1
    log("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
