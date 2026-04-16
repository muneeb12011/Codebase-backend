from flask import Blueprint, redirect, request, make_response, jsonify
from authlib.integrations.requests_client import OAuth2Session
import os
import jwt
import datetime

auth_bp = Blueprint("auth", __name__)

# ========================
# ENV
# ========================
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is required")

BASE_URL = os.getenv("BASE_URL")
FRONTEND_URL = os.getenv("FRONTEND_URL")

# ========================
# JWT GENERATOR
# ========================
def create_token(user):
    payload = {
        "user": user,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


# ========================
# GOOGLE LOGIN
# ========================
@auth_bp.route("/api/auth/google")
def google_login():
    if not GOOGLE_CLIENT_ID:
        return jsonify({"error": "Google OAuth not configured"}), 500

    google = OAuth2Session(
        GOOGLE_CLIENT_ID,
        scope="openid email profile",
        redirect_uri=f"{BASE_URL}/api/auth/google/callback",
    )

    url, _ = google.create_authorization_url(
        "https://accounts.google.com/o/oauth2/auth",
        access_type="offline",
        prompt="consent",
    )

    return redirect(url)


# ========================
# GOOGLE CALLBACK
# ========================
@auth_bp.route("/api/auth/google/callback")
def google_callback():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return jsonify({"error": "Google OAuth not configured"}), 500

    google = OAuth2Session(
        GOOGLE_CLIENT_ID,
        redirect_uri=f"{BASE_URL}/api/auth/google/callback",
    )

    try:
        # Exchange code for token
        token = google.fetch_token(
            "https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            authorization_response=request.url,
        )

        # Get user info (modern endpoint)
        userinfo = google.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            token=token
        ).json()

    except Exception as e:
        return jsonify({"error": f"OAuth failed: {str(e)}"}), 400

    if "email" not in userinfo:
        return jsonify({"error": "Failed to fetch user info"}), 400

    user = {
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
        "provider": "google",
        "createdAt": datetime.datetime.utcnow().isoformat(),
    }

    jwt_token = create_token(user)

    res = make_response(redirect(f"{FRONTEND_URL}/dashboard"))
    res.set_cookie(
        "token",
        jwt_token,
        httponly=True,
        secure=True,
        samesite="None",
        path="/"
    )

    return res


@auth_bp.route("/api/auth/github")
def github_login():
    if not GITHUB_CLIENT_ID:
        return jsonify({"error": "GitHub OAuth not configured"}), 500

    github = OAuth2Session(
        GITHUB_CLIENT_ID,
        redirect_uri=f"{BASE_URL}/api/auth/github/callback",
    )

    url, _ = github.create_authorization_url(
        "https://github.com/login/oauth/authorize"
    )

    return redirect(url)

@auth_bp.route("/api/auth/github/callback")
def github_callback():
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return jsonify({"error": "GitHub OAuth not configured"}), 500

    github = OAuth2Session(
        GITHUB_CLIENT_ID,
        redirect_uri=f"{BASE_URL}/api/auth/github/callback",
    )

    try:
        # Exchange code for token
        token = github.fetch_token(
            "https://github.com/login/oauth/access_token",
            client_id=GITHUB_CLIENT_ID,
            client_secret=GITHUB_CLIENT_SECRET,
            authorization_response=request.url,
        )

        # Get user info
        userinfo = github.get(
            "https://api.github.com/user",
            token=token
        ).json()

        # GitHub may not return email here → fetch separately
        if not userinfo.get("email"):
            emails = github.get(
                "https://api.github.com/user/emails",
                token=token
            ).json()
            primary = next((e for e in emails if e.get("primary")), {})
            email = primary.get("email")
        else:
            email = userinfo.get("email")

    except Exception as e:
        return jsonify({"error": f"OAuth failed: {str(e)}"}), 400

    user = {
        "email": email or f"{userinfo.get('login')}@github",
        "name": userinfo.get("login"),
        "picture": userinfo.get("avatar_url"),
        "provider": "github",
        "createdAt": datetime.datetime.utcnow().isoformat(),
    }

    jwt_token = create_token(user)

    res = make_response(redirect(f"{FRONTEND_URL}/dashboard"))
    res.set_cookie(
        "token",
        jwt_token,
        httponly=True,
        secure=True,
        samesite="None",
        path="/"
    )

    return res


# ========================
# GET CURRENT USER
# ========================
@auth_bp.route("/api/auth/me")
def get_me():
    token = request.cookies.get("token")

    if not token:
        return jsonify({"user": None}), 401

    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return jsonify({"user": data.get("user", data)})
    except Exception:
        return jsonify({"user": None}), 401


# ========================
# LOGOUT
# ========================
@auth_bp.route("/api/auth/logout")
def logout():
    res = make_response(jsonify({"message": "Logged out"}))
    res.set_cookie(
        "token",
        "",
        expires=0,
        httponly=True,
        secure=True,
        samesite="None",
        path="/"
    )
    return res