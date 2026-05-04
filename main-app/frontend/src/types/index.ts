export interface Pod {
  pod_id: string
  pod_name: string
  machine_hostname: string | null
  os_type: string | null
  status: 'online' | 'offline' | 'pending' | 'degraded'
  last_heartbeat: string | null
  created_at: string | null
  metadata: Record<string, unknown>
}

export interface FolderPath {
  path_id: string
  group_id: string
  absolute_path: string
  scan_recursive: boolean
  file_filters: {
    include: string[]
    exclude: string[]
  }
  is_active: boolean
  created_at: string | null
}

export interface FolderGroup {
  group_id: string
  team_id: string
  pod_id: string
  group_name: string
  group_type: 'code' | 'documentation' | 'diagrams' | 'configuration' | 'tickets' | 'other'
  namespace: string
  created_at: string | null
  last_trained: string | null
  training_status: 'pending' | 'training' | 'completed' | 'failed'
  folder_paths: FolderPath[]
  metadata: Record<string, unknown>
}

export interface Team {
  team_id: string
  team_name: string
  description: string | null
  auto_generate_description?: boolean
  created_at: string | null
  folder_groups: FolderGroup[]
  metadata: Record<string, unknown>
}

export interface TrainingJob {
  job_id: string
  group_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  started_at: string | null
  completed_at: string | null
  progress_data: {
    phase?: string
    progress?: number
    total?: number
    current_file?: string
  }
  statistics: {
    files_processed?: number
    chunks_created?: number
    entities_extracted?: number
    relationships_created?: number
    errors?: string[]
  }
  error_log: string | null
  created_at: string | null
}

export interface StepTimings {
  init_clients_ms: number
  classify_ms: number
  context_assembly_ms: number
  llm_answer_ms: number
  related_queries_ms: number
  total_ms: number
}

export interface QueryResponse {
  query_id: string
  question: string
  answer: string
  confidence_score: number
  sources: SourceReference[]
  related_queries: string[]
  duration_ms: number
  timings?: StepTimings
}

export interface SourceReference {
  type: 'code' | 'documentation' | 'diagram' | 'incident'
  pod_id: string
  group_name: string
  file_path: string | null
  snippet: string | null
  metadata: Record<string, unknown>
}

export interface DirectoryListing {
  files: FileEntry[]
  total_count: number
  total_size: number
}

// ---------------------------------------------------------------------------
// Agent types
// ---------------------------------------------------------------------------

export interface AgentSession {
  session_id: string
  team_id: string
  task: string
  status: 'running' | 'completed' | 'failed' | 'stopped'
  iterations: number
  final_answer: string | null
  error: string | null
  created_at: string | null
  completed_at: string | null
}

export interface AgentTool {
  name: string
  description: string
  requires_key: string | null
  available: boolean
  enabled?: boolean
  group?: string | null
}

export interface FileEntry {
  name: string
  path: string
  rel_path: string
  type: 'file' | 'directory'
  size: number
  modified: string
}

// ---------------------------------------------------------------------------
// Conversation types
// ---------------------------------------------------------------------------

export interface ConversationMessage {
  message_id: string
  conversation_id: string
  type: 'user' | 'status' | 'tools' | 'code' | 'output' | 'answer' | 'error' | 'done' | 'sop' | 'doc'
  content: string
  iteration?: number
  meta?: Record<string, any>
  position: number
  created_at: string | null
}

export interface Conversation {
  conversation_id: string
  team_id: string
  title: string
  created_at: string | null
  updated_at: string | null
  message_count: number
  messages?: ConversationMessage[]
}
