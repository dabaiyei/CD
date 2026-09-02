# Architecture

## Service boundaries

```text
Vue 3 Web (PC/H5)
        |
        v
FastAPI SaaS Core
  - tenant and RBAC isolation
  - projects, credits, assets, storyboards and clips
  - providers and four model types
  - Agent sessions, prompts, handbooks and memory
        |
        +--------------------> Media model gateway / workers
        |
        v  internal HTTP v2 (v1 compatibility route retained)
Agent Runtime Service
  - task workspace isolation
  - Skills snapshots and hash verification
  - controlled project-file snapshots and structured diffs
  - execution version manifest
        |
        v
AgentScope 2.0.7.post1
```

PostgreSQL with pgvector is authoritative for tenants, projects, configuration, tenant pricing rules, credits, tasks, notifications, chapter versions, assets, storyboards, dialogue versions, voice bindings, video/audio clips, immutable composition manifests, versioned conversation summaries and long-term Agent memory vectors. Redis carries queue messages and live events only. AgentScope state and events are execution records, not business state.

## Database migrations

```text
API container start -> alembic upgrade head -> revision guard -> seed (optional) -> serve traffic
Worker start ---------------------------------> revision guard -> recover tasks -> consume queue
```

- `apps/api/migrations` is the production schema history; runtime `create_all` is limited to development
  and test SQLite databases.
- The API container is the single Compose migration owner. Workers wait for the API health check and do
  not run migrations concurrently.
- API and Worker production startup query `alembic_version` and compare it with the code migration head.
  Missing or stale revisions fail fast before business queries or task recovery begin.
- The initial migration defers the circular `chapters.active_script_version_id` foreign key until
  `script_versions` exists, and supports both PostgreSQL production and SQLite migration tests.
- Each schema change must include a new revision, an upgrade/downgrade round-trip test and a clean
  `alembic check` against current SQLAlchemy metadata.
- Operational composite indexes follow the production query shapes for project task deduplication,
  tenant/user activity timelines, lease recovery and unread notification counts.

## Persistent task recovery

```text
API transaction -> PostgreSQL task row -> Redis enqueue hint -> Worker lease/heartbeat
                         ^                         |
                         +---- recovery scan <----+
```

- API requests commit the task and credit debit before enqueueing work.
- Task submission locks the owning project row before checking active work. Concurrent clicks or API
  replicas therefore observe the task committed by the first request before another debit can occur.
- Workers atomically claim work with a process identity, `started_at`, `heartbeat_at` and `lease_expires_at`. Heartbeats can extend only the lease owned by that process; terminal events persist `completed_at` and release ownership.
- Redis accelerates dispatch but is not authoritative. Startup and periodic recovery scans requeue eligible PostgreSQL tasks after message loss or process restart.
- Recovery uses the explicit lease deadline and falls back to the legacy `updated_at` cutoff only for pre-migration running rows. A background recovery loop scans expired leases while the Worker remains alive, and PostgreSQL `FOR UPDATE SKIP LOCKED` prevents multiple Workers from recovering the same row. Every intermediate and terminal write locks the task row and revalidates `status + worker_id`, so a late old Worker cannot overwrite a reclaimed task even when lease recovery and completion race.
- Every task persists an indexed idempotency key, and asynchronous media tasks persist an indexed provider job id in addition to their immutable result audit payload. These keys protect provider submission and billing paths from duplicate execution and support operational reconciliation.
- Tasks that cannot be resumed from a provider are moved to an explicit retryable or failed state and settled; they are never left running indefinitely.
- Every state transition appends a `TaskEvent` with progress, user-facing message, metadata and timestamp. Task list queries use one window-ranked event per task, so the UI receives the latest durable progress without N+1 queries or loading full histories.
- Terminal events create PostgreSQL notifications in the same transaction. The PC popover and H5 full-screen activity center read these records as the source of truth; Redis events only reduce refresh latency.
- Authenticated browser clients consume user-scoped Redis Pub/Sub events through an SSE endpoint. The access token stays in the request header, Nginx buffering is disabled for this route, and a 30-second PostgreSQL reconciliation poll covers disconnects or lost ephemeral events.
- Agent chat follows the same durable path: the API commits the user message, zero-cost `agent_chat_run` task and initial event together, then returns `202 Accepted`. A session permits only one active Agent task.
- The shared task submission service and project creation route enforce tenant readiness before any debit or task insert. Text, image and video defaults are mandatory; TTS remains optional. An unready tenant receives `503` without a task row or credit mutation.
- Agent chat uses the stable platform chat-session id as its Runtime session id. AgentScope state and the latest completed task receipt are written in one atomic envelope; replaying the same task returns the receipt without adding a duplicate turn.
- PostgreSQL remains the recovery authority. When Runtime state is absent, the latest versioned summary and a bounded recent-message window bootstrap the session. Once state exists, that recovery context is omitted to avoid duplicating history already held by AgentScope.
- After the main reply, the same configured text model performs an isolated memory-maintenance pass. It produces a new summary version and durable, source-tracked memory candidates; maintenance failure falls back to a bounded deterministic summary and never converts a completed user reply into a failed task.
- Each turn embeds the current intent and retrieves only tenant/user/project-scoped memories. PostgreSQL uses an HNSW cosine index through pgvector; SQLite development and tests use the same persisted vectors with in-process cosine ranking.

## Tenant pricing and credit settlement

```text
tenant pricing rule -> immutable task pricing snapshot -> debit + task commit
                              |
                              +-> retry / cancel / failure settlement
```

- `pricing_rules` has one row per tenant and supported task type. The seeded catalog currently covers 11 Agent, cover, chapter, asset, storyboard, video, dialogue and TTS task types.
- Tenant administrators read and update rules through `/api/v1/admin/pricing-rules`; authenticated creators receive the same tenant-scoped effective catalog from `GET /api/v1/pricing` for consistent UI estimates.
- Task submission resolves the rule inside the task transaction. The request payload freezes `unit_cost`, `quantity`, `total_cost` and `rule_version`, while `ai_tasks.cost` retains the authoritative settlement amount.
- Updating a rule increments its version and affects new tasks only. Retries do not reprice an existing task, and cancellation or failure refunds use the frozen `ai_tasks.cost` exactly once.
- Multi-asset prompt requests multiply the unit price by the selected quantity. Image, video and TTS generations remain separate tasks so each item has an independent debit, status and refund boundary.
- Credit-account rows are locked during debit and refund. Ledger rows and task state commit with the same PostgreSQL transaction; Redis never participates in balance calculation.

## Director workflow invariants

- Imported originals and EPUB-derived editable text are retained as project files.
- Chapters begin uninitialized and progress through analysis, script, review, assets, storyboard and video.
- Script history is immutable and exactly one version can be active per chapter.
- Activating a different script invalidates dependent asset extraction and storyboard versions without deleting history.
- Project assets are AI-readable. Tenant-global assets become AI-readable only after an explicit copy into the project.

## Chapter intelligence and script tasks

```text
chapter source -> AgentScope analysis task -> versioned analysis + JSON project file
                         |
                         v
          selected analysis + optional base script
                         |
                         v
             AgentScope screenplay task -> immutable reviewing script + Markdown file
```

- Analysis and screenplay tasks persist the source content hash, selected analysis id/version, optional base-script id/version, model, prompt and runtime manifest.
- A source change makes an older queued or running result fail and refund instead of overwriting the newer chapter context.
- Generated scripts always enter review as inactive versions. Only an explicit user action can make a script active and invalidate downstream assets or storyboards.
- Cancellation and failure restore the chapter to the latest durable stage derived from existing analysis and script records.

## Asset production tasks

```text
active script -> chapter asset extraction -> project assets + versioned JSON file
                                              |
                                              v
                                  batch prompt generation
                                              |
                                              v
                               one image task per selected asset
```

- Extraction and prompt generation run through the general Agent profile and the isolated AgentScope runtime; image tasks use the media gateway directly.
- Every task persists the selected model, input entity versions, cost and runtime manifest before execution. A script or asset edit makes stale results fail instead of overwriting newer user state.
- Duplicate active work is rejected per chapter or asset. Audio assets never enter the image path.
- Image files are committed only after validation. Failure refunds task cost; an older ready image remains usable when regeneration fails.
- The director UI derives queued/running state from the shared persistent task store and refreshes chapters, files and assets after task transitions.
- Character, scene and prop assets may form one-level same-type derivative relationships inside one library. Material and audio assets remain roots, and a root with children cannot be deleted or converted into a derivative.
- Every asset has an immutable `lineage_id`. Project/global copy operations preserve that lineage; copying a derivative resolves an existing destination root with the same lineage or atomically copies its root before the child, so imports and exports never create dangling relationships.

## Storyboard and video tasks

```text
active script + active extraction -> AgentScope storyboard task -> versioned shots + JSON file
                                                                  |
                                                                  v
                                                        one video task per shot
                                                                  |
                                      provider job id -> polling/recovery -> video project file
```

- A storyboard result is accepted only while its script and extraction versions remain active. Structured shots reference project asset ids and retain a JSON snapshot for audit and export.
- Editing a shot increments its entity version and invalidates active clips. Activating another storyboard invalidates clips belonging to other versions without deleting history.
- Video submissions use the persisted task id as the provider idempotency key. Async provider job ids are committed before polling, so a recovered worker continues the same provider job instead of submitting another one.
- A retry after a polling, download or storage interruption retains the provider job id and resumes polling. A provider-confirmed terminal failure starts a fresh generation attempt, while a changed shot version rejects the old retry entirely.
- Clip completion checks the storyboard and shot version again before committing media. A failed regeneration is refunded and never replaces an older ready clip.

## Dialogue and dubbing tasks

```text
active script + optional active storyboard -> AgentScope dialogue extraction -> versioned dialogue lines
                                                                           |
character asset -> versioned voice binding -------------------------------+
                                                                           v
                                                           one TTS task per dialogue line
                                                                           |
                                      provider job id -> polling/recovery -> audio project file
```

- Dialogue extraction uses the general Agent profile and persists the script hash, optional storyboard version, prompt/skill versions and runtime manifest.
- Dialogue lines retain speaker, emotion, performance direction, optional shot mapping, source hash and edit version. Editing one line invalidates only its active audio history.
- Voice bindings are project-scoped and attach a character asset to a TTS model and provider voice id. Updating or disabling a binding invalidates dependent active clips without deleting history.
- TTS execution remains in the media gateway, outside AgentScope. Async provider job ids are persisted before polling and resume after restart.
- Stale synchronous image, video and TTS requests without a provider job id are failed and refunded on recovery because the upstream result cannot be proven. Async video and TTS jobs with a durable provider id resume polling; transport failures retain that id for the next explicit retry.
- Each successful clip is stored as an audio project file. Dialogue extraction and per-line TTS use the tenant pricing catalog and retain their resolved prices in the originating task snapshot.

## Chapter composition and delivery

```text
active storyboard + active clips + optional dialogue audio
                         |
                         v
            immutable composition manifest
                         |
                         v
          persistent FFmpeg render task -> MP4 + project file
```

- Preparing a composition snapshots the exact storyboard, video clip, dialogue audio and uploaded music/environment file ids. A missing shot video blocks preparation; TTS remains optional and missing dialogue audio is recorded as a warning.
- Rendering is a zero-credit persistent task. The worker validates every entity version and source path before invoking FFmpeg asynchronously, then validates the active storyboard again before committing the output.
- Redis loss or worker restart requeues the PostgreSQL task and rerenders the immutable manifest. Source edits mark draft, rendering and ready compositions stale without deleting history.
- The API container and worker image include the same FFmpeg build. Output files and the full production manifest are retained in the project file library for audit and download.

## Isolation rules

- Every business query is scoped to the authenticated tenant; ordinary project access is additionally scoped to the owner.
- Login attempts are limited in Redis across API replicas and locked by pseudonymous tenant/email identity in PostgreSQL. Security audit rows retain HMAC fingerprints instead of raw email or IP values, and only tenant administrators can query their tenant events.
- Provider keys are encrypted at rest and only the selected task credential crosses the private runtime boundary.
- Uploaded covers use generated names under tenant/project directories and are decoded and re-encoded before serving.
- Skills editing is restricted to the configured Skills root. Runtime Skills are content-addressed task snapshots, not a shared writable directory.
- Agent-readable project text files are copied into an ephemeral task workspace. The runtime returns structured create/update/delete intents; it never writes PostgreSQL or shared uploads directly.
- The core locks and revalidates every returned file mutation against tenant, user, project, editability, type, size and the original SHA-256. Non-conflicting changes may commit while conflicts and rejected operations remain visible in the assistant message audit manifest.
- AgentScope state and persistent chat workspaces are scoped by tenant/project/session; one-off workflow calls remain tenant/project/task scoped. Runtime tools additionally enforce the current workspace and platform-authorized editable paths. Production must still add container filesystem, network and resource controls because the SDK is not the platform's only security boundary.
- Agent chat sessions and messages remain in the core database; only the selected provider credential and immutable project context cross the private runtime boundary for each run.
- Runtime snapshots are rebuilt before every run. Stale Skills, project files and Agent-created files from an interrupted run are removed before the immutable PostgreSQL snapshot is materialized again. Deleting a conversation cascades its summaries and derived automatic memories and asks the Runtime to remove the matching state and workspace.

## Object storage boundary

- Original TXT/EPUB uploads and generated covers, images, video, audio and chapter renders are written through an asynchronous object-storage interface. Local development uses a traversal-safe filesystem backend; Compose uses a private MinIO S3-compatible bucket.
- Object keys are generated by the platform from tenant/project scope and never accepted from the client. Original-file downloads remain behind JWT project authorization. Browser-rendered generated media uses path-bound HMAC capability URLs in production; unsigned paths are rejected and the object service itself is not exposed publicly.
- PostgreSQL manifests retain stable object keys. Before FFmpeg starts, the Worker validates every referenced business version, downloads missing objects into its local cache and creates an execution-only manifest with local paths. The immutable database manifest is never rewritten with node-specific paths.
- Upload or generation failure before the database commit removes both the object and its cache file. A recovered task can rehydrate committed objects after a Worker restart without resubmitting the upstream generation request.

## Versioning

The current runtime route and payload use contract `v2`; `/internal/v1/runs` remains available and the core client can fall back to it while runtime images are upgraded independently. Each Agent result includes the runtime version, pinned AgentScope version, contract version, model route, model id, prompt versions, Skills versions and project-file mutation outcomes. New AgentScope releases are deployed as new runtime images and can run beside previous images for compatibility and rollback.
