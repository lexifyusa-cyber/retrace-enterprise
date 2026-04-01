import axios from 'axios'
import { Pod, FolderGroup, Product, TrainingJob, QueryResponse, DirectoryListing, AgentSession, AgentTool, Conversation } from '@/types'

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

/** Backend `product_id` query value for agent docs/SOPs not tied to a product. */
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
}

// Local filesystem browse (backend server)
export const localFilesApi = {
  browse: async (path: string = '/'): Promise<DirectoryListing> => {
    const response = await api.get('/files/browse', { params: { path } })
    return response.data
  },
}

// Product APIs
export const productApi = {
  list: async (): Promise<Product[]> => {
    const response = await api.get('/products')
    return response.data
  },
  
  get: async (productId: string): Promise<Product> => {
    const response = await api.get(`/products/${productId}`)
    return response.data
  },
  
  create: async (data: {
    product_name: string
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
  }): Promise<Product> => {
    const response = await api.post('/products', data)
    return response.data
  },
  
  update: async (productId: string, data: Partial<{
    product_name: string
    description: string
  }>): Promise<Product> => {
    const response = await api.put(`/products/${productId}`, data)
    return response.data
  },
  
  delete: async (productId: string) => {
    const response = await api.delete(`/products/${productId}`)
    return response.data
  },
  
  addFolderGroup: async (productId: string, data: {
    pod_id: string
    group_name: string
    group_type: string
    folder_paths: Array<{
      absolute_path: string
      scan_recursive: boolean
      file_filters?: { include: string[]; exclude: string[] }
    }>
  }): Promise<FolderGroup> => {
    const response = await api.post(`/products/${productId}/groups`, data)
    return response.data
  },
  
  removeFolderGroup: async (productId: string, groupId: string) => {
    const response = await api.delete(`/products/${productId}/groups/${groupId}`)
    return response.data
  },
  
  trainAll: async (productId: string) => {
    const response = await api.post(`/products/${productId}/train`)
    return response.data
  },
  
  getTrainingStatus: async (productId: string) => {
    const response = await api.get(`/products/${productId}/training-status`)
    return response.data
  },
  
  stopTraining: async (productId: string) => {
    const response = await api.post(`/products/${productId}/stop-training`)
    return response.data
  },

  syncQA: async (productId: string, qaPairs: { question: string; answer: string }[]) => {
    const response = await api.post(`/products/${productId}/sync-qa`, { qa_pairs: qaPairs })
    return response.data
  },

  getTrainingTree: async (productId: string) => {
    const response = await api.get(`/products/${productId}/training-tree`)
    return response.data
  },

  generatePod: async (productId: string, targetOs: string, agentName?: string) => {
    const response = await api.post(`/products/${productId}/generate-pod`, null, {
      params: { target_os: targetOs, agent_name: agentName }
    })
    return response.data
  },

  listPods: async (productId: string) => {
    const response = await api.get(`/products/${productId}/pods`)
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
  ask: async (question: string, productIds: string[]): Promise<QueryResponse> => {
    const response = await api.post('/query', { question, product_ids: productIds })
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

  listSessions: async (productId?: string): Promise<AgentSession[]> => {
    const response = await api.get('/agent/sessions', { params: { product_id: productId } })
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
  listConversations: async (productId: string): Promise<Conversation[]> => {
    const response = await api.get('/agent/conversations', { params: { product_id: productId || '__none__' } })
    return response.data
  },

  createConversation: async (productId: string, title?: string): Promise<Conversation> => {
    const response = await api.post('/agent/conversations', {
      product_id: productId || null,
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

  deleteAllConversations: async (productId: string) => {
    const response = await api.delete(`/agent/conversations`, { params: { product_id: productId } })
    return response.data
  },

  saveMessages: async (conversationId: string, messages: Array<{
    type: string; content: string; iteration?: number; meta?: Record<string, any>
  }>) => {
    const response = await api.post(`/agent/conversations/${conversationId}/messages`, { messages })
    return response.data
  },

  // Learn This — train Q&A as expert knowledge
  learnThis: async (productId: string, question: string, answer: string) => {
    const response = await api.post('/agent/learn', { product_id: productId, question, answer })
    return response.data
  },

  // SOPs (Automations)
  listSOPs: async (productId: string) => {
    const response = await api.get('/agent/sops', { params: { product_id: productId } })
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
  listDocs: async (productId: string) => {
    const response = await api.get('/agent/docs', { params: { product_id: productId } })
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

  // Inboxes (per product)
  getInbox: async (productId: string) => {
    const response = await api.get(`/products/${productId}/inbox`)
    return response.data
  },
  createInbox: async (productId: string, data: {
    connection_id: string
    display_name?: string
    action_config?: Record<string, any>
  }) => {
    const response = await api.post(`/products/${productId}/inbox`, data)
    return response.data
  },
  updateInbox: async (productId: string, data: {
    is_active?: boolean
    action_config?: Record<string, any>
  }) => {
    const response = await api.patch(`/products/${productId}/inbox`, data)
    return response.data
  },
  deleteInbox: async (productId: string) => {
    const response = await api.delete(`/products/${productId}/inbox`)
    return response.data
  },

  // Drafts
  listDrafts: async (productId: string) => {
    const response = await api.get(`/products/${productId}/inbox/drafts`)
    return response.data
  },
  listUnread: async (productId: string) => {
    const response = await api.get(`/products/${productId}/inbox/unread`)
    return response.data
  },
  approveDraft: async (productId: string, messageId: string, editedResponse?: string) => {
    const response = await api.post(`/products/${productId}/inbox/drafts/${messageId}/approve`, {
      edited_response: editedResponse,
    })
    return response.data
  },
  rejectDraft: async (productId: string, messageId: string) => {
    const response = await api.post(`/products/${productId}/inbox/drafts/${messageId}/reject`)
    return response.data
  },
  editDraft: async (productId: string, messageId: string, editedResponse: string) => {
    const response = await api.patch(`/products/${productId}/inbox/drafts/${messageId}`, {
      edited_response: editedResponse,
    })
    return response.data
  },

  // Message log
  listMessages: async (productId: string, limit = 50, offset = 0) => {
    const response = await api.get(`/products/${productId}/inbox/messages`, {
      params: { limit, offset },
    })
    return response.data
  },
}

// Channel API (Slack / Teams)
export const channelApi = {
  listConnections: async (productId?: string) => {
    const response = await api.get('/channels/connections', {
      params: productId ? { product_id: productId } : {},
    })
    return response.data
  },
  createConnection: async (data: {
    product_id: string
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

export default api
