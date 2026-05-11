import axios from 'axios'
import { Pod, FolderGroup, Team, TrainingJob, QueryResponse, DirectoryListing, AgentSession, AgentTool, Conversation } from '@/types'

const api = axios.create({
  baseURL: '/api/v1',
  headers: {
    'Content-Type': 'application/json',
  },
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('rt_access_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

/** Backend `team_id` query value for agent docs/SOPs not tied to a team. */
export const AGENT_GENERAL_SCOPE = '__none__'

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config
    if (error.response?.status === 401 && !original._retry) {
      original._retry = true
      const refresh = localStorage.getItem('rt_refresh_token')
      if (refresh) {
        try {
          const resp = await axios.post('/api/v1/auth/refresh', { refresh_token: refresh })
          localStorage.setItem('rt_access_token', resp.data.access_token)
          localStorage.setItem('rt_refresh_token', resp.data.refresh_token)
          original.headers.Authorization = `Bearer ${resp.data.access_token}`
          return api(original)
        } catch {
          localStorage.removeItem('rt_access_token')
          localStorage.removeItem('rt_refresh_token')
          window.location.href = '/login'
        }
      } else {
        window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  }
)

// POD APIs
export const podApi = {
  list: async (): Promise<Pod[]> => {
    const response = await api.get('/pods')
    return response.data
  },
  
  get: async (podId: string): Promise<Pod> => {
    const response = await api.get(`/pods/${podId}`)
    return response.data
  },
  
  generate: async (data: { pod_name: string; target_os: string; custom_config?: Record<string, unknown> }) => {
    const response = await api.post('/pods/generate', data)
    return response.data
  },
  
  delete: async (podId: string) => {
    const response = await api.delete(`/pods/${podId}`)
    return response.data
  },
  
  browse: async (podId: string, path: string = '.', recursive: boolean = false): Promise<DirectoryListing> => {
    const response = await api.post(`/pods/${podId}/browse`, null, {
      params: { path, recursive }
    })
    return response.data
  },
  
  getStatus: async (podId: string) => {
    const response = await api.get(`/pods/${podId}/status`)
    return response.data
  },

  /**
   * Mint a short-lived signed URL for downloading the pod installer.
   *
   * The signed URL embeds a 60s JWT in a query parameter, so the actual
   * download can be triggered with a plain `<a href download>` click —
   * no Authorization header needed. This is the only pattern that works
   * inside the cross-origin iframe at retrace.me/try, where the
   * "fetch + blob + synthetic click" pattern is silently blocked by
   * Chrome's iframe download intervention after the fetch's await
   * consumes the iframe's transient user activation.
   */
  getDownloadUrl: async (podId: string): Promise<string> => {
    const response = await api.post(`/pods/${podId}/download-token`)
    return response.data.url
  },
}

// Local filesystem browse (backend server)
export const localFilesApi = {
  browse: async (path: string = '/'): Promise<DirectoryListing> => {
    const response = await api.get('/files/browse', { params: { path } })
    return response.data
  },
}

// Team APIs
export const teamApi = {
  list: async (): Promise<Team[]> => {
    const response = await api.get('/teams')
    return response.data
  },
  
  get: async (teamId: string): Promise<Team> => {
    const response = await api.get(`/teams/${teamId}`)
    return response.data
  },
  
  create: async (data: {
    team_name: string
    description?: string
    auto_generate_description?: boolean
    folder_groups?: Array<{
      pod_id: string
      group_name: string
      group_type: string
      folder_paths: Array<{
        absolute_path: string
        scan_recursive: boolean
        file_filters?: { include: string[]; exclude: string[] }
      }>
    }>
  }): Promise<Team> => {
    const response = await api.post('/teams', data)
    return response.data
  },
  
  update: async (teamId: string, data: Partial<{
    team_name: string
    description: string
  }>): Promise<Team> => {
    const response = await api.put(`/teams/${teamId}`, data)
    return response.data
  },
  
  delete: async (teamId: string) => {
    const response = await api.delete(`/teams/${teamId}`)
    return response.data
  },
  
  addFolderGroup: async (teamId: string, data: {
    pod_id: string
    group_name: string
    group_type: string
    folder_paths: Array<{
      absolute_path: string
      scan_recursive: boolean
      file_filters?: { include: string[]; exclude: string[] }
    }>
  }): Promise<FolderGroup> => {
    const response = await api.post(`/teams/${teamId}/groups`, data)
    return response.data
  },
  
  removeFolderGroup: async (teamId: string, groupId: string) => {
    const response = await api.delete(`/teams/${teamId}/groups/${groupId}`)
    return response.data
  },
  
  trainAll: async (teamId: string) => {
    const response = await api.post(`/teams/${teamId}/train`)
    return response.data
  },
  
  getTrainingStatus: async (teamId: string) => {
    const response = await api.get(`/teams/${teamId}/training-status`)
    return response.data
  },
  
  stopTraining: async (teamId: string) => {
    const response = await api.post(`/teams/${teamId}/stop-training`)
    return response.data
  },

  syncQA: async (teamId: string, qaPairs: { question: string; answer: string }[]) => {
    const response = await api.post(`/teams/${teamId}/sync-qa`, { qa_pairs: qaPairs })
    return response.data
  },

  getTrainingTree: async (teamId: string) => {
    const response = await api.get(`/teams/${teamId}/training-tree`)
    return response.data
  },

  generatePod: async (teamId: string, targetOs: string, agentName?: string) => {
    const response = await api.post(`/teams/${teamId}/generate-pod`, null, {
      params: { target_os: targetOs, agent_name: agentName }
    })
    return response.data
  },

  listPods: async (teamId: string) => {
    const response = await api.get(`/teams/${teamId}/pods`)
    return response.data
  },

  downloadPod: async (podId: string) => {
    // Returns the download URL — caller should open in new tab
    return `/api/v1/pods/${podId}/download`
  },
}

// Folder Group APIs (kept for backward compatibility and training)
export const groupApi = {
  startTraining: async (groupId: string) => {
    const response = await api.post(`/groups/${groupId}/train`)
    return response.data
  },
}

// Training APIs
export const trainingApi = {
  listJobs: async (groupId?: string, status?: string): Promise<TrainingJob[]> => {
    const response = await api.get('/training/jobs', {
      params: { group_id: groupId, status_filter: status }
    })
    return response.data
  },
  
  getJob: async (jobId: string): Promise<TrainingJob> => {
    const response = await api.get(`/training/jobs/${jobId}`)
    return response.data
  },
  
  cancelJob: async (jobId: string) => {
    const response = await api.post(`/training/jobs/${jobId}/cancel`)
    return response.data
  },
}

// Query APIs
export const queryApi = {
  ask: async (question: string, teamIds: string[]): Promise<QueryResponse> => {
    const response = await api.post('/query', { question, team_ids: teamIds })
    return response.data
  },
  
  getHistory: async (limit: number = 20) => {
    const response = await api.get('/query/history', { params: { limit } })
    return response.data
  },
  
  get: async (queryId: string): Promise<QueryResponse> => {
    const response = await api.get(`/query/${queryId}`)
    return response.data
  },
}

// Agent APIs
export const agentApi = {
  /** Returns the SSE endpoint URL (caller uses fetch + EventSource pattern) */
  getExecuteUrl: () => '/api/v1/agent/execute',

  /** Pre-start MCP extensions in the background (fire-and-forget). */
  warmupMcp: async (): Promise<void> => {
    await api.post('/agent/warmup-mcp').catch(() => {})
  },

  listSessions: async (teamId?: string): Promise<AgentSession[]> => {
    const response = await api.get('/agent/sessions', { params: { team_id: teamId } })
    return response.data
  },

  getSession: async (sessionId: string): Promise<AgentSession> => {
    const response = await api.get(`/agent/sessions/${sessionId}`)
    return response.data
  },

  listTools: async (): Promise<AgentTool[]> => {
    const response = await api.get('/agent/tools')
    return response.data
  },

  updateAgentTools: async (disabledTools: string[]) => {
    const response = await api.patch('/settings/agent-tools', { disabled_tools: disabledTools })
    return response.data
  },

  // Conversations
  listConversations: async (teamId: string): Promise<Conversation[]> => {
    const response = await api.get('/agent/conversations', { params: { team_id: teamId || '__none__' } })
    return response.data
  },

  createConversation: async (teamId: string, title?: string): Promise<Conversation> => {
    const response = await api.post('/agent/conversations', {
      team_id: teamId || null,
      title: title || 'New conversation',
    })
    return response.data
  },

  getConversation: async (conversationId: string): Promise<Conversation> => {
    const response = await api.get(`/agent/conversations/${conversationId}`)
    return response.data
  },

  renameConversation: async (conversationId: string, title: string) => {
    const response = await api.put(`/agent/conversations/${conversationId}`, { title })
    return response.data
  },

  deleteConversation: async (conversationId: string) => {
    const response = await api.delete(`/agent/conversations/${conversationId}`)
    return response.data
  },

  deleteAllConversations: async (teamId: string) => {
    const response = await api.delete(`/agent/conversations`, { params: { team_id: teamId } })
    return response.data
  },

  saveMessages: async (conversationId: string, messages: Array<{
    type: string; content: string; iteration?: number; meta?: Record<string, any>
  }>) => {
    const response = await api.post(`/agent/conversations/${conversationId}/messages`, { messages })
    return response.data
  },

  // Learn This — train Q&A as expert knowledge
  learnThis: async (teamId: string, question: string, answer: string) => {
    const response = await api.post('/agent/learn', { team_id: teamId, question, answer })
    return response.data
  },

  // SOPs (Automations)
  listSOPs: async (teamId: string) => {
    const response = await api.get('/agent/sops', { params: { team_id: teamId } })
    return response.data
  },

  deleteSOP: async (sopId: string) => {
    const response = await api.delete(`/agent/sops/${sopId}`)
    return response.data
  },

  approveSOP: async (sopId: string, scheduleType: string = 'none', scheduleConfig?: Record<string, any>) => {
    const response = await api.put(`/agent/sops/${sopId}/approve`, {
      schedule_type: scheduleType,
      schedule_config: scheduleConfig || {},
    })
    return response.data
  },

  manualRun: async (sopId: string) => {
    const response = await api.post(`/agent/sops/${sopId}/run`)
    return response.data
  },

  listRuns: async (sopId: string) => {
    const response = await api.get(`/agent/sops/${sopId}/runs`)
    return response.data
  },

  updateSchedule: async (sopId: string, scheduleType: string, scheduleConfig?: Record<string, any>) => {
    const response = await api.put(`/agent/sops/${sopId}/schedule`, {
      schedule_type: scheduleType,
      schedule_config: scheduleConfig || {},
    })
    return response.data
  },

  // Documentation
  listDocs: async (teamId: string) => {
    const response = await api.get('/agent/docs', { params: { team_id: teamId } })
    return response.data
  },

  deleteDoc: async (docId: string) => {
    const response = await api.delete(`/agent/docs/${docId}`)
    return response.data
  },

  approveDoc: async (docId: string) => {
    const response = await api.put(`/agent/docs/${docId}/approve`)
    return response.data
  },

  // Dashboard
  getDashboardStats: async () => {
    const response = await api.get('/agent/dashboard-stats')
    return response.data
  },

  // LLM Logs (developer mode)
  listLlmLogConversations: async (): Promise<LlmLogConversation[]> => {
    const response = await api.get('/agent/llm-logs/conversations')
    return response.data
  },
  getLlmLogs: async (conversationId: string): Promise<LlmLog[]> => {
    const response = await api.get('/agent/llm-logs', { params: { conversation_id: conversationId } })
    return response.data
  },
}

export interface LlmLogConversation {
  conversation_id: string
  title: string
  team_id: string | null
  updated_at: string | null
  log_count: number
}

export interface LlmLog {
  log_id: string
  user_id: string | null
  conversation_id: string | null
  iteration: number
  request_messages: string | null  // JSON string
  response_content: string | null
  tokens: {
    input: number
    output: number
    total: number
    cache_read: number
    cache_creation: number
    reasoning: number
  }
  model_provider: string | null
  model_name: string | null
  latency_ms: number
  error: string | null
  created_at: string | null
}

// Email API
export const emailApi = {
  // Connections
  listConnections: async () => {
    const response = await api.get('/email/connections')
    return response.data
  },
  createConnection: async (data: {
    provider: string
    tenant_name: string
    oauth_code: string
    redirect_uri?: string
    client_id?: string
    client_secret?: string
    tenant_id?: string
    slack_webhook_url?: string
    teams_webhook_url?: string
  }) => {
    const response = await api.post('/email/connections', data)
    return response.data
  },
  updateConnection: async (connectionId: string, data: {
    tenant_name?: string
    slack_webhook_url?: string
    teams_webhook_url?: string
  }) => {
    const response = await api.patch(`/email/connections/${connectionId}`, data)
    return response.data
  },
  deleteConnection: async (connectionId: string) => {
    const response = await api.delete(`/email/connections/${connectionId}`)
    return response.data
  },
  testConnection: async (connectionId: string) => {
    const response = await api.post(`/email/connections/${connectionId}/test`)
    return response.data
  },
  syncConnection: async (connectionId: string) => {
    const response = await api.post(`/email/connections/${connectionId}/sync`)
    return response.data
  },
  reconnectConnection: async (connectionId: string, oauthCode: string, redirectUri: string) => {
    const response = await api.post(`/email/connections/${connectionId}/reconnect`, {
      oauth_code: oauthCode,
      redirect_uri: redirectUri,
    })
    return response.data
  },

  // Inboxes (per team)
  getInbox: async (teamId: string) => {
    const response = await api.get(`/teams/${teamId}/inbox`)
    return response.data
  },
  createInbox: async (teamId: string, data: {
    connection_id: string
    display_name?: string
    action_config?: Record<string, any>
  }) => {
    const response = await api.post(`/teams/${teamId}/inbox`, data)
    return response.data
  },
  updateInbox: async (teamId: string, data: {
    is_active?: boolean
    action_config?: Record<string, any>
  }) => {
    const response = await api.patch(`/teams/${teamId}/inbox`, data)
    return response.data
  },
  deleteInbox: async (teamId: string) => {
    const response = await api.delete(`/teams/${teamId}/inbox`)
    return response.data
  },

  // Drafts
  listDrafts: async (teamId: string) => {
    const response = await api.get(`/teams/${teamId}/inbox/drafts`)
    return response.data
  },
  listUnread: async (teamId: string) => {
    const response = await api.get(`/teams/${teamId}/inbox/unread`)
    return response.data
  },
  approveDraft: async (teamId: string, messageId: string, editedResponse?: string) => {
    const response = await api.post(`/teams/${teamId}/inbox/drafts/${messageId}/approve`, {
      edited_response: editedResponse,
    })
    return response.data
  },
  rejectDraft: async (teamId: string, messageId: string) => {
    const response = await api.post(`/teams/${teamId}/inbox/drafts/${messageId}/reject`)
    return response.data
  },
  editDraft: async (teamId: string, messageId: string, editedResponse: string) => {
    const response = await api.patch(`/teams/${teamId}/inbox/drafts/${messageId}`, {
      edited_response: editedResponse,
    })
    return response.data
  },

  // Message log
  listMessages: async (teamId: string, limit = 50, offset = 0) => {
    const response = await api.get(`/teams/${teamId}/inbox/messages`, {
      params: { limit, offset },
    })
    return response.data
  },
}

// Channel API (Slack / Teams)
export const channelApi = {
  listConnections: async (teamId?: string) => {
    const response = await api.get('/channels/connections', {
      params: teamId ? { team_id: teamId } : {},
    })
    return response.data
  },
  createConnection: async (data: {
    team_id: string
    platform: string
    channel_name: string
    channel_id: string
    bot_token: string
    team_id?: string
    auto_respond?: boolean
    ingest_history?: boolean
  }) => {
    const response = await api.post('/channels/connections', data)
    return response.data
  },
  updateConnection: async (connectionId: string, data: {
    is_active?: boolean
    auto_respond?: boolean
    ingest_history?: boolean
  }) => {
    const response = await api.patch(`/channels/${connectionId}`, data)
    return response.data
  },
  deleteConnection: async (connectionId: string) => {
    const response = await api.delete(`/channels/${connectionId}`)
    return response.data
  },
  testConnection: async (connectionId: string) => {
    const response = await api.post(`/channels/${connectionId}/test`)
    return response.data
  },
  syncConnection: async (connectionId: string) => {
    const response = await api.post(`/channels/${connectionId}/sync`)
    return response.data
  },
  previewMessages: async (connectionId: string) => {
    const response = await api.get(`/channels/${connectionId}/preview`)
    return response.data
  },
}


// ─────────────────────────────────────────────────────────────────────
// Source-data Connections API (SFTP / SMB / S3 / Azure Blob)
// ─────────────────────────────────────────────────────────────────────

export interface SourceConnection {
  connection_id: string
  tenant_id: string | null
  name: string
  source_type: 'sftp' | 'smb' | 's3' | 'azure_blob'
  last_tested_at: string | null
  last_test_status: 'ok' | 'error' | null
  last_test_error: string | null
  created_at: string
  updated_at: string
}

export interface SourceSchemaField {
  name: string
  label: string
  type: 'string' | 'int' | 'password'
  required: boolean
  secret: boolean
  default?: string | number
}

export interface SourceDirEntry {
  name: string
  path: string
  is_dir: boolean
  size: number
}

export const connectionsApi = {
  list: async (): Promise<SourceConnection[]> => {
    const r = await api.get('/connections/')
    return r.data
  },
  schema: async (sourceType: string): Promise<{ source_type: string; fields: SourceSchemaField[] }> => {
    const r = await api.get(`/connections/schema/${sourceType}`)
    return r.data
  },
  create: async (data: { name: string; source_type: string; config: Record<string, any> }): Promise<SourceConnection> => {
    const r = await api.post('/connections/', data)
    return r.data
  },
  get: async (id: string) => {
    const r = await api.get(`/connections/${id}`)
    return r.data
  },
  update: async (id: string, data: { name?: string; config?: Record<string, any> }) => {
    const r = await api.patch(`/connections/${id}`, data)
    return r.data
  },
  delete: async (id: string) => {
    await api.delete(`/connections/${id}`)
  },
  test: async (id: string): Promise<{ ok: boolean; message: string }> => {
    const r = await api.post(`/connections/${id}/test`)
    return r.data
  },
  listDir: async (id: string, path: string = '/'): Promise<{ path: string; entries: SourceDirEntry[] }> => {
    const r = await api.get(`/connections/${id}/list`, { params: { path } })
    return r.data
  },
  oauthInit: async (provider: string, name: string): Promise<{ auth_url: string }> => {
    const r = await api.get(`/connections/oauth/init/${provider}`, { params: { name } })
    return r.data
  },
}

export default api
