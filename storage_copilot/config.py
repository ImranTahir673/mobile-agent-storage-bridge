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
# Dynamic Cross-Platform Storage Path Resolution
# ==============================================================================
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
        os.path.exists("/storage/emulated/0/Download") or 
        os.path.exists("/storage/emulated/0") or 
        os.path.exists(os.path.expanduser("~/storage/downloads")) or
        os.path.exists(os.path.expanduser("~/storage/shared"))
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

BASE_STORAGE_DIR = resolve_base_storage_dir()
BASE_DIR = BASE_STORAGE_DIR
TRASH_DIR = os.path.join(BASE_STORAGE_DIR, ".agent_trash")
LEDGER_DB_PATH = os.path.realpath(os.getenv("LEDGER_DB_PATH", os.path.join(BASE_STORAGE_DIR, ".ledger.db")))
WEB_DIR = os.path.realpath(os.path.join(REPO_ROOT, "web"))

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
