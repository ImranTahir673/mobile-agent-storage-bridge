"""
Storage Copilot - Mobile Storage Bridge & Intelligent File Organizer
v0.6.4-live Entry Point, Production Threaded WSGI Runner & Backward Compatibility Shim.

All core modules have been refactored into the `storage_copilot` package.
This module re-exports all public symbols and synchronizes runtime path overrides
with `storage_copilot.config` to preserve 100% backward compatibility.
"""
import os
import sys
from types import ModuleType
from storage_copilot import config
from storage_copilot.app import create_app

# Core constants & paths
APP_VERSION = config.APP_VERSION
SERVER_BOOT_TIME = config.SERVER_BOOT_TIME
SERVER_BOOT_ID = config.SERVER_BOOT_ID
BASE_STORAGE_DIR = config.BASE_STORAGE_DIR
BASE_DIR = config.BASE_DIR
TRASH_DIR = config.TRASH_DIR
LEDGER_DB_PATH = config.LEDGER_DB_PATH
WEB_DIR = config.WEB_DIR
GEMINI_QUOTA_BLOCKED_UNTIL = config.GEMINI_QUOTA_BLOCKED_UNTIL
AUTH_TOKEN = getattr(config, "AUTH_TOKEN", None)
AUTH_TOKEN_FILE = getattr(config, "AUTH_TOKEN_FILE", getattr(config, "AUTH_TOKEN_PATH", os.path.expanduser("~/.storage_copilot_token")))
ENFORCE_LOCAL_AUTH = getattr(config, "ENFORCE_LOCAL_AUTH", False)
AUTH_TOKEN_PATH = AUTH_TOKEN_FILE
load_environment = config.load_environment
resolve_base_storage_dir = config.resolve_base_storage_dir
resolve_default_ledger_path = config.resolve_default_ledger_path
is_android_fuse_mount = config.is_android_fuse_mount
format_bytes_human = config.format_bytes_human

# Database
from storage_copilot.db.ledger import get_db, init_db

# Security
from storage_copilot.security.path_resolver import resolve_safe_path, PROTECTED_ROOT_PREFIXES
from storage_copilot.security.pii_sanitizer import sanitize_pii, contains_tier1_pii, PII_PATTERNS
from storage_copilot.security.auth import (
    PROTECTED_MUTATION_PATHS,
    load_or_create_auth_token,
    get_or_create_auth_token,
    get_active_auth_token,
    verify_bearer_token,
    is_origin_allowed,
    is_remote_or_tunneled_request,
    extract_request_token,
    verify_mutation_auth,
    verify_request_security,
    apply_cors_headers,
)

# Storage
from storage_copilot.storage.content_extractor import extract_file_snippet
from storage_copilot.storage.deduplication import (
    calculate_file_hash,
    score_keeper_candidate,
    find_duplicate_clusters
)
from storage_copilot.storage.collision_resolver import (
    resolve_collision,
    ensure_unique_plan_destinations
)
from storage_copilot.storage.scanner import (
    collect_storage_files,
    scan_directory_manifest,
    hydrate_candidate_snippets,
    should_hydrate_file_upfront,
    is_cryptic_filename,
    manifest_index_manager,
    MAX_HYDRATE_CANDIDATES,
)

# Intelligence
from storage_copilot.intelligence.document_classifier import (
    classify_document,
    parse_query_intent,
    SEARCH_STOPWORDS
)
from storage_copilot.intelligence.search_engine import (
    score_file_relevance,
    score_document_match,
    compute_query_relevance_score
)
from storage_copilot.intelligence.gemini_client import (
    GEMINI_MODELS,
    call_gemini_json,
    call_gemini_text,
    call_gemini_api,
    call_gemini_micro_classifier
)
from storage_copilot.intelligence.intent_classifier import (
    UserIntent,
    classify_intent
)

# Execution
from storage_copilot.execution.plan_builder import (
    extract_json_plan,
    format_plan_badges,
    normalize_user_prompt
)
from storage_copilot.execution.executor import (
    execute_action_plan,
    execute_plan_single
)
from storage_copilot.execution.rollback import _rollback_internal
from storage_copilot.execution.event_bus import (
    EventBus,
    event_bus,
    publish_event,
    format_sse_frame
)
from storage_copilot.execution.tier_handlers import (
    handle_conversation_turn,
    handle_query_info_turn,
    handle_file_operation_turn
)

# Advice
from storage_copilot.advice.advisor import (
    FORMAT_CATEGORIES,
    get_format_category,
    get_unorganized_download_files,
    is_format_sorting_query,
    generate_dynamic_storage_advice
)

# API Route Handlers
from storage_copilot.api.core_routes import health, health_check, serve_static, api_status, configure_api_key
from storage_copilot.api.file_routes import list_files, read_file_snippet, write_file, make_directory, move_file, trash_file
from storage_copilot.api.history_routes import lookup_history, rollback_batch, rollback_last
from storage_copilot.api.profile_routes import get_user_profile_path, get_user_profile, update_user_profile
from storage_copilot.api.dedup_routes import scan_duplicates
from storage_copilot.api.utility_routes import seed_fixtures
from storage_copilot.api.plan_routes import execute_plan, get_recent_batch, propose_plan
from storage_copilot.api.stream_routes import stream_events

# Initialize default Flask app
app = create_app()


class _ServerCompatModule(ModuleType):
    """Dynamically synchronizes module attribute mutations with storage_copilot.config."""
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in (
            "BASE_DIR",
            "BASE_STORAGE_DIR",
            "TRASH_DIR",
            "LEDGER_DB_PATH",
            "GEMINI_QUOTA_BLOCKED_UNTIL",
            "AUTH_TOKEN",
            "AUTH_TOKEN_FILE",
            "ENFORCE_LOCAL_AUTH",
            "AUTH_TOKEN_PATH",
        ):
            setattr(config, name, value)


sys.modules[__name__].__class__ = _ServerCompatModule


def run_production_server(host: str = "0.0.0.0", port: int = 8080, threads: int = 8):
    """
    Phase 4 Production WSGI Runner:
    - Warms up the background directory manifest index in a non-blocking daemon thread.
    - Prefers `waitress` (1 process, 8 threads) so SSE connections, `_SNIPPET_CACHE`,
      and `EventBus` share the same process memory space without blocking.
    - Falls back to Flask's multi-threaded server (`threaded=True`).
    """
    manifest_index_manager.warm_up_async("Download")
    try:
        from waitress import serve
        print(f"[Storage Copilot] Starting Waitress WSGI server on {host}:{port} (threads={threads})...")
        serve(app, host=host, port=port, threads=threads)
    except ImportError:
        print(f"[Storage Copilot] Starting multi-threaded Flask server on {host}:{port} (threaded=True)...")
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    listen_port = int(os.getenv("PORT", "8080"))
    run_production_server(host="0.0.0.0", port=listen_port, threads=8)
