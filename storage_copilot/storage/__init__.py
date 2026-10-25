# storage_copilot/storage/__init__.py
from .content_extractor import extract_file_snippet
from .deduplication import calculate_file_hash, score_keeper_candidate, find_duplicate_clusters
from .collision_resolver import resolve_collision, ensure_unique_plan_destinations
from .scanner import (
    collect_storage_files,
    scan_directory_manifest,
    hydrate_candidate_snippets,
    should_hydrate_file_upfront,
    is_cryptic_filename,
    manifest_index_manager,
    MAX_HYDRATE_CANDIDATES
)

__all__ = [
    "extract_file_snippet",
    "calculate_file_hash",
    "score_keeper_candidate",
    "find_duplicate_clusters",
    "resolve_collision",
    "ensure_unique_plan_destinations",
    "collect_storage_files",
    "scan_directory_manifest",
    "hydrate_candidate_snippets",
    "should_hydrate_file_upfront",
    "is_cryptic_filename",
    "manifest_index_manager",
    "MAX_HYDRATE_CANDIDATES",
]
