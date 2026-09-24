"""Verify Google Play LoopHole Pro subscriptions for paid extract gate."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

PLAY_PACKAGE_NAME = (os.environ.get("PLAY_PACKAGE_NAME") or "com.loophole.app").strip()
PLAY_SUBSCRIPTION_ID = (
    os.environ.get("PLAY_SUBSCRIPTION_ID") or "loophole_pro_monthly"
).strip()
PRO_TOKEN_HEADER = "x-play-purchase-token"

# Positive cache: verified Pro tokens.
_CACHE: dict[str, float] = {}
# Negative cache: known-bad / unverifiable tokens (short TTL).
_NEG_CACHE: dict[str, float] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SEC = 600
_NEG_CACHE_TTL_SEC = 90
_CACHE_MAX = 2000
_TOKEN_MIN_LEN = 20
_TOKEN_MAX_LEN = 4096

_creds = None
_creds_lock = threading.Lock()
_warned_missing_sa = False


def _service_account_info() -> Optional[dict]:
    raw = (os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        path = (os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_FILE") or "").strip()
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = f.read()
            except OSError as e:
                print(f"[play-pro] cannot read SA file: {e}")
                return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("[play-pro] GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is not valid JSON")
        return None


def _access_token() -> Optional[str]:
    """OAuth token for androidpublisher. Never raises — fail closed."""
    global _creds, _warned_missing_sa
    try:
        info = _service_account_info()
        if not info:
            if not _warned_missing_sa:
                print(
                    "[play-pro] No GOOGLE_PLAY_SERVICE_ACCOUNT_JSON — "
                    "ScrapeCreators stays locked for all clients (fail-closed)"
                )
                _warned_missing_sa = True
            return None
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request
        except ImportError:
            print("[play-pro] google-auth not installed")
            return None

        with _creds_lock:
            if _creds is None:
                _creds = service_account.Credentials.from_service_account_info(
                    info,
                    scopes=["https://www.googleapis.com/auth/androidpublisher"],
                )
            if not _creds.valid:
                _creds.refresh(Request())
            return _creds.token
    except Exception as e:
        print(f"[play-pro] access token failed (fail-closed): {type(e).__name__}: {e}")
        with _creds_lock:
            _creds = None
        return None


def _cache_get(token: str) -> bool:
    now = time.time()
    with _CACHE_LOCK:
        exp = _CACHE.get(token)
        if exp is None:
            return False
        if now >= exp:
            _CACHE.pop(token, None)
            return False
        return True


def _neg_cache_get(token: str) -> bool:
    now = time.time()
    with _CACHE_LOCK:
        exp = _NEG_CACHE.get(token)
        if exp is None:
            return False
        if now >= exp:
            _NEG_CACHE.pop(token, None)
            return False
        return True


def _cache_put(token: str) -> None:
    now = time.time()
    with _CACHE_LOCK:
        _NEG_CACHE.pop(token, None)
        if len(_CACHE) >= _CACHE_MAX:
            dead = [k for k, e in _CACHE.items() if e <= now]
            for k in dead:
                _CACHE.pop(k, None)
            if len(_CACHE) >= _CACHE_MAX:
                for k in list(_CACHE.keys())[: len(_CACHE) // 2]:
                    _CACHE.pop(k, None)
        _CACHE[token] = now + _CACHE_TTL_SEC


def _neg_cache_put(token: str) -> None:
    now = time.time()
    with _CACHE_LOCK:
        if len(_NEG_CACHE) >= _CACHE_MAX:
            dead = [k for k, e in _NEG_CACHE.items() if e <= now]
            for k in dead:
                _NEG_CACHE.pop(k, None)
            if len(_NEG_CACHE) >= _CACHE_MAX:
                for k in list(_NEG_CACHE.keys())[: len(_NEG_CACHE) // 2]:
                    _NEG_CACHE.pop(k, None)
        _NEG_CACHE[token] = now + _NEG_CACHE_TTL_SEC


def _product_lines(payload: dict) -> list:
    """Return lineItems that match PLAY_SUBSCRIPTION_ID (or all if none declare productId)."""
    lines = list(payload.get("lineItems") or [])
    if not lines:
        return []
    declared = [(ln.get("productId") or "").strip() for ln in lines]
    if any(declared):
        matched = [
            ln for ln in lines
            if (ln.get("productId") or "").strip() == PLAY_SUBSCRIPTION_ID
        ]
        return matched
    return lines


def _subscription_active(payload: dict) -> bool:
    state = (payload.get("subscriptionState") or "").upper()
    lines = list(payload.get("lineItems") or [])
    if lines and any((ln.get("productId") or "").strip() for ln in lines):
        matched = _product_lines(payload)
        if not matched:
            print(
                f"[play-pro] token product mismatch want={PLAY_SUBSCRIPTION_ID} "
                f"got={[ln.get('productId') for ln in lines]}"
            )
            return False
    else:
        matched = lines

    if state in (
        "SUBSCRIPTION_STATE_ACTIVE",
        "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
    ):
        return True

    if state == "SUBSCRIPTION_STATE_CANCELED":
        for line in matched or lines:
            expiry = (line.get("expiryTime") or "").strip()
            if not expiry:
                continue
            try:
                exp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if exp > datetime.now(timezone.utc):
                    return True
            except Exception:
                continue
    return False


def verify_play_purchase_token(token: str) -> bool:
    """Return True if token is an active LoopHole Pro Play subscription.

    Never raises — always fail-closed on auth/network/payload errors.
    """
    try:
        token = (token or "").strip()
        if not token or not (_TOKEN_MIN_LEN <= len(token) <= _TOKEN_MAX_LEN):
            return False
        if _cache_get(token):
            return True
        if _neg_cache_get(token):
            return False

        access = _access_token()
        if not access:
            return False

        url = (
            "https://androidpublisher.googleapis.com/androidpublisher/v3/"
            f"applications/{PLAY_PACKAGE_NAME}/purchases/subscriptionsv2/tokens/"
            f"{requests.utils.quote(token, safe='')}"
        )
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {access}"},
                timeout=12,
            )
        except Exception as e:
            print(f"[play-pro] verify request failed: {e}")
            return False

        if resp.status_code != 200:
            print(f"[play-pro] verify HTTP {resp.status_code}: {resp.text[:200]}")
            # 4xx from Play → token invalid/expired; short neg-cache.
            if 400 <= resp.status_code < 500:
                _neg_cache_put(token)
            return False

        try:
            payload = resp.json()
        except Exception:
            return False

        if not _subscription_active(payload):
            print(f"[play-pro] token not active state={payload.get('subscriptionState')}")
            _neg_cache_put(token)
            return False

        _cache_put(token)
        return True
    except Exception as e:
        print(f"[play-pro] verify unexpected error (fail-closed): {type(e).__name__}: {e}")
        return False


def request_has_verified_pro(request) -> bool:
    """Hard gate: verified Play purchase token only (not rewarded temp Pro)."""
    try:
        token = (request.headers.get(PRO_TOKEN_HEADER) or "").strip()
        if not token:
            return False
        return verify_play_purchase_token(token)
    except Exception as e:
        print(f"[play-pro] request_has_verified_pro failed: {e}")
        return False
