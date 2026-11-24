"""Shared Firestore client (lazy)."""

from __future__ import annotations

from typing import Any

from app.config import settings

_client: Any = None


def get_db() -> Any:
    global _client
    if _client is None:
        from google.cloud import firestore

        project = (settings.google_cloud_project or "").strip() or None
        _client = firestore.Client(project=project) if project else firestore.Client()
    return _client


def server_timestamp() -> Any:
    from google.cloud import firestore

    return firestore.SERVER_TIMESTAMP
