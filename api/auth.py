from flask import Blueprint, redirect, request, make_response, jsonify
from authlib.integrations.requests_client import OAuth2Session
import os
import json
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

JWT_SECRET = os.getenv("JWT_SECRET", "supersecret")  # change in prod!

BASE_URL = "https://your-backend.vercel.app"
FRONTEND_URL = "https://codebase-8x89xgizo-muneeb12011s-projects.vercel.app"


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
    google = OAuth2Session(
        GOOGLE_CLIENT_ID,
        scope="openid email profile",
        redirect_uri=f"{BASE_URL}/api/auth/google/callback",
    )

    url, _ = google.authorization_url(
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
    google = OAuth2Session(
        GOOGLE_CLIENT_ID,
        redirect_uri=f"{BASE_URL}/api/auth/google/callback",
    )

    token = google.fetch_token(
        "https://oauth2.googleapis.com/token",
        client_secret=GOOGLE_CLIENT_SECRET,
        authorization_response=request.url,
    )

    userinfo = google.get(
        "https://www.googleapis.com/oauth2/v1/userinfo",
        token=token
    ).json()

    user = {
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
        "provider": "google"
    }

    jwt_token = create_token(user)

    res = make_response(redirect(f"{FRONTEND_URL}/dashboard"))
    res.set_cookie(
        "token",
        jwt_token,
        httponly=True,
        secure=True,
        samesite="None"
    )

    return res


# ========================
# GITHUB LOGIN
# ========================
@auth_bp.route("/api/auth/github")
def github_login():
    github = OAuth2Session(
        GITHUB_CLIENT_ID,
        redirect_uri=f"{BASE_URL}/api/auth/github/callback",
    )

    url, _ = github.authorization_url(
        "https://github.com/login/oauth/authorize"
    )

    return redirect(url)


# ========================
# GITHUB CALLBACK
# ========================
@auth_bp.route("/api/auth/github/callback")
def github_callback():
    github = OAuth2Session(
        GITHUB_CLIENT_ID,
        redirect_uri=f"{BASE_URL}/api/auth/github/callback",
    )

    token = github.fetch_token(
        "https://github.com/login/oauth/access_token",
        client_secret=GITHUB_CLIENT_SECRET,
        authorization_response=request.url,
    )

    userinfo = github.get(
        "https://api.github.com/user",
        token=token
    ).json()

    user = {
        "email": userinfo.get("email"),
        "name": userinfo.get("login"),
        "picture": userinfo.get("avatar_url"),
        "provider": "github"
    }

    jwt_token = create_token(user)

    res = make_response(redirect(f"{FRONTEND_URL}/dashboard"))
    res.set_cookie(
        "token",
        jwt_token,
        httponly=True,
        secure=True,
        samesite="None"
    )

    return res


# ========================
# GET CURRENT USER
# ========================
@auth_bp.route("/api/me")
def get_me():
    token = request.cookies.get("token")

    if not token:
        return jsonify({"user": None}), 401

    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return jsonify(data)
    except:
        return jsonify({"user": None}), 401


# ========================
# LOGOUT
# ========================
@auth_bp.route("/api/logout")
def logout():
    res = make_response(jsonify({"message": "Logged out"}))
    res.set_cookie("token", "", expires=0)
    return res