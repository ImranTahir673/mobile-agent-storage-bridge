# storage_copilot/security/auth.py
import os
import re
import stat
import hmac
import secrets
from typing import Optional
from flask import Request
from storage_copilot import config

PROTECTED_MUTATION_PATHS = {
    "/execute_plan",
    "/write_file",
    "/configure_api_key",
    "/trash_file",
    "/move_file",
    "/make_directory",
    "/rollback_batch",
    "/rollback_last",
    "/user_profile",
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
    Loads the pre-shared Bearer token from environment or ~/.storage_copilot_token.
    Generates a cryptographically strong 256-bit token (chmod 600) if none exists.
    """
    env_token = os.getenv("COPILOT_AUTH_TOKEN", "").strip()
    if env_token:
        return env_token

    path = token_file_path or getattr(
        config, "AUTH_TOKEN_FILE", os.path.expanduser("~/.storage_copilot_token")
    )
    try:
        if os.path.exists(path) and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            if len(existing) >= 16:
                return existing
    except Exception:
        pass

    new_token = secrets.token_urlsafe(32)
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_token + "\n")
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    except Exception:
        pass

    return new_token


def get_active_auth_token() -> str:
    """Returns the currently active auth token from config or initializes it."""
    tok = getattr(config, "AUTH_TOKEN", None)
    if not tok:
        tok = load_or_create_auth_token()
        config.AUTH_TOKEN = tok
    return tok


def is_origin_allowed(origin: Optional[str]) -> bool:
    """
    Validates a browser Origin header against verified localhost, Termux WebView,
    and configured Cloudflare tunnel hostnames.
    If no Origin header is present (same-origin or CLI client), returns True.
    """
    if not origin:
        return True

    clean_origin = origin.strip().rstrip("/")
    if clean_origin == "null":
        # Local file:// or sandboxed WebView iframe
        return True

    for pattern in TRUSTED_ORIGIN_PATTERNS:
        if pattern.match(clean_origin):
            return True

    # Check configured Cloudflare tunnel hostname if set
    cf_host = os.getenv("CLOUDFLARE_HOSTNAME", "").strip().lower()
    if cf_host:
        if clean_origin.lower() in (f"https://{cf_host}", f"http://{cf_host}"):
            return True

    extra_origins = os.getenv("COPILOT_ALLOWED_ORIGINS", "").strip()
    if extra_origins:
        allowed_set = {o.strip().rstrip("/").lower() for o in extra_origins.split(",") if o.strip()}
        if clean_origin.lower() in allowed_set:
            return True

    return False


def is_remote_or_tunneled_request(req: Request) -> bool:
    """
    Detects whether an incoming HTTP request arrived over a remote interface
    or via a Cloudflare / reverse-proxy tunnel rather than direct local loopback.
    """
    tunnel_headers = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "X-Real-Ip")
    for h in tunnel_headers:
        val = req.headers.get(h, "").strip()
        if val:
            # If X-Forwarded-For is purely loopback, don't treat as remote unless Cf-Ray/Cf-Connecting-Ip is set
            if h == "X-Forwarded-For" and val in ("127.0.0.1", "::1", "localhost"):
                continue
            return True

    remote_ip = (req.remote_addr or "").strip()
    if remote_ip and remote_ip not in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
        return True

    return False


def extract_request_token(req: Request) -> Optional[str]:
    """Extracts Bearer token from Authorization header or X-Copilot-Token header."""
    auth_header = req.headers.get("Authorization", "").strip()
    if auth_header:
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return auth_header

    custom_header = req.headers.get("X-Copilot-Token", "").strip()
    if custom_header:
        return custom_header

    return None


def verify_mutation_auth(req: Request) -> tuple[bool, Optional[str], int]:
    """
    Enforces Dual-Mode Zero-Trust authentication and CORS origin protection.
    Returns (is_authorized, error_message, http_status_code).
    """
    # 1. Always enforce Origin allowlist against Cross-Site Request Forgery (CSRF)
    origin = req.headers.get("Origin")
    if origin and not is_origin_allowed(origin):
        return False, f"Forbidden cross-origin request from untrusted origin: {origin}", 403

    # 2. Check if endpoint is a protected state-mutating route
    path = req.path.rstrip("/") or "/"
    is_protected_mutation = (
        req.method in ("POST", "PUT", "DELETE", "PATCH")
        and path in PROTECTED_MUTATION_PATHS
    )

    if not is_protected_mutation:
        return True, None, 200

    expected_token = get_active_auth_token()
    provided_token = extract_request_token(req)

    # 3. If a token is explicitly supplied, it MUST match (even on localhost)
    if provided_token is not None:
        if not provided_token or not hmac.compare_digest(provided_token, expected_token):
            return False, "Unauthorized: Invalid Bearer token", 401
        return True, None, 200

    # 4. If no token is supplied, check if strict auth is required
    enforce_local = bool(
        getattr(config, "ENFORCE_LOCAL_AUTH", False)
        or os.getenv("COPILOT_ENFORCE_AUTH", "0").strip() in ("1", "true", "yes")
    )
    is_remote = is_remote_or_tunneled_request(req)

    if enforce_local or is_remote:
        return (
            False,
            "Unauthorized: Missing Bearer token for protected mutation endpoint",
            401,
        )

    # 5. Direct local loopback with no foreign Origin and ENFORCE_LOCAL_AUTH=False -> allowed
    return True, None, 200