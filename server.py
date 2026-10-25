"""
Storage Copilot - Mobile Storage Bridge & Intelligent File Organizer
v0.6.2-live Entry Point & Backward Compatibility Shim.

All core modules have been refactored into the `storage_copilot` package.
This module re-exports all public symbols and synchronizes runtime path overrides
with `storage_copilot.config` to preserve 100% backward compatibility.
"""
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
load_environment = config.load_environment
resolve_base_storage_dir = config.resolve_base_storage_dir
format_bytes_human = config.format_bytes_human

# Database
from storage_copilot.db.ledger import get_db, init_db

# Security
from storage_copilot.security.path_resolver import resolve_safe_path, PROTECTED_ROOT_PREFIXES
from storage_copilot.security.pii_sanitizer import sanitize_pii, PII_PATTERNS

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
from storage_copilot.storage.scanner import collect_storage_files

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

# Initialize default Flask app
app = create_app()

# Backward compatibility proxy module
class _ServerCompatModule(ModuleType):
    """Dynamically synchronizes module attribute mutations with storage_copilot.config."""
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in ("BASE_DIR", "BASE_STORAGE_DIR", "TRASH_DIR", "LEDGER_DB_PATH", "GEMINI_QUOTA_BLOCKED_UNTIL"):
            setattr(config, name, value)

sys.modules[__name__].__class__ = _ServerCompatModule

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
