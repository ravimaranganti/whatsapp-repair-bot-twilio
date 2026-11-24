# Pilot operations notes

Implements `PILOT_SPEC.md` (Phases 0–5). Feature flags stay **off** until you enable them.

## Background processing tradeoff

- **Preferred:** set `CLOUD_TASKS_QUEUE` + `PUBLIC_BASE_URL` and create a Cloud Tasks queue that POSTs to `/tasks/process-message` with OIDC.
- **Pilot fallback (current default):** FastAPI `BackgroundTasks` when the queue env is empty.
  - Requires Cloud Run **CPU always allocated** (`--no-cpu-throttling` — already in `cloudbuild.yaml`).
  - Work can be lost if the instance is killed mid-task; fine for a friend pilot.

## Env vars (new)

| Var | Purpose |
|-----|---------|
| `PUBLIC_BASE_URL` | Exact HTTPS origin Twilio calls (signature validation) |
| `PILOT_ALLOWLIST` | Comma-separated E.164; empty = allow all |
| `PRO_JOIN_CODE` | Optional `JOIN <code>` for pros (bypasses allowlist) |
| `DISPATCH_ENABLED` | `true` to broker jobs to opted-in pros |
| `FEEDBACK_ENABLED` | DIY/job ratings + FEEDBACK keyword + digest |
| `MAX_PROS_PER_JOB` | default 4 |
| `COLLECTION_WINDOW_MIN` | default 60 |
| `TPL_JOB_ALERT_SID` | Content SID for pro job alert |
| `TPL_JOB_NOT_SELECTED_SID` | Not-selected template |
| `TPL_JOB_FOLLOWUP_SID` | Post-job rating template |
| `TPL_JOB_REOPEN_SID` | Optional reopen |
| `OPERATOR_PHONE` | Weekly digest recipient |
| `SKIP_TWILIO_SIGNATURE` | `true` only for local curl tests |
| `CLOUD_TASKS_QUEUE` | Optional queue id |

## Twilio Content Templates (submit early — Utility)

1. `job_alert` — "New {{1}} request near {{2}}: {{3}}. Severity: {{4}}. Interested?" Buttons Interested / Not available. Prefer button ids `int:{{job}}` / `dec:{{job}}` if dynamic ids are allowed; otherwise we resolve via `pros.pending_alert_job_ids`.
2. `job_not_selected` — thanks + chose another pro.
3. `job_followup` — rate the job.
4. `job_reopen` (optional).

Store SIDs in the `TPL_*` env vars / Secret Manager.

## Seed pros

```bash
# After deploy, once:
curl -X POST https://YOUR_SERVICE/admin/seed-pros
# Or: PYTHONPATH=. python scripts/seed_pros.py
```

Seeds from `professionals.json` with `opted_in=False`. Pros must `JOIN` (and complete consent) before receiving alerts.

## Enable dispatch (checklist)

1. Seed pros; have friends JOIN and opt in with real ZIPs.
2. Submit/approve templates; set SIDs.
3. Set `PILOT_ALLOWLIST` to friend phones.
4. Set `PUBLIC_BASE_URL` to the Cloud Run URL.
5. Set `DISPATCH_ENABLED=true` (and optionally `FEEDBACK_ENABLED=true`).
6. Configure Cloud Scheduler → `POST /tasks/weekly-digest` (Mondays).
7. Optional: Cloud Tasks queue for inbound + collection window.

## Firestore TTL

Create a TTL policy on `processed_messages.expire_at` (~3 days) in the Console.
