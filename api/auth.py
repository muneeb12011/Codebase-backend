from flask import Blueprint, redirect, request, make_response, jsonify
import os
import jwt
import datetime
import requests as http

auth_bp = Blueprint("auth", __name__)

# ========================
# ENV
# ========================
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

GITHUB_CLIENT_ID     = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")

JWT_SECRET   = os.getenv("JWT_SECRET")
BASE_URL     = os.getenv("BASE_URL")      # e.g. https://codebasebackend.vercel.app
FRONTEND_URL = os.getenv("FRONTEND_URL")  # e.g. https://codebase-nine-iota.vercel.app

for _name, _val in [
    ("JWT_SECRET", JWT_SECRET),
    ("BASE_URL", BASE_URL),
    ("FRONTEND_URL", FRONTEND_URL),
]:
    if not _val:
        raise RuntimeError(f"{_name} env var is required")


# ========================
# HELPERS
# ========================
def create_token(user: dict) -> str:
    payload = {
        "user": user,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _set_cors(res):
    origin = request.headers.get("Origin", FRONTEND_URL)
    res.headers["Access-Control-Allow-Origin"]      = origin
    res.headers["Access-Control-Allow-Credentials"] = "true"
    res.headers["Access-Control-Allow-Headers"]     = "Content-Type, Authorization"
    res.headers["Access-Control-Allow-Methods"]     = "GET, POST, OPTIONS"
    return res


def _preflight():
    res = make_response("", 204)
    return _set_cors(res)


def _token_redirect(jwt_token: str, dest: str):
    """
    Cross-domain safe: pass JWT in URL query param instead of cookie.
    Frontend reads ?token=... and stores in localStorage.
    """
    res = make_response(redirect(f"{dest}?token={jwt_token}"))
    return _set_cors(res)


def _error_redirect(msg: str):
    return redirect(f"{FRONTEND_URL}/login?error={msg}")


def _get_token_from_request() -> str | None:
    """
    Read JWT from Authorization: Bearer <token> header.
    Falls back to ?token= query param for convenience.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return request.args.get("token") or None


# ========================
# GOOGLE — step 1: redirect
# ========================
@auth_bp.route("/google", methods=["GET", "OPTIONS"])
def google_login():
    if request.method == "OPTIONS":
        return _preflight()

    from urllib.parse import urlencode
    params = urlencode({
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  f"{BASE_URL}/api/auth/google/callback",
        "response_type": "code",
        "scope":         "openid email profile",
        "prompt":        "consent",
        "access_type":   "offline",
    })
    return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


# ========================
# GOOGLE — step 2: callback
# ========================
@auth_bp.route("/google/callback", methods=["GET", "OPTIONS"])
def google_callback():
    if request.method == "OPTIONS":
        return _preflight()

    code  = request.args.get("code")
    error = request.args.get("error")

    if error or not code:
        return _error_redirect(error or "access_denied")

    try:
        token_res = http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code":          code,
                "client_id":     GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri":  f"{BASE_URL}/api/auth/google/callback",
                "grant_type":    "authorization_code",
            },
            timeout=10,
        )
        token_res.raise_for_status()
        access_token = token_res.json().get("access_token")
    except Exception as e:
        return _error_redirect(f"token_error: {e}")

    try:
        userinfo = http.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        ).json()
    except Exception as e:
        return _error_redirect(f"userinfo_error: {e}")

    user = {
        "email":         userinfo.get("email"),
        "name":          userinfo.get("name"),
        "picture":       userinfo.get("picture"),
        "provider":      "google",
        "emailVerified": userinfo.get("email_verified", True),
        "createdAt":     datetime.datetime.utcnow().isoformat(),
    }

    return _token_redirect(create_token(user), f"{FRONTEND_URL}/dashboard")


# ========================
# GITHUB — step 1: redirect
# ========================
@auth_bp.route("/github", methods=["GET", "OPTIONS"])
def github_login():
    if request.method == "OPTIONS":
        return _preflight()

    from urllib.parse import urlencode
    params = urlencode({
        "client_id":    GITHUB_CLIENT_ID,
        "redirect_uri": f"{BASE_URL}/api/auth/github/callback",
        "scope":        "user:email",
    })
    return redirect(f"https://github.com/login/oauth/authorize?{params}")


# ========================
# GITHUB — step 2: callback
# ========================
@auth_bp.route("/github/callback", methods=["GET", "OPTIONS"])
def github_callback():
    if request.method == "OPTIONS":
        return _preflight()

    code  = request.args.get("code")
    error = request.args.get("error")

    if error or not code:
        return _error_redirect(error or "access_denied")

    try:
        token_res = http.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "code":          code,
                "client_id":     GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "redirect_uri":  f"{BASE_URL}/api/auth/github/callback",
            },
            timeout=10,
        )
        token_res.raise_for_status()
        access_token = token_res.json().get("access_token")
        if not access_token:
            raise ValueError("No access_token in response")
    except Exception as e:
        return _error_redirect(f"token_error: {e}")

    gh_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept":        "application/vnd.github+json",
    }

    try:
        userinfo = http.get(
            "https://api.github.com/user",
            headers=gh_headers,
            timeout=10,
        ).json()
    except Exception as e:
        return _error_redirect(f"userinfo_error: {e}")

    email = userinfo.get("email")
    if not email:
        try:
            emails = http.get(
                "https://api.github.com/user/emails",
                headers=gh_headers,
                timeout=10,
            ).json()
            primary = next((e for e in emails if e.get("primary")), {})
            email = primary.get("email")
        except Exception:
            pass

    user = {
        "email":         email or f"{userinfo.get('login')}@users.noreply.github.com",
        "name":          userinfo.get("name") or userinfo.get("login"),
        "picture":       userinfo.get("avatar_url"),
        "provider":      "github",
        "emailVerified": True,
        "createdAt":     datetime.datetime.utcnow().isoformat(),
    }

    return _token_redirect(create_token(user), f"{FRONTEND_URL}/dashboard")


# ========================
# GET CURRENT USER
# ========================
@auth_bp.route("/me", methods=["GET", "OPTIONS"])
def get_me():
    if request.method == "OPTIONS":
        return _preflight()

    token = _get_token_from_request()

    if not token:
        res = jsonify({"error": "Not authenticated"})
        res.status_code = 401
        return _set_cors(res)

    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        res = jsonify(data["user"])
        return _set_cors(res)
    except jwt.ExpiredSignatureError:
        res = jsonify({"error": "Token expired"})
        res.status_code = 401
        return _set_cors(res)
    except Exception:
        res = jsonify({"error": "Invalid token"})
        res.status_code = 401
        return _set_cors(res)


# ========================
# LOGOUT
# ========================
@auth_bp.route("/logout", methods=["GET", "POST", "OPTIONS"])
def logout():
    if request.method == "OPTIONS":
        return _preflight()

    # Token lives in localStorage on frontend — just confirm logout server-side
    res = jsonify({"message": "Logged out"})
    return _set_cors(res)