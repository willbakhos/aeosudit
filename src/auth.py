"""monitor-dashboard auth: Supabase magic link + cookie session.

Flow:
  1. POST /dashboard/login (email)
       → supabase.auth.sign_in_with_otp({email, redirect=/dashboard/auth/callback})
       → Supabase emails a one-time link.
  2. User clicks the link → Supabase verifies → redirects to
     /dashboard/auth/callback#access_token=...&refresh_token=...
  3. The callback page is a tiny JS shim that POSTs the access_token from the
     URL fragment to /dashboard/auth/exchange, which sets a HttpOnly cookie.
  4. Subsequent requests carry the cookie; we verify the JWT locally — either
     via JWKS (modern Supabase signs with ES256/RS256 asymmetric keys) or with
     SUPABASE_JWT_SECRET (legacy HS256 shared secret). No round-trip per request.
"""
from __future__ import annotations

import os

import jwt
from fastapi import Request, Response
from jwt import PyJWKClient

COOKIE_NAME = "monitor_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def supabase_configured() -> bool:
    return bool(_env("SUPABASE_URL") and _env("SUPABASE_ANON_KEY"))


def _client():
    """Lazy import so the rest of the app boots without supabase installed."""
    from supabase import create_client  # type: ignore[import-not-found]
    return create_client(_env("SUPABASE_URL"), _env("SUPABASE_ANON_KEY"))


def send_magic_link(email: str, redirect_url: str) -> None:
    """Trigger Supabase to email the user a magic-link OTP."""
    _client().auth.sign_in_with_otp(
        {"email": email, "options": {"email_redirect_to": redirect_url}}
    )


def set_session_cookie(response: Response, access_token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        access_token,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        samesite="lax",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient | None:
    """Cache one PyJWKClient per process — it fetches + caches the JWKS itself."""
    global _jwks_client
    if _jwks_client is None:
        url = _env("SUPABASE_URL")
        if not url:
            return None
        _jwks_client = PyJWKClient(f"{url}/auth/v1/.well-known/jwks.json")
    return _jwks_client


def current_user(request: Request) -> dict | None:
    """Read the session cookie, verify the Supabase JWT, return {id, email}.
    Returns None if no cookie, expired, or signature invalid.

    Tries asymmetric verification (ES256/RS256 via JWKS) first because new
    Supabase projects sign with elliptic-curve keys. Falls back to HS256 +
    SUPABASE_JWT_SECRET for older projects still using the shared-secret mode.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None

    payload: dict | None = None

    # Try asymmetric (JWKS) — modern Supabase default.
    try:
        client = _jwks()
        if client is not None:
            signing_key = client.get_signing_key_from_jwt(token).key
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=["ES256", "RS256"],
                audience="authenticated",
            )
    except jwt.PyJWTError:
        payload = None
    except Exception:  # noqa: BLE001 — JWKS fetch errors, network failures
        payload = None

    # Fall back to HS256 with the legacy shared secret.
    if payload is None:
        secret = _env("SUPABASE_JWT_SECRET")
        if not secret:
            return None
        try:
            payload = jwt.decode(
                token, secret, algorithms=["HS256"], audience="authenticated"
            )
        except jwt.PyJWTError:
            return None

    sub = payload.get("sub")
    if not sub:
        return None
    return {"id": sub, "email": payload.get("email")}
