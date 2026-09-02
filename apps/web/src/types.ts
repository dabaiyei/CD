export type UserRole = 'admin' | 'user'
export type ModelType = 'text' | 'image' | 'video' | 'tts'
export type ProviderType = 'sub2api' | 'newapi' | 'openai_compatible' | 'custom'
export type HandbookType = 'visual' | 'director'
export type UserSkillStage =
  | 'script_generation'
  | 'script_review'
  | 'asset_extraction'
  | 'asset_prompt_generation'
  | 'storyboard_generation'
  | 'storyboard_review'
  | 'video_generation'
export type MarketplaceResourceType = 'skill' | 'template' | 'material'

export interface User {
  id: string
  tenant_id: string
  email: string
  display_name: string
  role: UserRole
  is_active: boolean
  avatar_url: string | null
}

export interface UserSession {
  user: User
  tenant_name: string
  tenant_slug: string
  credit_balance: string
}

export interface AdminUser extends User {
  credit_balance: string
  project_count: number
  task_count: number
  last_task_at: string | null
  created_at: string
  updated_at: string
}

export interface AdminUserPage {
  items: AdminUser[]
  total: number
  active_count: number
  admin_count: number
  total_balance: string
}

export interface CreditLedgerEntry {
  id: string
  user_id: string
  amount: string
  balance_after: string
  reason: string
  reference_type: string | null
  reference_id: string | null
  created_at: string
}

export interface CreditLedgerPage {
  items: CreditLedgerEntry[]
  next_before: string | null
  next_before_id: string | null
}

export interface AdminCreditAdjustmentResult {
  user: AdminUser
  ledger: CreditLedgerEntry
}

export interface InvitationSettings {
  url_prefix: string
}

export interface Invitation {
  id: string
  code: string
  name: string
  max_registrations: number
  registration_count: number
  remaining_registrations: number
  initial_credits: string
  enabled: boolean
  invite_url: string | null
  created_at: string
  updated_at: string
}

export interface InvitationRegistrationInfo {
  code: string
  tenant_name: string
  invitation_name: string
  initial_credits: string
  remaining_registrations: number
}

export interface SecurityEvent {
  id: string
  user_id: string | null
  event_type: 'login_succeeded' | 'login_failed' | 'login_locked' | 'login_rate_limited' | string
  success: boolean
  subject_hash: string
  ip_hash: string
  user_agent: string
  event_metadata: Record<string, unknown>
  created_at: string
}

export interface SecurityEventPage {
  items: SecurityEvent[]
  next_before: string | null
  next_before_id: string | null
}

export interface Project {
  id: string
  tenant_id: string
  owner_id: string
  name: string
  description: string
  cover_url: string | null
  video_model_id: string | null
  video_resolution: string
  aspect_ratio: string
  image_model_id: string | null
  image_resolution: '1K' | '2K' | '4K'
  visual_handbook_id: string | null
  director_handbook_id: string | null
  created_at: string
  updated_at: string
}

export interface AIModel {
  id: string
  provider_id: string
  model_id: string
  name: string
  model_type: ModelType
  capabilities: Record<string, unknown>
  enabled: boolean
  is_default: boolean
  last_tested_at: string | null
  last_test_ok: boolean | null
  last_test_message: string | null
  last_test_latency_ms: number | null
  created_at: string
  updated_at: string
}

export type ImageResolution = '1K' | '2K' | '4K'

export interface ImageResolutionModelRoute {
  id: string
  resolution: ImageResolution
  model_id: string
  created_at: string
  updated_at: string
}

export type VideoGenerationMode =
  | 'text_to_video'
  | 'first_frame'
  | 'first_last_frame'
  | 'last_frame'
  | 'full_reference'
  | 'multi_shot'

export interface ReferenceLimit {
  enabled: boolean
  min_count: number
  max_count: number
}

export interface DurationResolutionGroup {
  durations: number[]
  resolutions: string[]
}

export interface VideoModelCapabilities extends Record<string, unknown> {
  schema_version: 1
  generation_modes: VideoGenerationMode[]
  reference_limits: Record<'image' | 'video' | 'audio', ReferenceLimit>
  audio_policy: 'optional' | 'required' | 'disabled'
  duration_resolution_map: DurationResolutionGroup[]
  aspect_ratios: string[]
  prompt_languages: string[]
  negative_prompt_supported: boolean
  asynchronous: boolean
}

export interface ProviderCredentialField {
  key: string
  label: string
  input_type: 'text' | 'password' | 'url'
  required: boolean
  placeholder: string
  help_text: string
}

export interface ProviderAdapterConfig extends Record<string, unknown> {
  schema_version: 1
  credential_fields: ProviderCredentialField[]
}

export interface Provider {
  id: string
  code: string
  name: string
  provider_type: ProviderType
  base_url: string
  extra_headers: Record<string, string>
  adapter_config: Record<string, unknown>
  configured_credentials: string[]
  max_concurrency: number
  enabled: boolean
  has_api_key: boolean
  last_tested_at: string | null
  last_test_ok: boolean | null
  last_test_message: string | null
  created_at: string
  updated_at: string
}

export interface Handbook {
  id: string
  handbook_type: HandbookType
  name: string
  description: string
  cover_url: string | null
  skill_path: string
  version: number
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface ProjectOptions {
  video_models: AIModel[]
  visual_handbooks: Handbook[]
  director_handbooks: Handbook[]
  video_resolutions: string[]
  aspect_ratios: string[]
  image_resolutions: Array<'1K' | '2K' | '4K'>
}

export interface Readiness {
  ready: boolean
  required_defaults: Record<'text' | 'image' | 'video', boolean>
  optional_defaults: Record<'tts', boolean>
  image_resolution_models: Record<ImageResolution, boolean>
  missing: ModelType[]
}

export interface PricingRule {
  id: string
  task_type: string
  name: string
  description: string
  unit_label: string
  unit_cost: string
  display_order: number
  version: number
  created_at: string
  updated_at: string
}

export type TaskStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface AITask {
  id: string
  project_id: string | null
  task_type: string
  status: TaskStatus
  model_id: string | null
  cost: string
  request_payload: Record<string, unknown>
  result_payload: Record<string, unknown> | null
  error_message: string | null
  progress: number
  latest_message: string | null
  latest_event_at: string | null
  heartbeat_at: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string
  updated_at: string
}

export interface TaskEvent {
  id: string
  task_id: string
  status: TaskStatus
  progress: number
  message: string
  event_metadata: Record<string, unknown>
  created_at: string
}

export interface TaskPage {
  items: AITask[]
  next_before: string | null
}

export interface NotificationItem {
  id: string
  project_id: string | null
  task_id: string | null
  category: string
  title: string
  message: string
  notification_metadata: Record<string, unknown>
  is_read: boolean
  created_at: string
  updated_at: string
}

export interface NotificationPage {
  items: NotificationItem[]
  unread_count: number
}

export interface DiscoveredModel {
  model_id: string
  name: string
  owned_by: string | null
  inferred_type: ModelType
  is_imported: boolean
}

export type SourceMode = 'novel' | 'script'
export type ChapterStatus = 'uninitialized' | 'analyzing' | 'analyzed' | 'scripting' | 'reviewing' | 'assets' | 'storyboard' | 'video' | 'completed'
export type ProjectFileKind = 'source' | 'memory' | 'analysis' | 'script' | 'asset' | 'storyboard' | 'video' | 'audio' | 'other'

export interface ProjectFileItem {
  id: string
  project_id: string
  name: string
  kind: ProjectFileKind
  mime_type: string
  size_bytes: number
  editable: boolean
  file_metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface ProjectFileDetail extends ProjectFileItem {
  content: string | null
}

export interface Chapter {
  id: string
  project_id: string
  source_file_id: string
  source_mode: SourceMode
  order_index: number
  title: string
  original_content: string
  status: ChapterStatus
  active_script_version_id: string | null
  created_at: string
  updated_at: string
}

export interface ChapterAnalysisEvent {
  title: string
  description: string
  dramatic_value: string
}

export interface ChapterAnalysisCharacter {
  name: string
  role: string
  motivation: string
  relationship: string
}

export interface ChapterAnalysis {
  id: string
  project_id: string
  chapter_id: string
  version: number
  summary: string
  content: {
    summary: string
    core_conflict: string
    opening_hook: string
    adaptation_strategy: string
    events: ChapterAnalysisEvent[]
    characters: ChapterAnalysisCharacter[]
    risks: string[]
  }
  created_at: string
  updated_at: string
}

export interface ScriptVersion {
  id: string
  project_id: string
  chapter_id: string
  version: number
  title: string
  content: string
  status: 'draft' | 'reviewing' | 'approved'
  review_notes: string
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface ScriptReview {
  id: string
  script_version_id: string
  decision: 'approved' | 'changes_requested'
  notes: string
  reviewer_name: string
  activated: boolean
  created_at: string
}

export interface ScriptReviewResult {
  review: ScriptReview
  script: ScriptVersion
}

export type DirectorWorkflowStage =
  | 'script_adapting'
  | 'script_reviewing'
  | 'awaiting_script_decision'
  | 'script_repairing'
  | 'asset_extracting'
  | 'ready_for_asset_images'
  | 'asset_preparing'
  | 'storyboard_generating'
  | 'storyboard_reviewing'
  | 'awaiting_storyboard_decision'
  | 'storyboard_repairing'
  | 'ready_for_video'
  | 'failed'
  | 'cancelled'

export interface DirectorChildRun {
  id: string
  workflow_id: string
  parent_child_run_id: string | null
  task_id: string | null
  kind: string
  title: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  attempt: number
  max_attempts: number
  summary: string
  details: Record<string, unknown>
  input_refs: Record<string, unknown>
  output_refs: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface DirectorDecisionRequest {
  id: string
  workflow_id: string
  child_run_id: string | null
  decision_type: string
  prompt: string
  options: Array<{ value: string; label: string; description: string }>
  selected_option: string | null
  feedback: string
  resolved: boolean
  created_at: string
  updated_at: string
}

export interface DirectorWorkflow {
  id: string
  project_id: string
  chapter_id: string
  chat_session_id: string | null
  stage: DirectorWorkflowStage
  status: 'running' | 'waiting_user' | 'completed' | 'failed' | 'cancelled'
  script_version_id: string | null
  asset_extraction_id: string | null
  storyboard_version_id: string | null
  current_task_id: string | null
  context_snapshot: Record<string, unknown>
  last_message: string
  last_error: string | null
  created_at: string
  updated_at: string
}

export interface DirectorWorkflowDetail {
  workflow: DirectorWorkflow
  child_runs: DirectorChildRun[]
  pending_decision: DirectorDecisionRequest | null
}

export type AssetScope = 'project' | 'global'
export type AssetType = 'character' | 'scene' | 'prop' | 'material' | 'audio'
export type AssetStatus = 'extracted' | 'prompt_ready' | 'generating' | 'ready' | 'failed'

export interface AssetItem {
  id: string
  project_id: string | null
  scope: AssetScope
  asset_type: AssetType
  parent_asset_id: string | null
  name: string
  description: string
  generation_prompt: string
  media_url: string | null
  status: AssetStatus
  asset_metadata: Record<string, unknown>
  version: number
  created_at: string
  updated_at: string
}

export interface AssetRevision {
  id: string
  asset_id: string
  version: number
  change_type: string
  source_task_id: string | null
  source_revision_id: string | null
  parent_asset_id: string | null
  name: string
  description: string
  generation_prompt: string
  media_url: string | null
  status: AssetStatus
  asset_metadata: Record<string, unknown>
  created_at: string
}

export interface AssetExtraction {
  id: string
  project_id: string
  chapter_id: string
  script_version_id: string
  version: number
  is_active: boolean
  invalidated_reason: string | null
  created_at: string
  updated_at: string
}

export interface StoryboardVersion {
  id: string
  project_id: string
  chapter_id: string
  script_version_id: string
  version: number
  is_active: boolean
  invalidated_reason: string | null
  created_at: string
  updated_at: string
}

export interface StoryboardShot {
  id: string
  storyboard_version_id: string
  order_index: number
  title: string
  shot_type: string
  duration_seconds: string
  scene_description: string
  action_description: string
  dialogue: string
  image_prompt: string
  video_prompt: string
  asset_ids: string[]
  reference_image_url: string | null
  version: number
  created_at: string
  updated_at: string
}

export type VideoClipStatus = 'queued' | 'generating' | 'ready' | 'failed' | 'cancelled'

export interface VideoClip {
  id: string
  storyboard_version_id: string
  shot_id: string
  model_id: string
  version: number
  status: VideoClipStatus
  media_url: string | null
  provider_job_id: string | null
  error_message: string | null
  is_active: boolean
  invalidated_reason: string | null
  created_at: string
  updated_at: string
}

export interface StoryboardVersionDetail {
  version: StoryboardVersion
  shots: StoryboardShot[]
  video_clips: VideoClip[]
}

export interface DialogueVersion {
  id: string
  project_id: string
  chapter_id: string
  script_version_id: string
  storyboard_version_id: string | null
  version: number
  runtime_manifest: Record<string, unknown>
  is_active: boolean
  invalidated_reason: string | null
  created_at: string
  updated_at: string
}

export interface DialogueLine {
  id: string
  dialogue_version_id: string
  shot_id: string | null
  order_index: number
  speaker: string
  text: string
  emotion: string
  direction: string
  source_hash: string
  version: number
  created_at: string
  updated_at: string
}

export interface VoiceBinding {
  id: string
  project_id: string
  character_asset_id: string
  tts_model_id: string
  provider_voice_id: string
  provider_voice_name: string
  style: string
  instructions: string
  enabled: boolean
  version: number
  created_at: string
  updated_at: string
}

export type AudioClipStatus = 'queued' | 'generating' | 'ready' | 'failed' | 'cancelled'

export interface AudioClip {
  id: string
  dialogue_version_id: string
  dialogue_line_id: string
  voice_binding_id: string
  model_id: string
  line_version: number
  binding_version: number
  version: number
  status: AudioClipStatus
  media_url: string | null
  provider_job_id: string | null
  duration_seconds: string | null
  error_message: string | null
  is_active: boolean
  invalidated_reason: string | null
  created_at: string
  updated_at: string
}

export interface DialogueVersionDetail {
  version: DialogueVersion
  lines: DialogueLine[]
  audio_clips: AudioClip[]
}

export interface DubbingOptions {
  tts_models: AIModel[]
  character_assets: AssetItem[]
  voice_bindings: VoiceBinding[]
}

export type CompositionStatus = 'draft' | 'rendering' | 'ready' | 'failed' | 'stale'

export interface CompositionTimelineDialogue {
  dialogue_line_id: string
  audio_clip_id: string
  speaker: string
  duration_seconds: string
  timeline_start_seconds: string
  media_url: string | null
}

export interface CompositionTimelineShot {
  shot_id: string
  order_index: number
  title: string
  duration_seconds: string
  timeline_start_seconds: string
  video_clip_id: string
  media_url: string | null
  dialogue_clips: CompositionTimelineDialogue[]
}

export interface CompositionVersion {
  id: string
  project_id: string
  chapter_id: string
  storyboard_version_id: string
  dialogue_version_id: string | null
  version: number
  title: string
  duration_seconds: string
  resolution: string
  aspect_ratio: string
  fps: number
  timeline_manifest: {
    schema_version: number
    duration_seconds: string
    shots: CompositionTimelineShot[]
    background_music: { project_file_id: string; name: string } | null
    environment_audio: Array<{ project_file_id: string; name: string }>
    dialogue_volume: string
    background_music_volume: string
    environment_volume: string
    fade_seconds: string
    warnings: string[]
  }
  status: CompositionStatus
  output_url: string | null
  error_message: string | null
  is_active: boolean
  invalidated_reason: string | null
  created_at: string
  updated_at: string
}

export interface FinishingOptions {
  audio_files: ProjectFileItem[]
}

export interface AgentProfile {
  id: string
  kind: 'screenplay' | 'general'
  name: string
  description: string
  system_prompt: string
  text_model_id: string | null
  memory_enabled: boolean
  config: Record<string, unknown>
  enabled: boolean
  version: number
  created_at: string
  updated_at: string
}

export interface AgentOption {
  id: string
  kind: 'screenplay' | 'general'
  name: string
  description: string
  memory_enabled: boolean
}

export interface AgentSkill {
  id: string
  handbook_type: HandbookType
  name: string
  description: string
  version: number
}

export interface AgentChatOptions {
  agents: AgentOption[]
  skills: AgentSkill[]
}

export interface AgentChatSession {
  id: string
  project_id: string | null
  agent_profile_id: string
  title: string
  last_message_at: string
  runtime_manifest: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface AgentChatAttachment {
  id: string
  project_id: string | null
  name: string
  mime_type: string
  size_bytes: number
  media_url: string
}

export interface AgentGeneratedMedia extends AgentChatAttachment {
  prompt: string
  model_id: string
  model_name: string
  resolution: string
  aspect_ratio: string
  duration_seconds: number | null
  generation_mode: string | null
}

export interface AgentChatMessage {
  id: string
  session_id: string
  role: 'user' | 'assistant'
  content: string
  run_id: string | null
  finish_reason: string | null
  runtime_events: Record<string, unknown>[]
  runtime_manifest: Record<string, unknown> | null
  created_at: string
}

export interface AgentExecutionStep {
  id: string
  kind: 'model' | 'tool'
  name: string
  status: 'running' | 'succeeded' | 'failed'
  startedAt?: string
  completedAt?: string
}

export interface AgentProjectFileChangeOutcome {
  operation:
    | 'create'
    | 'update'
    | 'delete'
    | 'batch'
    | 'publish_script_version'
    | 'publish_storyboard_version'
    | 'queue_asset_prompt_generation'
    | 'queue_asset_image_generation'
    | 'queue_storyboard_workflow'
  file_id: string | null
  name: string
  status: 'applied' | 'conflict' | 'rejected'
  reason: string | null
  resource_type?:
    | 'script_version'
    | 'storyboard_version'
    | 'asset_prompt_task'
    | 'asset_image_tasks'
    | 'director_workflow'
  resource_id?: string
  task_id?: string
  task_ids?: string[]
  asset_ids?: string[]
  asset_count?: number
  prompt_task_id?: string | null
  prompt_asset_ids?: string[]
  prompt_asset_count?: number
  image_queue_error?: string | null
  auto_queue_images_after_prompt?: boolean
  auto_generate_missing_prompts?: boolean
  chapter_id?: string
  version?: number
  workflow_id?: string
  stage?: string
}

export interface AgentChatSessionDetail {
  session: AgentChatSession
  messages: AgentChatMessage[]
  active_task: AITask | null
}

export interface AgentChatRun {
  session: AgentChatSession
  user_message: AgentChatMessage
  assistant_message: AgentChatMessage
}

export interface AgentChatRunQueued {
  session: AgentChatSession
  user_message: AgentChatMessage
  task: AITask
}

export interface PromptTemplate {
  id: string
  code: string
  name: string
  description: string
  content: string
  version: number
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface UserSkill {
  id: string
  name: string
  trigger_stages: UserSkillStage[]
  description: string
  enabled: boolean
  version: number
  created_at: string
  updated_at: string
}

export interface UserTemplate {
  id: string
  name: string
  description: string
  category: string
  content: string
  version: number
  created_at: string
  updated_at: string
}

export interface MarketplaceListing {
  id: string
  resource_type: MarketplaceResourceType
  title: string
  description: string
  category: string
  tags: string[]
  cover_url: string | null
  payload: Record<string, unknown>
  version: number
  download_count: number
  publisher_name: string
  publisher_avatar_url: string | null
  owned_by_me: boolean
  acquired: boolean
  has_update: boolean
  target_id: string | null
  published_at: string
  updated_at: string
}

export interface MarketplacePage {
  items: MarketplaceListing[]
  total: number
  categories: string[]
}

export interface MarketplaceAcquisitionResult {
  listing_id: string
  target_type: 'user_skill' | 'user_template' | 'global_asset'
  target_id: string
  listing_version: number
  created: boolean
}

export interface UserSkillStageOption {
  value: UserSkillStage
  label: string
}

export interface HandbookSkillFile {
  key: string
  filename: string
  label: string
  purpose: string
  content: string
}

export interface HandbookPackage {
  handbook: Handbook
  files: HandbookSkillFile[]
}

export interface HandbookManifestFile {
  key: string
  filename: string
  label: string
  purpose: string
}

export interface HandbookManifest {
  handbook_type: HandbookType
  files: HandbookManifestFile[]
}

export interface SkillNode {
  name: string
  path: string
  kind: 'directory' | 'file'
  children: SkillNode[]
}

export interface ApiErrorPayload {
  detail?: string | Array<{ msg: string }>
}
