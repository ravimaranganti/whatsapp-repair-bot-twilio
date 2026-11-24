"""
Cloud Tasks helpers for deferred work.

Preferred: enqueue to Cloud Tasks → POST /tasks/* on this service (OIDC).
Pilot fallback: in-process BackgroundTasks when CLOUD_TASKS_QUEUE is empty.
Tradeoff (documented in PILOT.md): fallback needs Cloud Run CPU always allocated
(--no-cpu-throttling), which cloudbuild.yaml already sets.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


def tasks_enabled() -> bool:
    return bool((settings.cloud_tasks_queue or "").strip())


async def enqueue_http_task(
    *,
    path: str,
    payload: dict[str, Any],
    delay_seconds: int = 0,
) -> bool:
    """Enqueue an HTTP POST task. Returns False if Cloud Tasks not configured."""
    if not tasks_enabled():
        return False

    project = (settings.google_cloud_project or "").strip()
    location = settings.cloud_tasks_location or "us-central1"
    queue = settings.cloud_tasks_queue
    base = (settings.public_base_url or settings.task_handler_audience or "").rstrip(
        "/"
    )
    if not project or not base:
        logger.warning("Cloud Tasks configured but missing project/public_base_url")
        return False

    url = f"{base}{path}"
    audience = settings.task_handler_audience or base

    def _create() -> None:
        from google.cloud import tasks_v2
        from google.protobuf import timestamp_pb2

        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(project, location, queue)
        task: dict[str, Any] = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode(),
                "oidc_token": {
                    "service_account_email": f"cloud-build-deploy@{project}.iam.gserviceaccount.com",
                    "audience": audience,
                },
            }
        }
        if delay_seconds > 0:
            eta = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
            ts = timestamp_pb2.Timestamp()
            ts.FromDatetime(eta)
            task["schedule_time"] = ts
        client.create_task(request={"parent": parent, "task": task})

    import asyncio

    try:
        await asyncio.to_thread(_create)
        return True
    except Exception as e:
        logger.warning("enqueue_http_task failed: %s", e)
        return False


async def enqueue_process_message(message_dict: dict[str, Any]) -> bool:
    return await enqueue_http_task(
        path="/tasks/process-message", payload=message_dict, delay_seconds=0
    )


async def enqueue_collection_window(job_id: str) -> bool:
    delay = int(settings.collection_window_min or 60) * 60
    return await enqueue_http_task(
        path="/tasks/collection-window",
        payload={"job_id": job_id},
        delay_seconds=delay,
    )


async def enqueue_job_followup(job_id: str, delay_hours: int = 36) -> bool:
    return await enqueue_http_task(
        path="/tasks/job-followup",
        payload={"job_id": job_id},
        delay_seconds=delay_hours * 3600,
    )
