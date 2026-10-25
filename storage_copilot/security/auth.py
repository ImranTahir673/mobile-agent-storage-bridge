# storage_copilot/security/auth.py
import os
import re
import stat
import hmac
import secrets
from urllib.parse import urlparse
from typing import Optional
from flask import Request, Response, jsonify
from storage_copilot import config

# Endpoints that mutate state or sensitive configuration and require zero-trust protection
PROTECTED_MUTATION_PATHS = {
    "/execute_plan",
    "/write_file",
    "/configure_api_key",
    "/trash_file",
    "/move_file",
    "/make_directory",
    "/rollback_batch",
    "/rollback_last",
    "/seed_fixtures",
    "/user_profile",
}

# Streaming endpoints that support ?token=<token> query parameter for browser EventSource
STREAM_AUTH_PATHS = {
    "/stream",
    "/events",
}

LOOPBACK_ADDRS = {"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1", "::ffff:127.0.0.1"}

ALLOWED_STATIC_ORIGINS = {
    "file://",
    "null",
    "capacitor://localhost",
    "ionic://localhost",
    "http://localhost",
    "https://localhost",
    "http://127.0.0.1",
    "https://127.0.0.1",
}

TRUSTED_ORIGIN_PATTERNS = [
    re.compile(r"^https?://127\.0\.0\.1(?::\d+)?$", re.I),
    re.compile(r"^https?://localhost(?::\d+)?$", re.I),
    re.compile(r"^https?://\[::1\](?::\d+)?$", re.I),
    re.compile(r"^file://", re.I),
    re.compile(r"^capacitor://localhost$", re.I),
    re.compile(r"^ionic://localhost$", re.I),
]


def load_or_create_auth_token(token_file_path: Optional[str] = None) -> str:
    """
    Loads or generates the 256-bit pre-shared Bearer authentication token.
    1. Checks environment variable COPILOT_AUTH_TOKEN.
    2. Checks persistent token file at token_file_path or config.AUTH_TOKEN_PATH (~/.storage_copilot_token).
    3. Generates a new 32-byte token with 0600 permissions if absent.
    """
    env_tok = os.getenv("COPILOT_AUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok

    token_path = token_file_path or getattr(
        config, "AUTH_TOKEN_PATH", getattr(config, "AUTH_TOKEN_FILE", os.path.expanduser("~/.storage_copilot_token"))
    )
    try:
        if os.path.exists(token_path) and os.path.isfile(token_path):
            with open(token_path, "r", encoding="utf-8") as f:
                saved = f.read().strip()
            if len(saved) >= 16:
                return saved
    except Exception:
        pass

    new_token = secrets.token_hex(32)
    try:
        parent = os.path.dirname(os.path.abspath(token_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(new_token + "\n")
        try:
            os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    except Exception:
        pass
    return new_token


def get_or_create_auth_token() -> str:
    """Loads or generates the active Bearer token."""
    return load_or_create_auth_token()


def get_active_auth_token() -> str:
    """Returns the currently active auth token from config or initializes it."""
    tok = getattr(config, "AUTH_TOKEN", None)
    if not tok:
        tok = load_or_create_auth_token()
        config.AUTH_TOKEN = tok
    return tok


def is_origin_allowed(origin: Optional[str]) -> bool:
    """
    Validates browser Origin header against verified localhost, Termux WebView,
    and configured Cloudflare tunnel hostname.
    """
    if not origin:
        return True

    clean_origin = origin.strip().rstrip("/")
    if clean_origin in ALLOWED_STATIC_ORIGINS:
        return True

    for pattern in TRUSTED_ORIGIN_PATTERNS:
        if pattern.match(clean_origin):
            return True

    try:
        parsed = urlparse(clean_origin)
        host = (parsed.hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            return True

        cf_host = os.getenv("CLOUDFLARE_HOSTNAME", "").strip().lower()
        if cf_host and (host == cf_host or clean_origin.lower() in (f"https://{cf_host}", f"http://{cf_host}")):
            return True
    except Exception:
        pass

    extra_origins = os.getenv("COPILOT_ALLOWED_ORIGINS", "").strip()
    if extra_origins:
        allowed_set = {o.strip().rstrip("/").lower() for o in extra_origins.split(",") if o.strip()}
        if clean_origin.lower() in allowed_set:
            return True

    return False


def is_remote_or_tunneled_request(req: Request) -> bool:
    """
    Returns True if the request arrived via a reverse proxy / Cloudflare Tunnel
    or from a non-loopback remote IP address.
    """
    tunnel_headers = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "X-Real-Ip")
    for h in tunnel_headers:
        val = req.headers.get(h, "").strip()
        if val:
            if h == "X-Forwarded-For" and val in ("127.0.0.1", "::1", "localhost"):
                continue
            return True

    remote_ip = (req.remote_addr or "").strip().lower()
    if remote_ip.startswith("::ffff:"):
        remote_ip = remote_ip.replace("::ffff:", "")

    if remote_ip and remote_ip not in LOOPBACK_ADDRS:
        return True

    return False


def extract_request_token(req: Request, allow_query_token: bool = False) -> Optional[str]:
    """
    Extracts Bearer token from Authorization header, X-Copilot-Token header,
    or ?token= query parameter (when allow_query_token=True for SSE EventSource).
    """
    auth_header = (req.headers.get("Authorization") or "").strip()
    if auth_header:
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return auth_header

    custom_header = (req.headers.get("X-Copilot-Token") or "").strip()
    if custom_header:
        return custom_header

    if allow_query_token:
        query_tok = (req.args.get("token") or "").strip()
        if query_tok:
            return query_tok

    return None


def verify_bearer_token(candidate_token: str) -> bool:
    """Constant-time comparison against active server Bearer token."""
    if not candidate_token:
        return False
    expected = get_active_auth_token()
    return hmac.compare_digest(candidate_token.encode("utf-8"), expected.encode("utf-8"))


def requires_mutation_auth(req: Request) -> bool:
    """Determines if the current request targets a protected mutation or stream endpoint."""
    path = req.path.rstrip("/") or "/"
    if req.method.upper() in ("POST", "PUT", "DELETE", "PATCH") and path in PROTECTED_MUTATION_PATHS:
        return True
    if path in STREAM_AUTH_PATHS and req.method.upper() == "GET":
        return True
    return False


def verify_mutation_auth(req: Request) -> tuple[bool, Optional[str], int]:
    """
    Enforces Dual-Mode Zero-Trust authentication and CORS origin protection.
    Returns (is_authorized, error_message, http_status_code).
    Handles both protected mutation endpoints and SSE stream endpoints.
    """
    # 1. Always enforce Origin allowlist against Cross-Site Request Forgery (CSRF)
    origin = req.headers.get("Origin")
    if origin and not is_origin_allowed(origin):
        return False, f"CORS policy violation: Origin '{origin}' is not permitted.", 403

    if req.method.upper() == "OPTIONS":
        return True, None, 200

    path = req.path.rstrip("/") or "/"
    is_stream = path in STREAM_AUTH_PATHS and req.method.upper() == "GET"
    is_mutation = req.method.upper() in ("POST", "PUT", "DELETE", "PATCH") and path in PROTECTED_MUTATION_PATHS

    if not (is_stream or is_mutation):
        return True, None, 200

    provided_token = extract_request_token(req, allow_query_token=is_stream)

    # Rule 1: If a token is explicitly supplied, it MUST match
    if provided_token is not None:
        if not verify_bearer_token(provided_token):
            return False, "Unauthorized: Invalid Bearer token.", 401
        return True, None, 200

    # Rule 2: No token provided — check if strict auth or remote tunnel is active
    enforce_local = bool(
        getattr(config, "ENFORCE_LOCAL_AUTH", False)
        or os.getenv("COPILOT_ENFORCE_AUTH", "0").strip() in ("1", "true", "yes")
    )
    is_remote = is_remote_or_tunneled_request(req)

    if enforce_local or is_remote:
        return False, "Unauthorized: Missing Bearer token for protected endpoint.", 401

    return True, None, 200


def verify_request_security(req: Request) -> Optional[tuple[Response, int]]:
    """Flask before_request style wrapper around verify_mutation_auth."""
    is_ok, err_msg, status_code = verify_mutation_auth(req)
    if not is_ok:
        return jsonify({
            "error": err_msg,
            "status": "unauthorized" if status_code == 401 else "forbidden"
        }), status_code
    return None


def apply_cors_headers(response: Response, req: Request) -> Response:
    """
    Applies strict CORS headers:
    - Echoes back verified Origin (or defaults to http://127.0.0.1:8080 for local non-Origin requests).
    - Never outputs wildcard '*' for disallowed external origins.
    """
    origin = req.headers.get("Origin")
    if origin:
        if is_origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-Copilot-Token, X-Requested-With, Accept"
            )
    else:
        response.headers["Access-Control-Allow-Origin"] = "http://127.0.0.1:8080"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Copilot-Token, X-Requested-With, Accept"
        )
    return response
