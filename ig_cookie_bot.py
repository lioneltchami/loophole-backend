#!/usr/bin/env python3
"""
LoopHole Instagram cookie refresh bot (hardened).

1) Seed from live backend (preferred) or Render env, then Actions/env seed as fallback.
2) Keep-alive via curl_cffi + proxy; prove liveness (not just sessionid presence).
3) Optional Playwright relogin if dead and credentials set.
4) Hot-reload live backend (required for success); persist IG_COOKIES on Render (best-effort).
"""

from __future__ import annotations

import html
import json
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


ADMIN_KEY = os.environ.get("COOKIE_BOT_ADMIN_KEY", "").strip()
BACKEND_URL = (os.environ.get("BACKEND_URL") or "https://loophole-backend-1xo4.onrender.com").rstrip("/")
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
IG_COOKIES = os.environ.get("IG_COOKIES", "").strip()
IG_USERNAME = os.environ.get("IG_USERNAME", "").strip()
IG_PASSWORD = os.environ.get("IG_PASSWORD", "").strip()
IG_ACCOUNTS_JSON = os.environ.get("IG_ACCOUNTS_JSON", "").strip()
IG_USER_AGENT = os.environ.get("IG_USER_AGENT") or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
IG_WEB_APP_ID = "936619743392459"
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "").strip()
RENDER_WEB_SERVICE_ID = (
    os.environ.get("RENDER_WEB_SERVICE_ID") or "srv-d9hjl8715fvs73eo0meg"
).strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
DRY_RUN = os.environ.get("COOKIE_BOT_DRY_RUN", "").lower() in ("1", "true", "yes")
SMOKE_EXTRACT_URL = os.environ.get("COOKIE_BOT_SMOKE_URL", "").strip()
CLIENT_API_KEY = (os.environ.get("LOOPHOLE_API_KEY") or "LOOPHOLE_SECURE_V1_TOKEN").strip()


def log(msg: str) -> None:
    print(f"[ig-cookie-bot] {msg}", flush=True)



def _clean_cred(value: str) -> str:
    """Strip Excel/CSV quoting artifacts from username/password cells."""
    s = (value or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    return s.lstrip("'\"").strip()

def load_ig_accounts():
    """
    Load dummy IG accounts for Playwright relogin rotation.
    Prefers IG_ACCOUNTS_JSON=[{"username":"...","password":"..."}, ...]
    Falls back to single IG_USERNAME / IG_PASSWORD.
    """
    accounts = []
    if IG_ACCOUNTS_JSON:
        try:
            raw = json.loads(IG_ACCOUNTS_JSON)
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    user = _clean_cred(item.get("username") or item.get("user") or "")
                    password = _clean_cred(item.get("password") or item.get("pass") or "")
                    if user and password:
                        accounts.append({"username": user, "password": password})
        except json.JSONDecodeError as e:
            log(f"IG_ACCOUNTS_JSON parse error: {e}")
    if not accounts and IG_USERNAME and IG_PASSWORD:
        accounts.append({"username": _clean_cred(IG_USERNAME), "password": _clean_cred(IG_PASSWORD)})
    return accounts


def send_slack(message: str) -> None:
    """Post a plain-text ops alert to Slack Incoming Webhook."""
    if not SLACK_WEBHOOK_URL:
        log("SLACK_WEBHOOK_URL not configured; skipping alert")
        return
    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": message},
            timeout=10,
        )
        if resp.status_code >= 300:
            log(f"Slack alert HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log(f"Slack send failed: {e}")


def send_alert(message: str) -> None:
    """Ops notifier — Slack preferred (Telegram kept as optional legacy)."""
    send_slack(message)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
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


def extract_sessionid(netscape: str) -> Optional[str]:
    """Return sessionid cookie value from Netscape text, or None."""
    for raw in (netscape or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
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
            return None
        return value
    return None


def session_has_sessionid(netscape: str) -> bool:
    return extract_sessionid(netscape) is not None


def load_jar_from_netscape(seed_cookies: str) -> MozillaCookieJar:
    cookie_path = "/tmp/ig_bot_seed_cookies.txt"
    write_temp_netscape(seed_cookies, cookie_path)
    jar = MozillaCookieJar(cookie_path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def build_cffi_session(seed_cookies: str):
    if cffi_requests is None:
        raise RuntimeError("curl_cffi missing")
    jar = load_jar_from_netscape(seed_cookies)
    session = cffi_requests.Session(impersonate="chrome124")
    for c in jar:
        session.cookies.set(
            c.name,
            c.value,
            domain=c.domain,
            path=c.path or "/",
        )
    return session, jar


def probe_session_alive(netscape: str) -> tuple[bool, str]:
    """
    Prove the session is actually authenticated against Instagram.
    sessionid presence alone is not enough.
    """
    if not session_has_sessionid(netscape):
        return False, "no sessionid cookie"

    if cffi_requests is None:
        return False, "curl_cffi missing for liveness probe"

    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    headers = {
        "User-Agent": IG_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": IG_WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/accounts/edit/",
    }

    try:
        session, _jar = build_cffi_session(netscape)
        api_resp = session.get(
            "https://www.instagram.com/api/v1/accounts/edit/web_form_data/",
            headers=headers,
            proxies=proxies,
            timeout=45,
        )
        log(
            f"Liveness probe web_form_data -> {api_resp.status_code} "
            f"len={len(api_resp.text or '')}"
        )
        if api_resp.status_code in (401, 403):
            return False, f"web_form_data HTTP {api_resp.status_code}"
        if api_resp.status_code == 200:
            try:
                payload = api_resp.json()
            except Exception:
                return False, "web_form_data not JSON"
            if str(payload.get("status", "")).lower() == "fail":
                return False, "web_form_data status fail"
            username = (payload.get("form_data") or {}).get("username") or payload.get(
                "username"
            )
            if username:
                return True, f"web_form_data ok user={str(username)[:24]}"
            return False, "web_form_data missing username"

        # Fallback: HTML edit page must stay on /accounts/edit/ (not home or login)
        html_headers = {
            "User-Agent": IG_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = session.get(
            "https://www.instagram.com/accounts/edit/",
            headers=html_headers,
            proxies=proxies,
            timeout=45,
            allow_redirects=True,
        )
        final_url = str(getattr(resp, "url", "") or "")
        body = (resp.text or "")[:8000].lower()
        status = resp.status_code
        log(f"Liveness probe accounts/edit -> {status} url={final_url[:80]}")

        if status in (401, 403):
            return False, f"probe HTTP {status}"
        if "/accounts/login" in final_url:
            return False, "redirected to login"
        if "/accounts/edit" not in final_url:
            return False, f"unexpected final url={final_url[:80]}"
        if "loginform" in body or '"login_page"' in body:
            return False, "login page HTML"
        if "create an account" in body:
            return False, "signup page HTML"
        if status == 200:
            return True, f"accounts/edit ok status={status}"
        return False, f"probe inconclusive status={status}"
    except Exception as e:
        return False, f"probe error: {e}"


def smoke_extract_with_backend(netscape: str) -> tuple[bool, str]:
    """Optional: push cookies temporarily and hit /extract on a known public Reel."""
    if not SMOKE_EXTRACT_URL:
        return True, "smoke skipped (COOKIE_BOT_SMOKE_URL unset)"
    if DRY_RUN:
        return True, "smoke skipped (dry run)"
    snapshot, snap_src = fetch_live_cookies_from_backend()
    if not hot_reload_backend(netscape):
        return False, "smoke aborted: hot-reload failed before extract"
    try:
        resp = requests.get(
            f"{BACKEND_URL}/extract",
            params={"url": SMOKE_EXTRACT_URL},
            headers={"x-api-key": CLIENT_API_KEY},
            timeout=60,
        )
        if resp.status_code in (401, 403):
            if snapshot:
                hot_reload_backend(snapshot)
            return False, (
                f"smoke auth failed HTTP {resp.status_code} "
                f"(check LOOPHOLE_API_KEY / client token)"
            )
        ok = resp.status_code == 200
        if not ok and snapshot:
            log(f"Smoke failed; restoring prior cookies from {snap_src}")
            hot_reload_backend(snapshot)
        return ok, f"smoke extract -> {resp.status_code}"
    except Exception as e:
        if snapshot:
            hot_reload_backend(snapshot)
        return False, f"smoke extract error: {e}"


def fetch_live_cookies_from_backend() -> tuple[str, str]:
    if not ADMIN_KEY:
        return "", "no COOKIE_BOT_ADMIN_KEY"
    try:
        resp = requests.get(
            f"{BACKEND_URL}/admin/ig-cookies",
            headers={"x-api-key": ADMIN_KEY},
            timeout=30,
        )
        if resp.status_code != 200:
            return "", f"backend export HTTP {resp.status_code}"
        data = resp.json()
        cookies = ensure_netscape_header((data or {}).get("cookies") or "")
        if session_has_sessionid(cookies):
            return cookies, "backend live export"
        return "", "backend export missing sessionid"
    except Exception as e:
        return "", f"backend export error: {e}"


def fetch_live_cookies_from_render() -> tuple[str, str]:
    if not RENDER_API_KEY:
        return "", "no RENDER_API_KEY"
    try:
        resp = requests.get(
            f"https://api.render.com/v1/services/{RENDER_WEB_SERVICE_ID}/env-vars/IG_COOKIES",
            headers={
                "Authorization": f"Bearer {RENDER_API_KEY}",
                "Accept": "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return "", f"render GET IG_COOKIES HTTP {resp.status_code}"
        data = resp.json()
        # Render may return {key,value} or envVar wrapper
        value = ""
        if isinstance(data, dict):
            value = data.get("value") or (data.get("envVar") or {}).get("value") or ""
        cookies = ensure_netscape_header(value or "")
        if session_has_sessionid(cookies):
            return cookies, "render env IG_COOKIES"
        return "", "render IG_COOKIES missing sessionid"
    except Exception as e:
        return "", f"render export error: {e}"


def resolve_seed() -> tuple[str, str]:
    """
    Prefer live backend cookies, then Render env, then Actions/local IG_COOKIES.
    Avoids stale GitHub secret overwriting a healthy live session.
    """
    candidates = []

    live_be, detail_be = fetch_live_cookies_from_backend()
    log(f"Seed candidate backend: {detail_be}")
    if live_be:
        candidates.append((live_be, detail_be))

    live_render, detail_render = fetch_live_cookies_from_render()
    log(f"Seed candidate render: {detail_render}")
    if live_render:
        candidates.append((live_render, detail_render))

    seed_env = ensure_netscape_header(IG_COOKIES)
    if session_has_sessionid(seed_env):
        candidates.append((seed_env, "env/Actions IG_COOKIES"))
    elif seed_env:
        log("env/Actions IG_COOKIES present but no valid sessionid")

    if not candidates:
        return "", "no seed available"

    # Prefer a candidate that already passes liveness
    for cookies, source in candidates:
        alive, detail = probe_session_alive(cookies)
        log(f"Seed probe [{source}]: {detail}")
        if alive:
            return cookies, source

    # Fall back to first candidate (will try keep-alive / relogin)
    return candidates[0][0], candidates[0][1]


def keep_alive_with_curl(seed_cookies: str) -> tuple[str, str]:
    """Warm Instagram with existing cookies. Returns (netscape, detail)."""
    if cffi_requests is None:
        return seed_cookies, "curl_cffi missing"

    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    session, jar = build_cffi_session(seed_cookies)

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
            resp = session.get(
                url, headers=headers, proxies=proxies, timeout=45, allow_redirects=True
            )
            last_status = resp.status_code
            log(f"Keep-alive GET {url} -> {resp.status_code}")
            time.sleep(1.5)
        except Exception as e:
            log(f"Keep-alive request failed for {url}: {e}")

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

    netscape = ensure_netscape_header(jar_to_netscape(jar))
    return netscape, f"keep-alive status={last_status}"


def relogin_with_playwright(
    username: str, password: str
) -> tuple[Optional[str], str]:
    """Best-effort IG login for one account. Returns (netscape_or_none, detail)."""
    if not username or not password:
        return None, "username/password missing"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright not installed"

    proxy = parse_proxy(PROXY_URL)
    user_hint = username[:3] + "***"
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
            page.goto(
                "https://www.instagram.com/accounts/login/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            time.sleep(3)

            for label in (
                "Allow all cookies",
                "Accept",
                "Allow essential and optional cookies",
            ):
                try:
                    page.get_by_role("button", name=label).click(timeout=2000)
                    time.sleep(1)
                except Exception:
                    pass

            user_sel = 'input[name="username"]'
            pass_sel = 'input[name="password"]'
            page.wait_for_selector(user_sel, timeout=30000)
            page.fill(user_sel, username)
            page.fill(pass_sel, password)
            page.get_by_role("button", name="Log in").click()
            time.sleep(8)

            for text in ("Not Now", "Not now", "Save info"):
                try:
                    page.get_by_role("button", name=text).click(timeout=3000)
                    time.sleep(1)
                except Exception:
                    pass

            page.goto(
                "https://www.instagram.com/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            time.sleep(3)

            cookies = context.cookies()
            browser.close()

        lines = ["# Netscape HTTP Cookie File", "# LoopHole Playwright login export", ""]
        has_session = False
        for c in cookies:
            domain = c.get("domain") or ""
            if "instagram" not in domain:
                continue
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path") or "/"
            secure = "TRUE" if c.get("secure") else "FALSE"
            raw_expires = c.get("expires")
            # Playwright uses -1 for session cookies; Netscape wants 0
            if raw_expires is None or float(raw_expires) < 0:
                expires = "0"
            else:
                expires = str(int(float(raw_expires)))
            name = c.get("name") or ""
            value = c.get("value") or ""
            if name == "sessionid" and value and value not in ("0", "deleted"):
                has_session = True
            lines.append(
                "\t".join([domain, include_sub, path, secure, expires, name, value])
            )

        if not has_session:
            return None, f"Playwright login ({user_hint}) missing sessionid (checkpoint/2FA/captcha?)"

        return "\n".join(lines) + "\n", f"playwright login ok ({user_hint})"
    except Exception as e:
        return None, f"playwright login failed ({user_hint}): {e}"


def try_relogin_rotate() -> tuple[Optional[str], str, str]:
    """
    Try each configured IG account until one yields a live session.
    Returns (netscape_or_none, detail, source_label).
    """
    accounts = load_ig_accounts()
    if not accounts:
        return None, "no IG accounts configured (IG_ACCOUNTS_JSON / IG_USERNAME)", ""

    log(f"Playwright rotation: {len(accounts)} account(s)")
    last_detail = "no attempts"
    for idx, acct in enumerate(accounts, 1):
        user = acct["username"]
        log(f"Trying account {idx}/{len(accounts)} ({user[:3]}***)")
        fresh, detail = relogin_with_playwright(user, acct["password"])
        log(detail)
        last_detail = detail
        if not fresh:
            continue
        alive, alive_detail = probe_session_alive(fresh)
        log(f"Post-login liveness ({user[:3]}***): {alive_detail}")
        if alive:
            return fresh, alive_detail, f"playwright-login:{user[:3]}***"
    return None, last_detail, ""


def hot_reload_backend(netscape: str) -> bool:
    if DRY_RUN:
        log("DRY_RUN: skip hot-reload")
        return True
    if not ADMIN_KEY:
        log("COOKIE_BOT_ADMIN_KEY missing; cannot hot-reload")
        return False
    try:
        resp = requests.post(
            f"{BACKEND_URL}/admin/ig-cookies",
            headers={"x-api-key": ADMIN_KEY, "Content-Type": "application/json"},
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
    log("Starting Instagram cookie refresh (hardened)")
    if not PROXY_URL:
        log("WARNING: PROXY_URL empty — IG may block datacenter IP")
    if not ADMIN_KEY:
        log("ERROR: COOKIE_BOT_ADMIN_KEY required")
        send_alert(
            "🚨 *LoopHole Cookie Bot*\n\n"
            "Missing `COOKIE_BOT_ADMIN_KEY`. Refusing to run."
        )
        return 3

    accounts = load_ig_accounts()
    log(f"Configured IG login accounts: {len(accounts)}")

    seed, seed_source = resolve_seed()
    netscape = seed
    source = seed_source
    alive = False
    alive_detail = "no seed"

    if seed:
        log(f"Using seed from: {seed_source}")
        alive, alive_detail = probe_session_alive(netscape)
        log(f"Initial liveness: {alive_detail}")

        if not alive:
            try:
                warmed, detail = keep_alive_with_curl(seed)
                log(detail)
                alive, alive_detail = probe_session_alive(warmed)
                log(f"Post keep-alive liveness: {alive_detail}")
                if alive:
                    netscape = warmed
                    source = "keep-alive"
                else:
                    netscape = warmed
            except Exception as e:
                log(f"Keep-alive crashed: {e}")
                traceback.print_exc()
    else:
        log("No cookie seed — will rely on Playwright account rotation")

    if not alive:
        log("Session dead/missing — attempting Playwright account rotation")
        fresh, detail, login_source = try_relogin_rotate()
        if fresh:
            netscape = fresh
            alive = True
            alive_detail = detail
            source = login_source or "playwright-login"
        else:
            alive_detail = detail

    if not alive:
        safe_detail = (alive_detail or "unknown")[:240]
        send_alert(
            "🚨 *LoopHole Cookie Bot*\n\n"
            "Instagram session is dead after keep-alive/login rotation.\n"
            f"*Detail:* `{safe_detail}`\n"
            f"*Accounts tried:* {len(accounts)}\n\n"
            "Action: export fresh Netscape cookies from a dummy IG account "
            "in Chrome, then hot-reload / update `IG_COOKIES`."
        )
        return 2

    # Optional extract smoke (uses hot-reload internally)
    smoke_ok, smoke_detail = smoke_extract_with_backend(netscape)
    log(smoke_detail)
    if SMOKE_EXTRACT_URL and not smoke_ok:
        send_alert(
            "🚨 *LoopHole Cookie Bot*\n\n"
            f"Liveness OK but smoke extract failed: `{smoke_detail[:240]}`\n"
            "Not persisting cookies. Manual cookie refresh needed."
        )
        return 2

    # If smoke already hot-reloaded, still ensure final cookies are loaded
    hot_ok = hot_reload_backend(netscape)
    persist_ok = persist_to_render(netscape)

    send_alert(
        "🍪 *LoopHole Cookie Bot*\n\n"
        f"*Source:* {source}\n"
        f"*Liveness:* {alive_detail[:120]}\n"
        f"*Smoke:* {smoke_detail[:120]}\n"
        f"*Hot-reload:* {'ok' if hot_ok else 'FAIL'}\n"
        f"*Render persist:* {'ok' if persist_ok else 'fail/skip'}"
    )

    # Hot-reload is required for the live process; persist alone is not success.
    if not hot_ok:
        log("FAIL: hot-reload required but failed")
        return 1
    if not persist_ok:
        log("WARN: live hot-reload OK but Render persist failed (cold start may lose cookies)")
    log("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
