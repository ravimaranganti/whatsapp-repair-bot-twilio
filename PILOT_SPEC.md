# Pilot Upgrade Spec — Houston Home Repair AI (WhatsApp)

> **For Cursor:** This spec upgrades the existing repo `whatsapp-repair-bot-twilio` (FastAPI + Twilio WhatsApp + Gemini `gemini-2.5-flash` + Firestore + Vertex Memory Bank, deployed on Cloud Run). Implement **one phase at a time**, in order. Each phase must keep the bot deployable and working. Do not rewrite working code that a phase doesn't touch. Add unit tests for new logic (state machine, parsing, matching). Ask before adding new GCP services beyond those named here. Where this spec describes Twilio or Gemini API details, **verify against current official docs** before coding; if something differs, follow the docs and leave a comment.

---

## Goal of the pilot

Move from "bot recommends pros from a static list and shows their numbers" to a **brokered, fully automated flow**:

1. Homeowner sends a photo/description → bot diagnoses it.
2. If DIY is safe → bot gives steps and asks if it helped.
3. If a pro is needed → bot asks permission, then sends a WhatsApp job alert to 3–5 **opted-in** matching pros.
4. Pros tap Interested / Not available, then give availability + a rough estimate.
5. Bot shows the homeowner the options as tappable buttons.
6. Homeowner taps one → bot exchanges contact details between the two, tells the other pros the job is filled.
7. A day later, bot asks both sides for a rating. All feedback is stored and summarized weekly.

Pilot audience: a few friends (homeowners) and a few pros, all known to the owner. No manual steps by the operator.

---

## Phase 0 — Reliability and safety foundations

These fix risks in the current architecture and must come first.

1. **Acknowledge Twilio immediately.** Twilio's webhook times out quickly (~15s) and may retry, but Gemini calls currently allow up to 50s. Change `/webhook` to validate, dedupe, enqueue, and return `200` with empty TwiML right away. Do the real work in a background worker:
   - Preferred: **Cloud Tasks** queue → internal endpoint `/tasks/process-message` on the same Cloud Run service (authenticated with OIDC).
   - Acceptable for pilot: FastAPI background task **only if** Cloud Run CPU is set to "always allocated"; document the tradeoff.
   - All outbound replies are sent via the Twilio REST API, not TwiML.
2. **Validate Twilio signatures** on `/webhook` using `X-Twilio-Signature` and the auth token. Reject invalid requests with 403.
3. **Idempotency:** store processed `MessageSid` values in Firestore (`processed_messages/{MessageSid}` with TTL ~3 days). Skip duplicates.
4. **Pilot allowlist:** env var `PILOT_ALLOWLIST` (comma-separated E.164 numbers). Non-allowlisted senders get one polite "this is a private pilot" message and nothing else. Pros joining via `JOIN` must also be on the allowlist (or use a join code, see Phase 3).
5. **Feature flags (env vars):** `DISPATCH_ENABLED` (pro job alerts on/off), `FEEDBACK_ENABLED`. Default off until their phase ships.
6. **Event log:** add `events` collection. Write one document for every inbound message, outbound message, Gemini call (model, latency, parsed output, errors), state transition, and button tap. Fields: `ts`, `phone`, `role` (homeowner/pro/system), `type`, `job_id` (nullable), `payload`. This is the primary pilot-analysis data.

---

## Phase 1 — Prompt and diagnosis rewrite

### 1a. Shared persona
Create `prompts/persona.py` with one `BASE_PERSONA` used by **both** the image and chat paths, passed via `system_instruction` in `types.GenerateContentConfig` (not concatenated into user content).

Persona content (write it in this spirit):
- Name: **Houston Home Repair AI**. Friendly, practical, like a knowledgeable neighbor. Plain English, no jargon unless explaining it.
- Covers plumbing, HVAC, electrical, handyman, and common appliance issues equally.
- WhatsApp style: short paragraphs, minimal formatting (WhatsApp uses `*bold*`, not `**bold**`), no walls of text.
- Honest about uncertainty; asks one clarifying question rather than guessing when a photo is ambiguous.
- Safety first: never give DIY steps for gas, electrical panel/wiring work, active flooding near electricity, sewage backups, structural damage, or anything requiring permits.
- Never invents prices, pro details, or facts not in provided context.

### 1b. Image diagnosis → structured JSON
Replace the free-text format and `**PRO BRIEF:**` string splitting with **Gemini structured output** (`response_mime_type="application/json"` + `response_schema` as a Pydantic model). Schema:

```python
class Trade(str, Enum): plumbing, hvac, electrical, handyman, appliance, other, not_home_issue
class Severity(str, Enum): low, medium, high, emergency
class SafetyFlag(str, Enum): gas, electrical_hazard, active_flooding, sewage, mold, structural, fire_risk, carbon_monoxide

class ProBrief(BaseModel):
    issue: str            # 1 sentence
    likely_cause: str     # 1 sentence
    parts_needed: list[str]   # max 5
    est_time: str         # e.g. "1-2 hours"
    notes: str | None

class Diagnosis(BaseModel):
    trade: Trade
    severity: Severity
    safety_flags: list[SafetyFlag]
    confidence: Literal["low","medium","high"]
    diy_appropriate: bool
    homeowner_message: str        # <= 500 chars, plain English, what it is + immediate safety advice
    diy_steps: list[str]          # empty unless diy_appropriate; max 5 short steps
    follow_up_question: str | None  # ask when confidence is low
    pro_brief: ProBrief
```

Prompt instructions for the image mode (appended to persona):
- Diagnose from the photo plus caption plus provided conversation context.
- Set `diy_appropriate` true only for low-risk fixes a typical homeowner can do with basic tools (e.g., running toilet flapper, clogged aerator, tripped GFCI reset, dirty HVAC filter).
- If confidence is low, still fill fields with best guess and set `follow_up_question`.
- If the image is not a home issue, use `trade=not_home_issue` and a friendly `homeowner_message`.
- Include 2–3 few-shot examples (as text descriptions of photos with expected JSON) covering: a DIY plumbing fix, an electrical hazard, and an ambiguous photo.

**Code-level safety overrides (do not trust the model alone):** if `safety_flags` contains `gas`, `carbon_monoxide`, `electrical_hazard`, `active_flooding`, `sewage`, `structural`, or `fire_risk`, force `diy_appropriate=False` and clear `diy_steps`. For `gas` / `carbon_monoxide` / `fire_risk`, prepend a hardcoded emergency message: leave the area, don't operate switches, call 911 / the gas utility from outside.

On JSON parse failure: retry once; if it fails again, send a generic "couldn't analyze, please try another photo" message and log the error.

Remove the keyword-based profession classification; use `trade` from the schema.

### 1c. Give the image path context
Inject the same context the chat path gets: last ~10 Firestore turns and the top Memory Bank facts. This makes follow-up photos ("here's another angle") work.

### 1d. Chat mode — remove the directory dump, add tools
- **Delete** the embedded `professionals.json` dump from `CHAT_SYSTEM`.
- Use **Gemini function calling** with these tools (implemented in Python, reading Firestore):
  - `search_pros(trade, area)` → returns pro display names, trade, rating, general availability. **No phone numbers** (contacts are only exchanged after a homeowner picks a pro in the job flow).
  - `request_quotes(job_summary)` → starts the Phase 4 dispatch flow for this homeowner (requires an existing diagnosis or enough text detail; otherwise the model should ask for a photo).
  - `get_job_status()` → current job state for this homeowner.
  - `save_service_note(note)` → see 1e.
- Chat history: pass prior turns as proper multi-turn `contents` (user/model roles) rather than one flat string.
- Chat mode instructions: answer repair questions, use tools when the user asks about pros/quotes/status, ask for a photo when visual diagnosis is needed, keep replies 2–4 sentences.

### 1e. Service notes (replaces the "remember my number" problem)
- Add an explicit `homeowners/{phone}.service_notes` list (text, created_at). Don't rely on Memory Bank for these.
- When the user asks to remember something for a home visit (gate code, unit number, parking, pets, preferred times), the model calls `save_service_note`, then confirms briefly.
- Service notes are injected into prompts and **shared with a pro only after the homeowner picks that pro** (Phase 4), and only after the bot asks "Share your saved access notes with Mike?" (Yes/No buttons).
- Memory Bank remains for general long-term context. Add a prompt line: if facts conflict, prefer the most recent conversation turn; treat Memory Bank facts as possibly stale.

---

## Phase 2 — Data model (Firestore)

```
homeowners/{phone}
  name, area_zip, neighborhood, service_notes[], created_at, active_job_id

pros/{phone}
  name, business_name, trades[], service_zips[], availability_text,
  rate_text, opted_in (bool), opted_in_at, status (active|paused),
  rating_avg, rating_count, response_stats{alerts_sent, responses, avg_response_min}

jobs/{job_id}
  homeowner_phone, trade, severity, diagnosis (Diagnosis JSON), photo_url,
  area_zip, state, created_at, updated_at, expires_at,
  alerted_pro_phones[], selected_pro_phone, followup_sent

jobs/{job_id}/responses/{pro_phone}
  status (alerted|interested|declined|details_given|selected|not_selected),
  availability_text, estimate_text, estimate_low, estimate_high,
  responded_at

feedback/{id}
  job_id, phone, role, kind (diy_helpful|job_rating|freeform), rating, text, ts

conversation_state/{phone}
  mode (idle|awaiting_area|awaiting_dispatch_consent|awaiting_pick|
        pro_onboarding_step_N|pro_awaiting_details|awaiting_feedback),
  context (job_id etc.), updated_at
```

Seed `pros` from the existing `professionals.json` with `opted_in=False`. **Only `opted_in=True` pros may ever receive job alerts** (WhatsApp policy requires opt-in for business-initiated messages).

Implement the job lifecycle as an explicit state machine in `services/jobs.py` with allowed transitions and unit tests:

```
diagnosed → awaiting_consent → dispatched → awaiting_pick → assigned → completed
                    ↘ cancelled        ↘ expired (no responses)   ↘ cancelled
```

`conversation_state` decides how to interpret the next inbound message (e.g., a "1" means "pick option 1" only when `mode=awaiting_pick`).

---

## Phase 3 — Automated pro onboarding

- A pro sends `JOIN` (or `JOIN <code>` where `PRO_JOIN_CODE` is an env var, so the allowlist isn't needed for pros).
- Bot walks them through, one question at a time, using interactive messages where possible:
  1. Name / business name (free text)
  2. Trade(s) (list picker: Plumbing, HVAC, Electrical, Handyman, Appliance)
  3. Service ZIP codes (free text; parse with simple regex for 5-digit ZIPs)
  4. Typical availability (list picker: Same day, Next day, Within a week)
  5. Rate or visit fee (free text)
  6. Consent: "You'll receive job alerts on WhatsApp. Reply STOP anytime." (Yes / No buttons)
- On Yes → `opted_in=True`. Confirm with a summary.
- Support `STOP` (sets `opted_in=False`), `PAUSE` / `RESUME`, and `PROFILE` (shows their saved info).
- All of this happens inside the 24-hour window because the pro messaged first, so no templates are needed here.

---

## Phase 4 — Brokered job dispatch and pick

### Homeowner side
1. After a diagnosis where `diy_appropriate=False` (or the user taps "Still stuck" after DIY), if `homeowners.area_zip` is missing, ask for their ZIP once and save it.
2. Ask consent: "Want me to ask 3–5 local {trade} pros for availability and a rough estimate? Your number stays private until you choose one." Buttons: **Yes, find pros** / **No thanks**.
3. On Yes → create job, select pros, send alerts, reply: "Sent to N pros. I'll message you as responses come in (usually within an hour)."

### Pro selection
- Filter: `opted_in=True`, `status=active`, `trade` match, `area_zip in service_zips`.
- Rank by severity: high/emergency → fastest availability first; medium → rating then availability; low → rating then rate. Keep the existing deterministic sort logic but read from Firestore.
- Cap at `MAX_PROS_PER_JOB` (default 4). If zero matches, tell the homeowner honestly and log it.

### Pro job alert (business-initiated → requires an approved template)
- Send via a **Twilio Content Template** (approved by WhatsApp, category Utility) with quick-reply buttons **Interested** / **Not available**. Body variables: trade, short issue summary (from `pro_brief.issue`), severity, neighborhood/ZIP. **Never include homeowner name, number, or address.**
- Include the job photo if the template type supports media; otherwise omit it.
- Button payloads must identify the job, e.g. `int:{job_id}` / `dec:{job_id}`. Verify in Twilio docs how dynamic payloads work in Content Templates; if payloads can't be dynamic, store `pros/{phone}.pending_alert_job_ids` and resolve the tap to the most recent open alert.
- Inbound button taps arrive on the webhook with fields like `ButtonPayload` / `ButtonText` (and list-picker selections as their own fields). Confirm exact field names in Twilio docs and handle them in one parser: `parse_interaction(form) -> Interaction(kind, payload, text)`.

### Pro response
- **Not available** → mark declined, thank them.
- **Interested** → pro is now in the 24-hour window. Ask: "When can you come, and what's a rough estimate or visit fee?" Accept free text; extract `availability_text`, `estimate_low`, `estimate_high` with a small Gemini structured-output call. Confirm back to the pro what was captured.
- First 3 complete responses (or when the collection window closes) trigger the homeowner options message.

### Homeowner options + pick
- Send options in the body text, numbered, with name, rating, availability, estimate.
- Use quick-reply buttons (max 3) or a list picker (if >3), with payloads `pick:{job_id}:{pro_phone_hash}`. These are sent inside the homeowner's 24-hour window, so they don't need WhatsApp approval, but they still need to be created as Twilio Content resources; verify this in Twilio docs.
- Also accept typed `1` / `2` / `3` or a name when `conversation_state.mode=awaiting_pick`; use Gemini only as a fallback to map messy text to an option.
- On pick:
  1. Validate: job is `awaiting_pick` and the tapper is the job's homeowner. Ignore duplicate/late taps.
  2. Transition to `assigned`, record `selected_pro_phone`.
  3. Send the homeowner the pro's name and number.
  4. Send the pro the homeowner's first name, number, and the job summary. If the homeowner has service notes, first ask the homeowner whether to share them.
  5. Send other interested pros "The homeowner chose another pro this time. Thanks for responding!" (template if outside their window).
  6. Clear `conversation_state`.

### If the homeowner has an active job
Only one active job per homeowner for the pilot. New photos during an active job get diagnosed normally, but before dispatching again ask whether this is a new issue.

---

## Phase 5 — Timers, follow-ups, feedback

Cloud Run doesn't run timers itself. Use **Cloud Tasks** (scheduled tasks with `schedule_time`) for per-job timers, and **Cloud Scheduler** for periodic jobs.

Per-job timers:
- `COLLECTION_WINDOW_MIN` (default 60): when it fires, if ≥1 response → send options; if 0 → tell homeowner no one was available yet, offer to retry later or widen the search; mark `expired`.
- Pick reminder after 12h if still `awaiting_pick` (only if inside the homeowner's 24h window; otherwise skip).
- Follow-up 24–48h after `assigned`: template to both sides: "How did it go?" with rating buttons (1–5 via list picker or Good / Okay / Bad quick replies) and an optional comment. Ratings update `pros.rating_avg`.

Feedback capture (requires `FEEDBACK_ENABLED`):
- After DIY steps: buttons **That fixed it** / **Didn't help** / **Get a pro** (the last one routes into Phase 4).
- Keyword `FEEDBACK` (any time, any role): next message (text or voice note) is stored as freeform feedback. Voice notes: store the media URL and transcribe with Gemini.
- Weekly digest (Cloud Scheduler, Monday morning): query the last 7 days of `feedback`, `jobs`, and `events`; have Gemini produce a short summary (volume, DIY vs. pro split, DIY helpful rate, pro response rate and median response time, ratings, top complaints, notable misdiagnoses); send it to `OPERATOR_PHONE` on WhatsApp and store it in `digests/`.

---

## Templates to create and submit (Twilio Content Template Builder → WhatsApp approval)

Keep these to Utility category, plain and specific. Submit early; approval can take time and wording gets rejected.

1. `job_alert` — "New {{trade}} request near {{zip}}: {{issue}}. Severity: {{severity}}. Interested?" Buttons: Interested / Not available.
2. `job_not_selected` — "Thanks for responding to the {{trade}} request near {{zip}}. The homeowner chose another pro this time."
3. `job_followup` — "How did the {{trade}} job on {{date}} go? Tap to rate." Buttons for rating.
4. `job_reopen` (optional) — to reach a homeowner outside their 24h window when pro responses arrive late.

Document every template's Content SID as an env var (`TPL_JOB_ALERT_SID`, etc.).

---

## Non-goals for the pilot
- Payments, billing, or commissions.
- Business verification (the unverified 250 unique users/day limit is fine).
- A web admin dashboard (use the Firestore console + weekly digest).
- Multi-city support.

## Definition of done (pilot ready)
- A friend can send a photo, get a structured diagnosis, accept dispatch, receive options, pick one, and get the pro's contact, with no operator action.
- A pro can JOIN, receive an alert, respond, get selected or not-selected messages, and rate the job.
- Every step is logged in `events`, and the weekly digest arrives on the operator's phone.
- Tests cover the job state machine, interaction parsing, pro matching, and the safety overrides.
