from flask import Blueprint, redirect, request, make_response, jsonify
import os
import jwt
import datetime
import secrets
import hashlib
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
BASE_URL     = os.getenv("BASE_URL")
FRONTEND_URL = os.getenv("FRONTEND_URL")

# Optional: for sending verification emails
SMTP_FROM    = os.getenv("SMTP_FROM", "noreply@codebasevisualizer.com")
SENDGRID_KEY = os.getenv("SENDGRID_API_KEY")  # optional

for _name, _val in [
    ("JWT_SECRET", JWT_SECRET),
    ("BASE_URL", BASE_URL),
    ("FRONTEND_URL", FRONTEND_URL),
]:
    if not _val:
        raise RuntimeError(f"{_name} env var is required")

# ========================
# IN-MEMORY STORES
# (swap for a DB in production)
# ========================
# { email: { name, password_hash, verified, verify_token, created_at } }
_users: dict = {}
# { verify_token: email }
_verify_tokens: dict = {}
# { reset_token: { email, expires } }
_reset_tokens: dict = {}


# ========================
# HELPERS
# ========================
def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def _check_password(password: str, hashed: str) -> bool:
    return hashlib.sha256(password.encode()).hexdigest() == hashed

def create_token(user: dict, days: int = 7) -> str:
    payload = {
        "user": user,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=days),
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

def _json(data: dict, status: int = 200):
    res = jsonify(data)
    res.status_code = status
    return _set_cors(res)

def _token_redirect(jwt_token: str, dest: str):
    res = make_response(redirect(f"{dest}?token={jwt_token}"))
    return _set_cors(res)

def _error_redirect(msg: str):
    return redirect(f"{FRONTEND_URL}/login?error={msg}")

def _get_bearer_token() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.args.get("token") or None

def _send_verification_email(email: str, token: str):
    """Send verification email via SendGrid if key is set, else just print."""
    verify_url = f"{FRONTEND_URL}/verify-email?token={token}"
    if not SENDGRID_KEY:
        # Dev mode — print to logs
        print(f"[DEV] Verify email for {email}: {verify_url}")
        return
    try:
        http.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "personalizations": [{"to": [{"email": email}]}],
                "from": {"email": SMTP_FROM},
                "subject": "Verify your Codebase Visualizer account",
                "content": [{
                    "type": "text/html",
                    "value": f"""
                        <h2>Welcome to Codebase Visualizer!</h2>
                        <p>Click the link below to verify your email address:</p>
                        <a href="{verify_url}" style="
                            display:inline-block;padding:12px 24px;
                            background:#6366f1;color:white;
                            border-radius:8px;text-decoration:none;font-weight:600;
                        ">Verify Email</a>
                        <p style="color:#888;font-size:12px;">
                            This link expires in 24 hours.
                        </p>
                    """,
                }],
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[WARN] Failed to send verification email: {e}")


# ========================
# EMAIL SIGNUP
# ========================
@auth_bp.route("/signup", methods=["POST", "OPTIONS"])
def signup():
    if request.method == "OPTIONS":
        return _preflight()

    body = request.get_json(silent=True) or {}
    name     = (body.get("name") or "").strip()
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if not name or not email or not password:
        return _json({"error": "Name, email and password are required"}, 400)

    if len(password) < 8:
        return _json({"error": "Password must be at least 8 characters"}, 400)

    if email in _users:
        return _json({"error": "An account with this email already exists"}, 409)

    verify_token = secrets.token_urlsafe(32)
    _users[email] = {
        "name":          name,
        "email":         email,
        "password_hash": _hash_password(password),
        "provider":      "email",
        "verified":      False,
        "verify_token":  verify_token,
        "created_at":    datetime.datetime.utcnow().isoformat(),
    }
    _verify_tokens[verify_token] = email

    _send_verification_email(email, verify_token)

    return _json({
        "message":              "Account created. Please verify your email.",
        "requiresVerification": True,
        "user": {
            "email":         email,
            "name":          name,
            "provider":      "email",
            "emailVerified": False,
        },
    }, 201)


# ========================
# EMAIL LOGIN
# ========================
@auth_bp.route("/login", methods=["POST", "OPTIONS"])
def login():
    if request.method == "OPTIONS":
        return _preflight()

    body = request.get_json(silent=True) or {}
    email    = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if not email or not password:
        return _json({"error": "Email and password are required"}, 400)

    stored = _users.get(email)
    if not stored or not _check_password(password, stored["password_hash"]):
        return _json({"error": "Invalid email or password"}, 401)

    if not stored.get("verified"):
        return _json({
            "error":               "Please verify your email before signing in.",
            "requiresVerification": True,
        }, 403)

    user = {
        "email":         email,
        "name":          stored["name"],
        "provider":      "email",
        "emailVerified": True,
        "createdAt":     stored["created_at"],
    }

    return _json({"token": create_token(user), "user": user})


# ========================
# VERIFY EMAIL
# ========================
@auth_bp.route("/verify-email", methods=["GET", "OPTIONS"])
def verify_email():
    if request.method == "OPTIONS":
        return _preflight()

    token = request.args.get("token", "")
    email = _verify_tokens.get(token)

    if not email or email not in _users:
        return _error_redirect("invalid_verification_token")

    _users[email]["verified"] = True
    del _verify_tokens[token]

    user = {
        "email":         email,
        "name":          _users[email]["name"],
        "provider":      "email",
        "emailVerified": True,
        "createdAt":     _users[email]["created_at"],
    }

    return _token_redirect(create_token(user), f"{FRONTEND_URL}/dashboard")


# ========================
# RESEND VERIFICATION
# ========================
@auth_bp.route("/resend-verification", methods=["POST", "OPTIONS"])
def resend_verification():
    if request.method == "OPTIONS":
        return _preflight()

    body  = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()

    stored = _users.get(email)
    if not stored:
        # Don't reveal if email exists
        return _json({"message": "If that email exists, a verification link was sent."})

    if stored.get("verified"):
        return _json({"message": "Email is already verified."})

    new_token = secrets.token_urlsafe(32)
    stored["verify_token"] = new_token
    _verify_tokens[new_token] = email

    _send_verification_email(email, new_token)
    return _json({"message": "Verification email resent."})


# ========================
# FORGOT PASSWORD
# ========================
@auth_bp.route("/forgot-password", methods=["POST", "OPTIONS"])
def forgot_password():
    if request.method == "OPTIONS":
        return _preflight()

    body  = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()

    if email in _users:
        reset_token = secrets.token_urlsafe(32)
        _reset_tokens[reset_token] = {
            "email":   email,
            "expires": datetime.datetime.utcnow() + datetime.timedelta(hours=1),
        }
        reset_url = f"{FRONTEND_URL}/reset-password?token={reset_token}"
        print(f"[DEV] Password reset for {email}: {reset_url}")

        if SENDGRID_KEY:
            try:
                http.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={
                        "Authorization": f"Bearer {SENDGRID_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "personalizations": [{"to": [{"email": email}]}],
                        "from": {"email": SMTP_FROM},
                        "subject": "Reset your Codebase Visualizer password",
                        "content": [{
                            "type": "text/html",
                            "value": f"""
                                <h2>Password Reset</h2>
                                <p>Click below to reset your password. This link expires in 1 hour.</p>
                                <a href="{reset_url}" style="
                                    display:inline-block;padding:12px 24px;
                                    background:#6366f1;color:white;
                                    border-radius:8px;text-decoration:none;font-weight:600;
                                ">Reset Password</a>
                            """,
                        }],
                    },
                    timeout=10,
                )
            except Exception as e:
                print(f"[WARN] Failed to send reset email: {e}")

    return _json({"message": "If that email exists, a reset link was sent."})


# ========================
# RESET PASSWORD
# ========================
@auth_bp.route("/reset-password", methods=["POST", "OPTIONS"])
def reset_password():
    if request.method == "OPTIONS":
        return _preflight()

    body     = request.get_json(silent=True) or {}
    token    = body.get("token", "")
    password = body.get("password", "")

    entry = _reset_tokens.get(token)
    if not entry:
        return _json({"error": "Invalid or expired reset token"}, 400)

    if datetime.datetime.utcnow() > entry["expires"]:
        del _reset_tokens[token]
        return _json({"error": "Reset token has expired"}, 400)

    email = entry["email"]
    if email not in _users:
        return _json({"error": "User not found"}, 404)

    if len(password) < 8:
        return _json({"error": "Password must be at least 8 characters"}, 400)

    _users[email]["password_hash"] = _hash_password(password)
    del _reset_tokens[token]

    return _json({"message": "Password reset successfully. You can now sign in."})


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
        return _error_redirect(f"token_error")

    try:
        userinfo = http.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        ).json()
    except Exception as e:
        return _error_redirect("userinfo_error")

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
        return _error_redirect("token_error")

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
    except Exception:
        return _error_redirect("userinfo_error")

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

    token = _get_bearer_token()
    if not token:
        return _json({"error": "Not authenticated"}, 401)

    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return _json(data["user"])
    except jwt.ExpiredSignatureError:
        return _json({"error": "Token expired"}, 401)
    except Exception:
        return _json({"error": "Invalid token"}, 401)


# ========================
# LOGOUT
# ========================
@auth_bp.route("/logout", methods=["GET", "POST", "OPTIONS"])
def logout():
    if request.method == "OPTIONS":
        return _preflight()
    # Token is in localStorage on frontend — just confirm
    return _json({"message": "Logged out"})