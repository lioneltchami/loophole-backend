"""Verify Google Play LoopHole Pro subscriptions for paid extract gate."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

import requests

PLAY_PACKAGE_NAME = (os.environ.get("PLAY_PACKAGE_NAME") or "com.loophole.app").strip()
PLAY_SUBSCRIPTION_ID = (
    os.environ.get("PLAY_SUBSCRIPTION_ID") or "loophole_pro_monthly"
).strip()
PRO_TOKEN_HEADER = "x-play-purchase-token"

# In-memory positive cache so extract storms do not hammer Play API.
_CACHE: dict[str, float] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SEC = 600
_CACHE_MAX = 2000

_creds = None
_creds_lock = threading.Lock()
_warned_missing_sa = False


def _service_account_info() -> Optional[dict]:
    raw = (os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        path = (os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_FILE") or "").strip()
        if path and os.path.isfile(path):
            raw = open(path, encoding="utf-8").read()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("[play-pro] GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is not valid JSON")
        return None


def _access_token() -> Optional[str]:
    """OAuth token for androidpublisher via service account."""
    global _creds, _warned_missing_sa
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


def _cache_put(token: str) -> None:
    now = time.time()
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            # Drop expired / oldest half
            dead = [k for k, e in _CACHE.items() if e <= now]
            for k in dead:
                _CACHE.pop(k, None)
            if len(_CACHE) >= _CACHE_MAX:
                for k in list(_CACHE.keys())[: len(_CACHE) // 2]:
                    _CACHE.pop(k, None)
        _CACHE[token] = now + _CACHE_TTL_SEC


def _subscription_active(payload: dict) -> bool:
    state = (payload.get("subscriptionState") or "").upper()
    if state in (
        "SUBSCRIPTION_STATE_ACTIVE",
        "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
    ):
        return True
    # Canceled but still in paid period
    if state == "SUBSCRIPTION_STATE_CANCELED":
        for line in payload.get("lineItems") or []:
            expiry = (line.get("expiryTime") or "").strip()
            if not expiry:
                continue
            try:
                # RFC3339
                from datetime import datetime, timezone

                exp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if exp > datetime.now(timezone.utc):
                    return True
            except Exception:
                continue
    return False


def verify_play_purchase_token(token: str) -> bool:
    """Return True if token is an active LoopHole Pro Play subscription."""
    token = (token or "").strip()
    if not token:
        return False
    if _cache_get(token):
        return True

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
        return False

    try:
        payload = resp.json()
    except Exception:
        return False

    if not _subscription_active(payload):
        print(f"[play-pro] token not active state={payload.get('subscriptionState')}")
        return False

    _cache_put(token)
    return True


def request_has_verified_pro(request) -> bool:
    """Hard gate: verified Play purchase token only (not rewarded temp Pro)."""
    token = (request.headers.get(PRO_TOKEN_HEADER) or "").strip()
    if not token:
        return False
    return verify_play_purchase_token(token)
