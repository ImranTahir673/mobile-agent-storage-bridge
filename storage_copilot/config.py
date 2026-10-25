# storage_copilot/config.py
import os
import time

APP_VERSION = "v0.6.2-live"
SERVER_BOOT_TIME = int(time.time())
SERVER_BOOT_ID = f"build-v0.6.2-{SERVER_BOOT_TIME}"
GEMINI_QUOTA_BLOCKED_UNTIL = 0.0

# Repository Root Directory
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==============================================================================
# Environment Configuration (.env loader)
# ==============================================================================
def load_environment():
    """Loads environment variables from .env in repo root or current directory."""
    candidates = [
        os.path.join(REPO_ROOT, ".env"),
        os.path.join(os.getcwd(), ".env")
    ]
    for env_path in candidates:
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_environment()


# ==============================================================================
# Dynamic Cross-Platform Storage & FUSE-Safe Ledger Path Resolution
# ==============================================================================
def is_android_fuse_mount(path: str) -> bool:
    """
    Returns True if the given storage path resides on an Android emulated FUSE / sdcardfs
    mount (/storage/emulated/0, /sdcard, or ~/storage/shared) where SQLite WAL shared-memory
    mmap files (-shm) are prone to locking or corruption.
    """
    norm = (path or "").replace("\\", "/").rstrip("/")
    if norm.startswith("/storage/emulated/") or norm.startswith("/sdcard"):
        return True
    termux_shared = os.path.expanduser("~/storage/shared").replace("\\", "/").rstrip("/")
    if norm == termux_shared or (os.path.exists(termux_shared) and norm == os.path.realpath(termux_shared).replace("\\", "/")):
        return True
    return False


def resolve_base_storage_dir() -> str:
    """
    Dynamically detects and configures the storage base directory across platforms:
    - Android Termux: checks /storage/emulated/0/Download or ~/storage/downloads.
      Sets /storage/emulated/0 as BASE_STORAGE_DIR.
    - Windows / Linux fallback: local downloads/ or ./fixtures or ~/storage/shared.
    """
    env_dir = os.getenv("STORAGE_BASE_DIR")
    if env_dir:
        target = os.path.realpath(env_dir)
        os.makedirs(target, exist_ok=True)
        return target

    # Android Termux check
    is_android = (
        os.path.exists("/storage/emulated/0/Download")
        or os.path.exists("/storage/emulated/0")
        or os.path.exists(os.path.expanduser("~/storage/downloads"))
        or os.path.exists(os.path.expanduser("~/storage/shared"))
    )
    if is_android:
        if os.path.exists("/storage/emulated/0"):
            return "/storage/emulated/0"
        termux_shared = os.path.expanduser("~/storage/shared")
        if os.path.exists(termux_shared):
            return os.path.realpath(termux_shared)

    # Windows / Linux local fallbacks: check ./fixtures or ./downloads
    candidates = [
        os.path.join(REPO_ROOT, "fixtures"),
        os.path.join(REPO_ROOT, "downloads"),
        os.path.join(REPO_ROOT, "Download"),
        os.path.expanduser("~/storage/shared"),
        os.path.expanduser("~/Downloads")
    ]
    for cand in candidates:
        if os.path.exists(cand) and os.path.isdir(cand):
            return os.path.realpath(cand)

    # Default fallback: create local fixtures/ in repository
    fallback_dir = os.path.join(REPO_ROOT, "fixtures")
    os.makedirs(fallback_dir, exist_ok=True)
    return os.path.realpath(fallback_dir)


def resolve_default_ledger_path(base_storage_dir: str) -> str:
    """
    Phase 3 FUSE-Safe SQLite WAL Ledger Resolution:
    - Honors explicit LEDGER_DB_PATH environment variable if set (used by test suites).
    - If base_storage_dir is an Android emulated FUSE mount (/storage/emulated/0),
      relocates the default SQLite WAL ledger to Termux app-private POSIX storage
      ($HOME/.storage_copilot_ledger.db) to eliminate FUSE mmap corruption.
    - Otherwise defaults to <base_storage_dir>/.ledger.db.
    """
    env_ledger = os.getenv("LEDGER_DB_PATH", "").strip()
    if env_ledger:
        return os.path.realpath(env_ledger)

    if is_android_fuse_mount(base_storage_dir):
        home_dir = os.path.expanduser("~")
        return os.path.realpath(os.path.join(home_dir, ".storage_copilot_ledger.db"))

    return os.path.realpath(os.path.join(base_storage_dir, ".ledger.db"))


BASE_STORAGE_DIR = resolve_base_storage_dir()
BASE_DIR = BASE_STORAGE_DIR
TRASH_DIR = os.path.join(BASE_STORAGE_DIR, ".agent_trash")
LEDGER_DB_PATH = resolve_default_ledger_path(BASE_STORAGE_DIR)
WEB_DIR = os.path.realpath(os.path.join(REPO_ROOT, "web"))

# Phase 3 Security Configuration
AUTH_TOKEN_FILE = os.path.realpath(
    os.getenv("COPILOT_TOKEN_FILE", os.path.expanduser("~/.storage_copilot_token"))
)
AUTH_TOKEN = os.getenv("COPILOT_AUTH_TOKEN", "").strip()
ENFORCE_LOCAL_AUTH = os.getenv("COPILOT_ENFORCE_AUTH", "0").strip() in ("1", "true", "yes")


def init_storage_dirs():
    """Initializes baseline storage directories."""
    os.makedirs(BASE_STORAGE_DIR, exist_ok=True)
    os.makedirs(TRASH_DIR, exist_ok=True)
    os.makedirs(WEB_DIR, exist_ok=True)
    os.makedirs(os.path.join(BASE_STORAGE_DIR, "Download"), exist_ok=True)
    os.makedirs(os.path.join(BASE_STORAGE_DIR, "Documents"), exist_ok=True)


init_storage_dirs()


def format_bytes_human(size_bytes: int) -> str:
    """Formats raw byte counts into human-readable strings (KB, MB, GB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"