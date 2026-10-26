import time
import threading
from typing import Dict, Any, Optional


class SessionContextStore:
    """
    Phase 9 Conversational Continuity Ledger.
    Preserves structured state across multi-turn interactions (e.g., remembering that
    the previous turn audited 67 near-duplicates with include_near=True, or audited
    forgotten files with stale_days=90) so follow-ups like 'Yes clean them' or
    'Yes clean all the duplicate files' execute with full context.
    """
    def __init__(self, ttl_seconds: float = 1800.0):
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._state: Dict[str, Dict[str, Any]] = {}

    def _default_entry(self, target_folder: str) -> Dict[str, Any]:
        return {
            "target_folder": target_folder,
            "last_intent": None,
            "last_sub_intent": None,
            "last_audit_type": None,
            "include_near": False,
            "exact_duplicates_count": 0,
            "near_duplicates_count": 0,
            "forgotten_files_count": 0,
            "stale_days": 90.0,
            "unorganized_count": 0,
            "updated_at": time.time(),
        }

    def get_context(self, target_folder: str = "Download") -> Dict[str, Any]:
        key = (target_folder or "Download").strip().lower()
        now = time.time()
        with self._lock:
            entry = self._state.get(key)
            if not entry or (now - entry.get("updated_at", 0.0)) > self._ttl:
                fresh = self._default_entry(target_folder)
                self._state[key] = fresh
                return dict(fresh)
            return dict(entry)

    def update_context(self, target_folder: str = "Download", **kwargs) -> Dict[str, Any]:
        key = (target_folder or "Download").strip().lower()
        with self._lock:
            entry = self._state.get(key)
            if not entry:
                entry = self._default_entry(target_folder)
            entry.update(kwargs)
            entry["updated_at"] = time.time()
            self._state[key] = entry
            return dict(entry)

    def record_duplicate_audit(
        self,
        target_folder: str,
        dup_res: Dict[str, Any],
        include_near_requested: bool = False
    ) -> Dict[str, Any]:
        exact_cnt = int(dup_res.get("exact_clusters_count", 0))
        near_cnt = int(dup_res.get("near_clusters_count", 0))
        total_dups = int(dup_res.get("total_duplicates", 0))
        has_near = bool(include_near_requested or near_cnt > 0)
        return self.update_context(
            target_folder=target_folder,
            last_intent="QUERY_INFO",
            last_sub_intent="deduplicate_near_audit" if has_near else "deduplicate_audit",
            last_audit_type="near_duplicates" if has_near else "exact_duplicates",
            include_near=has_near,
            exact_duplicates_count=exact_cnt,
            near_duplicates_count=near_cnt,
            total_duplicates_count=total_dups,
        )

    def record_forgotten_audit(
        self,
        target_folder: str,
        forg_res: Dict[str, Any],
        stale_days: float = 90.0
    ) -> Dict[str, Any]:
        return self.update_context(
            target_folder=target_folder,
            last_intent="QUERY_INFO",
            last_sub_intent="forgotten_audit",
            last_audit_type="forgotten_files",
            forgotten_files_count=int(forg_res.get("total_forgotten", 0)),
            stale_days=stale_days,
        )

    def clear(self, target_folder: Optional[str] = None) -> None:
        with self._lock:
            if target_folder:
                self._state.pop(target_folder.strip().lower(), None)
            else:
                self._state.clear()


# Global singleton SessionContextStore
session_context_store = SessionContextStore()
