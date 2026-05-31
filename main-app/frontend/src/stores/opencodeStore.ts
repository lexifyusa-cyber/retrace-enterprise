import { create } from 'zustand'
import toast from 'react-hot-toast'
import { agentApi } from '@/utils/api'
import type { FilePart } from '@/utils/attachments'

/** Wire-shape of an OC agent-mention part (the `@build` typeahead pick).
 * OC reads `name` for the mention, `source` for hover-replay in its own UI. */
export type SendAgentPart = {
  type: 'agent'
  name: string
  source: { value: string; start: number; end: number }
}

/** Anything we can hand to `sendMessage` alongside the text content. The
 * backend's `opencode_send_message` whitelists which types it'll forward to
 * OC's `prompt_async`. */
export type SendPart = FilePart | SendAgentPart

async function _getAuthHeaders(): Promise<Record<string, string>> {
  const token = localStorage.getItem('rt_access_token')
  if (!token) return {}
  return { Authorization: `Bearer ${token}` }
}

async function _refreshToken(): Promise<boolean> {
  const refresh = localStorage.getItem('rt_refresh_token')
  if (!refresh) return false
  try {
    const resp = await fetch('/api/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refresh }),
    })
    if (!resp.ok) return false
    const data = await resp.json()
    if (data.access_token) {
      localStorage.setItem('rt_access_token', data.access_token)
      if (data.refresh_token) localStorage.setItem('rt_refresh_token', data.refresh_token)
      return true
    }
  } catch { /* ignore */ }
  return false
}

// ---------------------------------------------------------------------------
// Type model — mirrors OpenCode's message/part structure
// ---------------------------------------------------------------------------

export type TextPart = {
  type: 'text'
  id: string
  text: string
  /** Set when this user-role part was injected by retrace (scheduled
   * followup, monitor, remote-trigger webhook) or by OC's own task.inject
   * for completion notifications. Renders with a "system" badge instead of
   * the user's avatar. Absent (= undefined) on user-typed messages. */
  synthetic?: boolean
}

export type ToolPart = {
  type: 'tool'
  id: string
  tool: string
  callID: string
  title?: string
  state: {
    status: 'pending' | 'running' | 'completed' | 'error'
    input?: Record<string, unknown>
    output?: string
  }
}

export type ThinkingPart = {
  type: 'thinking'
  id: string
  text: string
}

export type SOPPart = {
  type: 'sop'
  id: string
  sopId: string
  title: string
  content: string
  approved?: boolean
}

export type DocPart = {
  type: 'doc'
  id: string
  docId: string
  title: string
  content: string
  approved?: boolean
}

/** Attachment part — wire-compatible with OC's `{type:"file", mime, filename, url}`
 * shape; we add `id` for React keys. `url` is the RFC 2397 data URL. */
export type AttachmentPart = {
  type: 'file'
  id: string
  mime: string
  filename: string
  url: string
}

export type Part = TextPart | ToolPart | ThinkingPart | SOPPart | DocPart | AttachmentPart

export interface OCMessage {
  id: string
  role: 'user' | 'assistant'
  parts: Part[]
  model?: string
  cost?: number
  tokens?: { input: number; output: number; reasoning: number; cache: number }
  isStreaming?: boolean
  /** Marks the message as loaded from the ReTrace DB (a prior dead OpenCode
   * session). Preserved across refreshMessages so it stays visible after a
   * fresh OC session takes over. Live OC messages do not set this. */
  isLegacy?: boolean
}

export interface SessionCost {
  session_id: string
  total_cost: number
  tokens: { input: number; output: number; reasoning: number; cache: number }
  message_count: number
}

// Legacy compatibility — flatten OCMessage[] into the old format for gradual migration
export interface FlatMessage {
  id: string
  role: 'user' | 'assistant' | 'tool'
  content: string
  toolName?: string
  toolInput?: Record<string, unknown>
  toolOutput?: string
  isStreaming?: boolean
}

export function flattenMessages(msgs: OCMessage[]): FlatMessage[] {
  const flat: FlatMessage[] = []
  for (const msg of msgs) {
    if (msg.role === 'user') {
      const textPart = msg.parts.find(p => p.type === 'text')
      if (textPart && textPart.type === 'text') {
        flat.push({ id: msg.id, role: 'user', content: textPart.text })
      }
    } else if (msg.role === 'assistant') {
      for (const part of msg.parts) {
        if (part.type === 'text' && part.text) {
          flat.push({ id: part.id, role: 'assistant', content: part.text, isStreaming: msg.isStreaming })
        } else if (part.type === 'tool') {
          const toolOutput = typeof part.state.output === 'string' ? part.state.output : JSON.stringify(part.state.output || '', null, 2)
          flat.push({
            id: part.id,
            role: 'tool',
            content: toolOutput,
            toolName: part.tool,
            toolInput: part.state.input,
            toolOutput,
            isStreaming: part.state.status !== 'completed' && msg.isStreaming,
          })
        } else if (part.type === 'thinking' && part.text) {
          flat.push({ id: part.id, role: 'assistant', content: `> _Thinking..._\n${part.text}`, isStreaming: msg.isStreaming })
        }
      }
    }
  }
  return flat
}

// ---------------------------------------------------------------------------
// Per-conversation session state
// ---------------------------------------------------------------------------

export interface SessionState {
  /** ReTrace conversation_id — this is the key */
  conversationId: string
  /** OpenCode session id — may be null until the first send creates it upstream */
  sessionId: string | null
  messages: OCMessage[]
  isRunning: boolean
  pendingPermission: { id: string; tool: string; input: any } | null
  /** OC's `question` tool — full request shape from `GET /question`. The agent
   * blocks until this is replied to via `replyToQuestion` or `rejectQuestion`. */
  pendingQuestion: {
    id: string
    sessionID: string
    questions: Array<{
      question: string
      header?: string
      options?: Array<{ label: string; description?: string }>
      multiple?: boolean
      custom?: boolean
    }>
    tool?: { messageID?: string; callID?: string }
  } | null
  sessionCost: SessionCost | null
  canUndo: boolean
  canRedo: boolean
  /** Agent mode for THIS conversation — set at OpenCode session create, immutable after */
  agent: 'build' | 'plan'
  /** Reasoning/"Thinking" mode for THIS conversation. 'off' = no thinking (default,
   * fully backward-compatible); 'think' = normal thinking; 'deep' = exceptional
   * thinking. Threaded into the send body as `reasoning`; the backend resolves it
   * per-model (native reasoning when supported, otherwise a no-op). */
  reasoning: 'off' | 'think'
  /** Messages the user submitted while a run was in-flight. Drained FIFO when
   * the current turn finishes — matches OC's `pendingMessages` / `ui.message.queued`
   * model. Each entry triggers another `sendMessage` call when the head pops.
   * `parts` carries any attachments + agent mentions picked at queue time. */
  queuedMessages: Array<{ id: string; text: string; parts?: SendPart[] }>
  /** Per-session ring buffer of prompts the user has sent, newest last. Used
   * by the composer's Up/Down-arrow recall. Capped at 50 entries. */
  recentPrompts: string[]
}

function makeSession(conversationId: string, agent: 'build' | 'plan' = 'build'): SessionState {
  return {
    conversationId,
    sessionId: null,
    messages: [],
    isRunning: false,
    pendingPermission: null,
    pendingQuestion: null,
    sessionCost: null,
    canUndo: false,
    canRedo: false,
    agent,
    reasoning: 'off',
    queuedMessages: [],
    recentPrompts: [],
  }
}

// ---------------------------------------------------------------------------
// Store interface
// ---------------------------------------------------------------------------

interface OpenCodeState {
  // ── Global server state ───────────────────────────────────────────────
  isStarting: boolean
  /** True when the backend runs against a dev-managed external OC (env var
   *  RETRACE_OPENCODE_EXTERNAL_URL). Used to hide "Stop OpenCode server" —
   *  which is a no-op in this mode (we don't own the OC process). */
  externalMode: boolean

  // ── Per-conversation sessions ─────────────────────────────────────────
  sessions: Record<string, SessionState>
  /** Which conversation the input bar is currently bound to (mirrors page state) */
  activeConversationId: string | null

  // ── Server-level actions ──────────────────────────────────────────────
  startServer: (teamId?: string) => Promise<void>
  stopServer: () => Promise<void>
  checkStatus: () => Promise<{ running: boolean; healthy: boolean }>
  regenerateConfig: (teamId?: string) => Promise<void>

  // ── Active conversation tracking ──────────────────────────────────────
  setActiveConversation: (conversationId: string | null) => void

  // ── Per-conversation actions ──────────────────────────────────────────
  createSession: (conversationId: string, teamId?: string, agent?: 'build' | 'plan') => Promise<string>
  loadSession: (conversationId: string, opencodeSessionId: string) => Promise<void>
  /** Load the conversation's persisted messages from ReTrace's DB into the
   * session view. Marks them all as `isLegacy: true` so a subsequent
   * `createSession` + live stream keeps them visible above the new messages. */
  loadFromDb: (conversationId: string) => Promise<void>
  sendMessage: (conversationId: string, content: string, parts?: SendPart[]) => Promise<void>
  stopMessage: (conversationId: string) => void
  /** Drop a queued message by id (the `×` on a queue chip). */
  cancelQueuedMessage: (conversationId: string, queuedId: string) => void
  /** Internal: pop the head of `queuedMessages` and dispatch it via sendMessage.
   * Exposed on the public interface only because the SSE-done branch + stopMessage
   * both need to call it. Not meant for use by components. */
  _drainQueue: (conversationId: string) => void
  undo: (conversationId: string) => Promise<void>
  redo: (conversationId: string) => Promise<void>
  /** Revert the conversation to before `messageId` without sending anything
   * else. Wires the per-message "Revert message" context menu item. */
  revertToMessage: (conversationId: string, messageId: string) => Promise<void>
  refreshMessages: (conversationId: string) => Promise<void>
  fetchCost: (conversationId: string) => Promise<void>
  clearSession: (conversationId: string) => void
  forkSession: (conversationId: string, messageId?: string) => Promise<{ sessionId: string | null; conversationId: string | null } | null>
  compactSession: (conversationId: string) => Promise<void>
  /** Generate a public share URL for the session. Returns the URL (or null). */
  shareSession: (conversationId: string) => Promise<string | null>
  /** Revoke the share URL. */
  unshareSession: (conversationId: string) => Promise<void>
  /** Trigger OC's session digest. Returns the digest text (or null). */
  summarizeSession: (conversationId: string) => Promise<string | null>
  injectPart: (conversationId: string, part: SOPPart | DocPart) => void
  updatePart: (conversationId: string, partId: string, updates: Partial<SOPPart> | Partial<DocPart>) => void
  respondPermission: (conversationId: string, response: 'once' | 'always' | 'reject') => Promise<void>
  /** Submit answers for OC's `question` tool. `answers` is an array (one inner
   * array per question) of selected option labels (or the literal text typed
   * into the "Other (specify)" custom field). */
  replyToQuestion: (conversationId: string, answers: string[][]) => Promise<void>
  /** Dismiss the pending question — the agent will see a tool error. */
  rejectQuestion: (conversationId: string) => Promise<void>
  setAgent: (conversationId: string, agent: 'build' | 'plan') => void
  /** Set the "Thinking" reasoning mode for a conversation. Unlike `agent`, this
   * can change at any time — it only affects the next message sent. */
  setReasoning: (conversationId: string, reasoning: 'off' | 'think') => void
  /** Close every local SSE reader. OpenCode continues generating upstream. */
  abortAllStreams: () => void
}

// ---------------------------------------------------------------------------
// Module-level mutable stream tracking (not in Zustand state — these hold
// AbortControllers and timers that don't serialize cleanly)
// ---------------------------------------------------------------------------

interface StreamControl {
  abort: AbortController
  generation: number
  stuckTimeout: ReturnType<typeof setTimeout> | null
}

const _streamsByConvo = new Map<string, StreamControl>()
let _msgCounter = 0

// Idle timeout: fires only after this many ms with ZERO inbound events from
// OpenCode. The timer resets on every received SSE event, so legitimate long-
// running tool calls (web research, big bash, screenops) don't trip it as long
// as OC is emitting progress. 5 minutes of complete silence is treated as a
// genuine hang.
const _STUCK_TIMEOUT_MS = 5 * 60_000

function _cancelStream(conversationId: string) {
  const ctrl = _streamsByConvo.get(conversationId)
  if (!ctrl) return
  if (ctrl.stuckTimeout) clearTimeout(ctrl.stuckTimeout)
  try { ctrl.abort.abort() } catch { /* ignore */ }
  _streamsByConvo.delete(conversationId)
}

function _newStream(conversationId: string): StreamControl {
  _cancelStream(conversationId)
  const ctrl: StreamControl = {
    abort: new AbortController(),
    generation: Date.now() + Math.floor(Math.random() * 1000),
    stuckTimeout: null,
  }
  _streamsByConvo.set(conversationId, ctrl)
  return ctrl
}

function _armStuckTimeout(conversationId: string, ctrl: StreamControl) {
  if (ctrl.stuckTimeout) clearTimeout(ctrl.stuckTimeout)
  ctrl.stuckTimeout = setTimeout(() => {
    // Only fire if this is still the current stream for the conversation.
    const current = _streamsByConvo.get(conversationId)
    if (current !== ctrl) return
    console.warn('[OpenCode] Idle timeout — no events for 5 min, aborting upstream', { conversationId })
    const sess = useOpenCodeStore.getState().sessions[conversationId]
    const ocSessionId = sess?.sessionId
    // Fire-and-forget upstream abort. If it fails, local state still gets cleared
    // below — the user is unblocked either way.
    if (ocSessionId) {
      agentApi.opencode.abort(ocSessionId).catch(err => {
        console.warn('[OpenCode] upstream abort on idle failed', err)
      })
    }
    try { ctrl.abort.abort() } catch { /* ignore */ }
    _streamsByConvo.delete(conversationId)
    useOpenCodeStore.getState()._patchSession(conversationId, s => ({
      ...s,
      messages: s.messages.map(m => m.isStreaming ? { ...m, isStreaming: false } : m),
      isRunning: false,
    }))
  }, _STUCK_TIMEOUT_MS)
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

type StoreInternal = OpenCodeState & {
  _patchSession: (conversationId: string, updater: (sess: SessionState) => SessionState) => void
  _ensureSession: (conversationId: string, agent?: 'build' | 'plan') => SessionState
}

export const useOpenCodeStore = create<StoreInternal>((set, get) => ({
  // ── Global state ──────────────────────────────────────────────────────
  isStarting: false,
  externalMode: false,

  sessions: {},
  activeConversationId: null,

  // ── Internal helpers ──────────────────────────────────────────────────
  _patchSession: (conversationId, updater) => {
    set(state => {
      const existing = state.sessions[conversationId]
      if (!existing) return state
      return {
        sessions: {
          ...state.sessions,
          [conversationId]: updater(existing),
        },
      }
    })
  },

  _ensureSession: (conversationId, agent = 'build') => {
    const existing = get().sessions[conversationId]
    if (existing) return existing
    const fresh = makeSession(conversationId, agent)
    set(state => ({ sessions: { ...state.sessions, [conversationId]: fresh } }))
    return fresh
  },

  // ── Server-level ──────────────────────────────────────────────────────
  startServer: async (teamId?: string) => {
    set({ isStarting: true })
    try {
      await agentApi.opencode.start(teamId)
    } finally {
      set({ isStarting: false })
    }
  },

  stopServer: async () => {
    // Try the backend stop. If the request fails (network blip, backend down,
    // 502), we still clear local state — the user explicitly clicked Stop, so
    // the UI should reflect that intent rather than stay in a confusing
    // "still running" state. Any actually-orphaned OS process can be cleaned
    // up the next time the user starts again (start_for_user resets state).
    try {
      await agentApi.opencode.stop()
    } catch (err) {
      console.warn('[OC] stopServer request failed; clearing local state anyway', err)
    }
    // Cancel every in-flight stream and clear every session — the upstream
    // OC server is (or should be) dead, every session ID is invalid.
    for (const cid of Array.from(_streamsByConvo.keys())) _cancelStream(cid)
    set({ sessions: {}, activeConversationId: null })
  },

  checkStatus: async () => {
    const status = await agentApi.opencode.getStatus()
    // Latch the external-mode flag — won't change during the page's lifetime
    // (it's a backend env var), so first response wins.
    if (status.external !== undefined) set({ externalMode: !!status.external })
    return { running: status.running, healthy: status.healthy }
  },

  regenerateConfig: async (teamId?: string) => {
    await agentApi.opencode.regenerateConfig(teamId)
  },

  // ── Active conversation tracking ──────────────────────────────────────
  setActiveConversation: (conversationId: string | null) => {
    set({ activeConversationId: conversationId })
  },

  // ── Per-conversation actions ──────────────────────────────────────────
  createSession: async (conversationId, teamId, agent) => {
    const mode: 'build' | 'plan' = agent ?? get().sessions[conversationId]?.agent ?? 'build'
    // Cold-start resilience: right after opening the agent the engine (OpenCode)
    // may still be booting, so the first create can fail transiently (5xx / no
    // response). Retry a few times — the engine usually warms within a couple
    // seconds — so a startup race self-heals instead of surfacing an error.
    let result!: Awaited<ReturnType<typeof agentApi.opencode.createSession>>
    for (let attempt = 0; ; attempt++) {
      try {
        result = await agentApi.opencode.createSession(teamId, conversationId, mode === 'plan' ? 'plan' : undefined)
        break
      } catch (err: any) {
        const status = err?.response?.status
        const engineWarming = status === undefined || status >= 500
        if (!engineWarming || attempt >= 3) throw err
        await new Promise(resolve => setTimeout(resolve, 800 * (attempt + 1)))
      }
    }
    // Preserve any legacy messages already loaded into the view (e.g. via
    // loadFromDb after a SESSION_NOT_FOUND). Non-legacy messages are wiped
    // because they belonged to the dead/previous OC session.
    const prevLegacy = (get().sessions[conversationId]?.messages || []).filter(m => m.isLegacy)
    set(state => ({
      sessions: {
        ...state.sessions,
        [conversationId]: {
          ...(state.sessions[conversationId] || makeSession(conversationId, mode)),
          sessionId: result.opencode_session_id,
          messages: prevLegacy,
          canUndo: false,
          canRedo: false,
          sessionCost: null,
          pendingPermission: null,
          pendingQuestion: null,
          agent: mode,
          queuedMessages: [],
          recentPrompts: state.sessions[conversationId]?.recentPrompts ?? [],
        },
      },
    }))
    await get().refreshMessages(conversationId)
    return result.opencode_session_id
  },

  loadSession: async (conversationId, opencodeSessionId) => {
    // Verify the OC session exists upstream before adopting it locally. After a
    // sidecar restart, OC session ids saved in sessionStorage / DB become stale
    // and produce empty chats with no error. We surface a 404 explicitly so the
    // caller can fall back to creating a fresh session and re-linking the
    // conversation.
    try {
      await agentApi.opencode.getSession(opencodeSessionId)
    } catch (err: any) {
      if (err?.response?.status === 404) {
        console.warn('[OC] loadSession: upstream 404', { opencodeSessionId, conversationId })
        throw new Error('SESSION_NOT_FOUND')
      }
      // Transient (network blip, timeout) — proceed optimistically, refreshMessages
      // will surface the real problem if it persists.
      console.warn('[OC] loadSession: upstream check failed (transient?), proceeding', err)
    }
    // Preserve any legacy messages already loaded; the new OC session may
    // build on top of them, and the user just saw them.
    const prevLegacy = (get().sessions[conversationId]?.messages || []).filter(m => m.isLegacy)
    set(state => ({
      sessions: {
        ...state.sessions,
        [conversationId]: {
          ...(state.sessions[conversationId] || makeSession(conversationId)),
          sessionId: opencodeSessionId,
          messages: prevLegacy,
          canUndo: false,
          canRedo: false,
          sessionCost: null,
          pendingPermission: null,
          pendingQuestion: null,
          queuedMessages: [],
        },
      },
    }))
    // Pull DB history first (will be marked isLegacy). refreshMessages then
    // dedups against OC's live response, so anything the current session
    // produced isn't shown twice. Net effect: legacy from earlier sessions
    // shows above OC's live messages, even after page reloads.
    if (prevLegacy.length === 0) {
      await get().loadFromDb(conversationId)
      // loadFromDb sets sessionId=null; restore it before refresh.
      set(state => ({
        sessions: {
          ...state.sessions,
          [conversationId]: {
            ...(state.sessions[conversationId] || makeSession(conversationId)),
            sessionId: opencodeSessionId,
          },
        },
      }))
    }
    await get().refreshMessages(conversationId)
    // Rehydrate any pending `question` tool request — the SSE `question.asked`
    // event was emitted before this page mounted, so the live event handler
    // never fires for it. Without this, the user would see the question's
    // tool block in display-only mode (no radios, no submit) even though OC
    // is still parked on the reply.
    try {
      const pending = await agentApi.opencode.listPendingQuestions()
      const match = pending.find(p => p.sessionID === opencodeSessionId)
      if (match) {
        get()._patchSession(conversationId, s => ({
          ...s,
          pendingQuestion: {
            id: match.id,
            sessionID: match.sessionID,
            questions: match.questions || [],
            tool: match.tool,
          },
        }))
      }
    } catch (err) {
      console.warn('[OC] rehydrate pending questions failed', err)
    }
  },

  loadFromDb: async (conversationId: string) => {
    try {
      const convo = await agentApi.getConversation(conversationId)
      const rows = convo?.messages || []
      const mapped: OCMessage[] = []
      let pendingToolMsg: OCMessage | null = null

      for (const row of rows) {
        const meta = (row.meta as Record<string, any>) || {}
        const id = `db-${row.message_id}`

        if (row.type === 'user') {
          pendingToolMsg = null
          // Restore the `synthetic` flag persisted by session_bridge so
          // fire-injected user bubbles (scheduled / monitor / webhook /
          // task-completion) render with the system badge after a reload,
          // not as user-typed.
          const isSyntheticRow = meta.synthetic === true
          mapped.push({
            id,
            role: 'user',
            // OC part id isn't stored separately for user messages, so use the
            // DB row id as a stable synthetic part id.
            parts: [{
              type: 'text',
              id: `${id}:text`,
              text: row.content,
              ...(isSyntheticRow ? { synthetic: true } : {}),
            }],
            isLegacy: true,
          })
        } else if (row.type === 'answer') {
          pendingToolMsg = null
          mapped.push({
            id,
            role: 'assistant',
            // Use the raw OC part id (= row.message_id) so refreshMessages can
            // dedup this against the live OC response after the session is
            // restored.
            parts: [{ type: 'text', id: row.message_id, text: row.content }],
            model: meta.model,
            cost: meta.cost,
            tokens: meta.tokens,
            isLegacy: true,
          })
        } else if (row.type === 'thinking') {
          pendingToolMsg = null
          mapped.push({
            id,
            role: 'assistant',
            parts: [{ type: 'thinking', id: row.message_id, text: row.content }],
            isLegacy: true,
          })
        } else if (row.type === 'code') {
          const ocPartId = meta.oc_part_id || row.message_id
          let input: any = row.content
          try { input = JSON.parse(row.content) } catch { /* keep as string */ }
          pendingToolMsg = {
            id,
            role: 'assistant',
            parts: [{
              type: 'tool',
              id: ocPartId,
              tool: meta.tool || '',
              callID: '',
              state: { status: meta.status === 'error' ? 'error' : 'completed', input, output: '' },
            }],
            isLegacy: true,
          }
          mapped.push(pendingToolMsg)
        } else if (row.type === 'output') {
          const ocPartId = meta.oc_part_id
          if (pendingToolMsg && ocPartId) {
            // Attach to the matching tool part on the pending message.
            pendingToolMsg.parts = pendingToolMsg.parts.map(p => {
              if (p.type === 'tool' && p.id === ocPartId) {
                return { ...p, state: { ...p.state, output: row.content, status: meta.status === 'error' ? 'error' : 'completed' } }
              }
              return p
            })
          } else {
            // Orphan output (call row missing). Render as plain text.
            mapped.push({
              id,
              role: 'assistant',
              parts: [{ type: 'text', id: `${id}:text`, text: row.content }],
              isLegacy: true,
            })
          }
        } else if (row.type === 'sop') {
          const sopId = meta.sop_id || row.message_id
          const title = meta.title || ''
          const sopPart: Part = { type: 'sop', id, sopId, title, content: row.content, approved: meta.approved }
          const lastAssist = [...mapped].reverse().find(m => m.role === 'assistant')
          if (lastAssist) lastAssist.parts.push(sopPart)
          else mapped.push({ id, role: 'assistant', parts: [sopPart], isLegacy: true })
        } else if (row.type === 'doc') {
          const docId = meta.doc_id || row.message_id
          const title = meta.title || ''
          const docPart: Part = { type: 'doc', id, docId, title, content: row.content, approved: meta.approved }
          const lastAssist = [...mapped].reverse().find(m => m.role === 'assistant')
          if (lastAssist) lastAssist.parts.push(docPart)
          else mapped.push({ id, role: 'assistant', parts: [docPart], isLegacy: true })
        }
        // Skip other types (status, tools, error, done) — those were transient
        // UI events in the legacy CodeAct schema, not durable conversation state.
      }

      set(state => ({
        sessions: {
          ...state.sessions,
          [conversationId]: {
            ...(state.sessions[conversationId] || makeSession(conversationId)),
            // OC session is dead — caller will createSession later if needed.
            // Keep sessionId null so the input area's gating knows there's no
            // live session yet.
            sessionId: null,
            messages: mapped,
          },
        },
      }))
    } catch (err) {
      console.error('[OC] loadFromDb failed', err)
    }
  },

  _drainQueue: (conversationId: string) => {
    // Shift the head off `queuedMessages` and re-enter `sendMessage` with it.
    // Used by the SSE-done branch, the post-stream cleanup, and `stopMessage`
    // so a queued message can't get stranded.
    const queueHead = get().sessions[conversationId]?.queuedMessages?.[0]
    if (!queueHead) return
    get()._patchSession(conversationId, s => ({
      ...s,
      queuedMessages: s.queuedMessages.slice(1),
    }))
    // Microtask defer so the previous turn's state settles fully before the
    // next user-message bubble appears.
    Promise.resolve().then(() => {
      get().sendMessage(conversationId, queueHead.text, queueHead.parts).catch(err => {
        console.warn('[OC] Drained queued message failed', err)
      })
    })
  },

  sendMessage: async (conversationId, content, parts) => {
    let sess = get().sessions[conversationId]
    let sessionId = sess?.sessionId

    // Heal a dead or missing OpenCode session before prompting. After a sidecar
    // restart (including a scale-to-zero wake) OpenCode loses its in-memory
    // sessions, so a stored session id 404s and the prompt fails upstream with
    // "Session not found" — surfaced to the user as "async failed". Verify the
    // session exists; if it's gone (404) or was never created, mint a fresh one
    // (which re-links the conversation) and continue with that session id.
    let sessionAlive = false
    if (sessionId) {
      try {
        await agentApi.opencode.getSession(sessionId)
        sessionAlive = true
      } catch (err: any) {
        // Only an explicit 404 means dead; transient errors proceed as-is.
        sessionAlive = err?.response?.status !== 404
      }
    }
    if (!sessionId || !sessionAlive) {
      console.warn('[OC] sendMessage: no live OpenCode session — creating a fresh one', { conversationId, deadSessionId: sessionId ?? null })
      sessionId = await get().createSession(conversationId, undefined, sess?.agent)
      sess = get().sessions[conversationId]
    }
    if (!sess || !sessionId) throw new Error('No active OpenCode session for this conversation')

    // Always record what the user submitted into the per-session prompt history
    // — even if the message ends up queued. Caps at 50 and ignores empty input.
    if (content.trim()) {
      get()._patchSession(conversationId, s => {
        const next = [...s.recentPrompts, content]
        return { ...s, recentPrompts: next.length > 50 ? next.slice(next.length - 50) : next }
      })
    }

    // If a run is already in flight for this conversation, queue this message
    // for FIFO dispatch when the current turn ends. The `done` branch of the
    // SSE loop (or the post-stream cleanup) shifts the head and calls
    // sendMessage again with it. Matches OC's `pendingMessages` behavior.
    if (sess.isRunning) {
      get()._patchSession(conversationId, s => ({
        ...s,
        queuedMessages: [
          ...s.queuedMessages,
          { id: `queued-${++_msgCounter}`, text: content, parts },
        ],
      }))
      console.log('[OC] sendMessage QUEUED', { conversationId, queueLength: get().sessions[conversationId]?.queuedMessages?.length })
      return
    }

    console.log('[OC] sendMessage START', { conversationId, sessionId, contentLength: content.length })

    const ctrl = _newStream(conversationId)
    const myGen = ctrl.generation

    const userMsgId = `msg-${++_msgCounter}`
    const assistantMsgId = `msg-${++_msgCounter}`

    // Build the optimistic user-message parts: the text part first, then any
    // attachment file-parts. MessageList renders the file parts above the text.
    // Agent-mention parts (`type:"agent"`) are not rendered as part of the
    // user bubble — they're metadata for the LLM; the visible `@build` already
    // sits inside the text part.
    const optimisticUserParts: Part[] = [
      { type: 'text' as const, id: `part-${++_msgCounter}`, text: content },
      ...(parts ?? [])
        .filter((p): p is FilePart => p.type === 'file')
        .map<AttachmentPart>(p => ({
          type: 'file' as const,
          id: `part-${++_msgCounter}`,
          mime: p.mime,
          filename: p.filename,
          url: p.url,
        })),
    ]

    get()._patchSession(conversationId, s => ({
      ...s,
      messages: [...s.messages.filter(m => !m.isStreaming), {
        id: userMsgId,
        role: 'user' as const,
        parts: optimisticUserParts,
      }, {
        id: assistantMsgId,
        role: 'assistant' as const,
        parts: [],
        isStreaming: true,
      }],
      isRunning: true,
      canUndo: false,
      canRedo: false,
    }))

    _armStuckTimeout(conversationId, ctrl)

    const isCurrent = () => _streamsByConvo.get(conversationId)?.generation === myGen

    try {
      const authHeaders = await _getAuthHeaders()
      const url = agentApi.opencode.getMessageUrl(sessionId)
      // Body shape: `{content, parts?, agent?}`. The backend forwards `parts`
      // to OC's prompt_async after concatenating the text part. `agent`
      // carries the per-turn Build/Plan choice (OC accepts an `agent` field
      // on each prompt_async — same session, different agent per turn — no
      // session recreation needed for mid-conversation switches).
      const sessionAgent = get().sessions[conversationId]?.agent
      const sessionReasoning = get().sessions[conversationId]?.reasoning
      const bodyObj: { content: string; parts?: SendPart[]; agent?: string; reasoning?: string } = { content }
      if (parts && parts.length > 0) bodyObj.parts = parts
      if (sessionAgent === 'plan' || sessionAgent === 'build') bodyObj.agent = sessionAgent
      // Only send `reasoning` when the user has turned Thinking on, so requests
      // are byte-identical to today's when the toggle is off (backward-compatible).
      if (sessionReasoning === 'think') bodyObj.reasoning = sessionReasoning
      const body = JSON.stringify(bodyObj)
      console.log('[OC] POST', url, parts ? `(with ${parts.length} attachment(s))` : '')

      let response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders, Accept: 'text/event-stream' },
        body,
        signal: ctrl.abort.signal,
      })

      if (response.status === 401) {
        const refreshed = await _refreshToken()
        if (refreshed) {
          const newHeaders = await _getAuthHeaders()
          response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...newHeaders, Accept: 'text/event-stream' },
            body,
            signal: ctrl.abort.signal,
          })
        }
      }

      if (!response.ok || !response.body) {
        console.error('[OC] Bad response', { status: response.status })
        if (!isCurrent()) return
        get()._patchSession(conversationId, s => ({
          ...s,
          messages: s.messages.map(m =>
            m.id === assistantMsgId
              ? { ...m, parts: [{ type: 'text', id: 'part-err', text: `Error: ${response.status} ${response.statusText}` }], isStreaming: false }
              : m
          ),
          isRunning: false,
        }))
        return
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let currentAssistantId = assistantMsgId
      let eventCount = 0

      while (true) {
        const { done, value } = await reader.read()
        if (done) { console.log('[OC] Stream done (reader)'); break }
        if (!isCurrent()) {
          reader.cancel()
          console.log('[OC] Stream cancelled (generation mismatch)', { conversationId })
          break
        }

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (!line.startsWith('data:')) continue
          const dataStr = line.slice(5).trim()
          if (!dataStr) continue

          // Per-line guard so dozens of stale events from a cancelled stream
          // can't slip through after `reader.cancel()`.
          if (!isCurrent()) break

          // Any event from OpenCode = not stuck. Re-arm the 5-min idle timer.
          _armStuckTimeout(conversationId, ctrl)

          try {
            const data = JSON.parse(dataStr)
            eventCount++

            if (data.type === 'message.part.created') {
              const part = data.part
              if (part?.type === 'text') {
                const partId = part.id || ''
                // OpenCode emits `part.text` as the FULL accumulated text on
                // each `message.part.updated` event, not a delta. So
                // replacing (not appending) is correct — verified against
                // OC's TextPart schema.
                get()._patchSession(conversationId, s => ({
                  ...s,
                  messages: s.messages.map(m => {
                    if (m.id !== currentAssistantId) return m
                    const existingIdx = m.parts.findIndex(p => p.type === 'text' && p.id === partId)
                    if (existingIdx >= 0) {
                      const newParts = [...m.parts]
                      newParts[existingIdx] = { type: 'text' as const, id: partId, text: part.text || '' }
                      return { ...m, parts: newParts }
                    }
                    return { ...m, parts: [...m.parts, { type: 'text' as const, id: partId, text: part.text || '' }] }
                  }),
                }))
              } else if (part?.type === 'tool') {
                const toolOutput = typeof part.output === 'string' ? part.output : JSON.stringify(part.output || '', null, 2)
                get()._patchSession(conversationId, s => ({
                  ...s,
                  messages: s.messages.map(m => {
                    if (m.id !== currentAssistantId) return m
                    const existingIdx = m.parts.findIndex(p => p.type === 'tool' && p.id === (part.id || ''))
                    if (existingIdx >= 0) {
                      const existing = m.parts[existingIdx]
                      if (existing.type === 'tool') {
                        const updated = { ...existing, state: { ...existing.state, status: part.status === 'completed' ? 'completed' as const : part.status === 'error' ? 'error' as const : existing.state.status, output: toolOutput || existing.state.output, input: part.input || existing.state.input } }
                        const newParts = [...m.parts]
                        newParts[existingIdx] = updated
                        return { ...m, parts: newParts }
                      }
                    }
                    const toolPart: ToolPart = {
                      type: 'tool',
                      id: part.id || `tp-${++_msgCounter}`,
                      tool: part.tool || '',
                      callID: part.callID || '',
                      title: part.title,
                      state: {
                        status: part.status === 'completed' ? 'completed' : 'running',
                        input: part.input,
                        output: toolOutput,
                      },
                    }
                    return { ...m, parts: [...m.parts, toolPart] }
                  }),
                }))
              } else if (part?.type === 'thinking') {
                const partId = part.id || `tk-${++_msgCounter}`
                get()._patchSession(conversationId, s => ({
                  ...s,
                  messages: s.messages.map(m => {
                    if (m.id !== currentAssistantId) return m
                    const existing = m.parts.find(p => p.type === 'thinking' && p.id === partId)
                    if (existing && existing.type === 'thinking') {
                      return { ...m, parts: m.parts.map(p => p.id === partId && p.type === 'thinking' ? { ...p, text: part.text || '' } : p) }
                    }
                    return { ...m, parts: [...m.parts, { type: 'thinking' as const, id: partId, text: part.text || '' }] }
                  }),
                }))
              }
            } else if (data.type === 'message.part.delta') {
              // Token-by-token stream chunk from OpenCode.
              // Backend forwards OC's native `message.part.delta` payload as
              // `{partID, messageID, field, delta}`. We append `delta` to the
              // text/thinking part identified by `partID`, creating that part
              // if it doesn't exist yet (deltas arrive before the matching
              // `message.part.updated` "part created" event).
              const partId: string = data.partID || ''
              const field: string = data.field || ''
              const delta: string = data.delta || ''
              if (!partId || !delta) {
                // ignore malformed
              } else if (field === 'text') {
                get()._patchSession(conversationId, s => ({
                  ...s,
                  messages: s.messages.map(m => {
                    if (m.id !== currentAssistantId) return m
                    const idx = m.parts.findIndex(p => p.type === 'text' && p.id === partId)
                    if (idx >= 0) {
                      const existing = m.parts[idx]
                      if (existing.type === 'text') {
                        const newParts = [...m.parts]
                        newParts[idx] = { ...existing, text: (existing.text || '') + delta }
                        return { ...m, parts: newParts }
                      }
                    }
                    return { ...m, parts: [...m.parts, { type: 'text' as const, id: partId, text: delta }] }
                  }),
                }))
              } else if (field === 'thinking' || field === 'reasoning') {
                // Treat 'reasoning' as an alias for 'thinking' — some
                // extended-thinking models (e.g. Grok) emit deltas with
                // `field:"reasoning"` while OC's part `type` for both is
                // `thinking`. Without this alias the reasoning panel would
                // snap into place at the end instead of streaming.
                get()._patchSession(conversationId, s => ({
                  ...s,
                  messages: s.messages.map(m => {
                    if (m.id !== currentAssistantId) return m
                    const idx = m.parts.findIndex(p => p.type === 'thinking' && p.id === partId)
                    if (idx >= 0) {
                      const existing = m.parts[idx]
                      if (existing.type === 'thinking') {
                        const newParts = [...m.parts]
                        newParts[idx] = { ...existing, text: (existing.text || '') + delta }
                        return { ...m, parts: newParts }
                      }
                    }
                    return { ...m, parts: [...m.parts, { type: 'thinking' as const, id: partId, text: delta }] }
                  }),
                }))
              }
            } else if (data.type === 'message.completed') {
              const finish = data.message?.finish
              if (finish && finish !== 'stop') {
                // Continuation (tool-use turn boundary, etc.) — close the
                // current assistant message before opening the new one so we
                // don't end up with two simultaneous "typing dots" indicators.
                const prevAssistantId = currentAssistantId
                const newAssistantId = `msg-${++_msgCounter}`
                currentAssistantId = newAssistantId
                get()._patchSession(conversationId, s => ({
                  ...s,
                  messages: [
                    ...s.messages.map(m =>
                      m.id === prevAssistantId && m.isStreaming
                        ? { ...m, isStreaming: false }
                        : m
                    ),
                    {
                      id: newAssistantId,
                      role: 'assistant' as const,
                      parts: [],
                      isStreaming: true,
                    },
                  ],
                }))
              }
            } else if (data.type === 'error') {
              console.error('[OC] EVT error', { error: data.error, conversationId })
              get()._patchSession(conversationId, s => ({
                ...s,
                messages: s.messages.map(m => {
                  if (m.id !== currentAssistantId) return m
                  const hasText = m.parts.some(p => p.type === 'text')
                  if (hasText) return { ...m, isStreaming: false }
                  return { ...m, parts: [...m.parts, { type: 'text' as const, id: 'part-err', text: `Error: ${data.error || 'Unknown error'}` }], isStreaming: false }
                }),
              }))
            } else if (data.type === 'session.error') {
              // OC-level error: provider HTTP failure, MCP server crash, etc.
              // Show a toast AND inline the message in the assistant bubble so
              // the user has a permanent record after the toast fades.
              const msg = (data.message as string) || 'Session error'
              const code = (data.code as string) || ''
              console.error('[OC] EVT session.error', { code, msg, conversationId })
              toast.error(msg.length > 200 ? msg.slice(0, 200) + '…' : msg)
              get()._patchSession(conversationId, s => ({
                ...s,
                messages: s.messages.map(m => {
                  if (m.id !== currentAssistantId) return m
                  return {
                    ...m,
                    parts: [...m.parts, { type: 'text' as const, id: `part-err-${Date.now()}`, text: `**Error${code ? ` (${code})` : ''}:** ${msg}` }],
                    isStreaming: false,
                  }
                }),
                isRunning: false,
              }))
            } else if (data.type === 'message.part.removed') {
              // OC removed a part from a message (revert / fork cleanup).
              // Drop it from the local message tree so the UI doesn't show
              // a part that no longer exists upstream.
              const msgId = data.messageID as string
              const partId = data.partID as string
              console.log('[OC] EVT message.part.removed', { msgId, partId, conversationId })
              get()._patchSession(conversationId, s => ({
                ...s,
                messages: s.messages.map(m =>
                  m.id === msgId
                    ? { ...m, parts: m.parts.filter(p => p.id !== partId) }
                    : m
                ),
              }))
            } else if (data.type === 'permission.asked') {
              const perm = data.permission || {}
              console.log('[OC] EVT permission.asked', { id: perm.id, tool: perm.tool, conversationId })
              get()._patchSession(conversationId, s => ({
                ...s,
                pendingPermission: { id: perm.id, tool: perm.tool || '', input: perm.input || {} },
              }))
            } else if (data.type === 'question.asked') {
              const req = data.request || {}
              console.log('[OC] EVT question.asked', { id: req.id, count: req.questions?.length, conversationId })
              get()._patchSession(conversationId, s => ({
                ...s,
                pendingQuestion: {
                  id: req.id || '',
                  sessionID: req.sessionID || '',
                  questions: Array.isArray(req.questions) ? req.questions : [],
                  tool: req.tool || undefined,
                },
              }))
            } else if (data.type === 'question.replied' || data.type === 'question.rejected') {
              console.log('[OC] EVT', data.type, { requestID: data.requestID, conversationId })
              get()._patchSession(conversationId, s => {
                if (s.pendingQuestion && s.pendingQuestion.id === data.requestID) {
                  return { ...s, pendingQuestion: null }
                }
                return s
              })
            } else if (data.type === 'conversation.updated') {
              // Backend emits this from the post-stream sync once OC's
              // LLM-generated title (e.g. "Greeting") lands — so the
              // conversation pill can upgrade from the truncation stopgap
              // ("hello") in the same turn, with no extra refetch heuristic.
              // Dispatched as a window CustomEvent so Agent.tsx can pick it
              // up without the store needing a queryClient reference.
              console.log('[OC] EVT conversation.updated', data)
              try {
                window.dispatchEvent(new CustomEvent('retrace:conversation-updated', {
                  detail: { conversationId: data.conversation_id, title: data.title },
                }))
              } catch (e) {
                console.warn('[OC] conversation.updated dispatch failed', e)
              }
            } else if (data.type === 'done') {
              console.log('[OC] EVT done', { totalEvents: eventCount, conversationId })
              reader.cancel()
              break
            }
          } catch (e) {
            console.warn('[OC] SSE parse error', { line: dataStr.slice(0, 80), error: e })
          }
        }
      }

      if (isCurrent()) {
        _streamsByConvo.delete(conversationId)
        console.log('[OC] Stream finished, refreshing messages...', { conversationId })
        await get().refreshMessages(conversationId)
        await get().fetchCost(conversationId)
        get()._patchSession(conversationId, s => ({
          ...s,
          messages: s.messages.map(m => m.isStreaming ? { ...m, isStreaming: false } : m),
          isRunning: false,
          canUndo: true,
        }))

        // Drain one queued message FIFO (chains: each turn's drain fires next).
        get()._drainQueue(conversationId)
      }
    } catch (err: any) {
      if (err?.name === 'AbortError') {
        // Caller (or a newer sendMessage) cancelled this stream — leave state alone for the new stream.
        console.log('[OC] sendMessage aborted', { conversationId })
        return
      }
      console.error('[OC] sendMessage ERROR', err)
      if (!isCurrent()) return
      _streamsByConvo.delete(conversationId)
      get()._patchSession(conversationId, s => ({
        ...s,
        messages: s.messages.map(m =>
          m.id === assistantMsgId
            ? { ...m, parts: [{ type: 'text' as const, id: 'part-err', text: `Connection error: ${err}` }], isStreaming: false }
            : m
        ),
        isRunning: false,
      }))
    }
  },

  stopMessage: (conversationId: string) => {
    // Fire the upstream abort first so OpenCode actually stops generating.
    // Local cancellation alone leaves OC churning through the prompt and racking
    // up token cost. We don't await — the abort is fire-and-forget; the local
    // teardown below disconnects us regardless of whether abort reaches OC.
    const ocSessionId = get().sessions[conversationId]?.sessionId
    if (ocSessionId) {
      agentApi.opencode.abort(ocSessionId).catch(err => {
        console.warn('[OpenCode] upstream abort failed', err)
      })
    }
    _cancelStream(conversationId)
    get()._patchSession(conversationId, s => ({
      ...s,
      messages: s.messages.map(m => m.isStreaming ? { ...m, isStreaming: false } : m),
      isRunning: false,
    }))
    // Stop only aborts the current turn — queued messages from before the
    // stop continue. If the user wants to drop them, they can `×` the chips.
    get()._drainQueue(conversationId)
  },

  cancelQueuedMessage: (conversationId, queuedId) => {
    get()._patchSession(conversationId, s => ({
      ...s,
      queuedMessages: s.queuedMessages.filter(q => q.id !== queuedId),
    }))
  },

  /** Disconnect every local SSE reader without telling OpenCode to abort.
   * OC keeps generating in the background; the next `refreshMessages` call for
   * a given conversation picks up the latest state. Used by Agent.tsx on
   * unmount so streams don't tie up local fetch resources after the page is
   * gone, while preserving in-flight work upstream.
   *
   * Marks every previously-running session as not-running locally — without
   * this, the conversation chip's spinner would stay on after the user
   * returns, even though we no longer have a fetch attached to update state. */
  abortAllStreams: () => {
    const wasRunning = Array.from(_streamsByConvo.keys())
    for (const cid of wasRunning) _cancelStream(cid)
    if (wasRunning.length === 0) return
    set(state => {
      const next = { ...state.sessions }
      for (const cid of wasRunning) {
        const s = next[cid]
        if (!s) continue
        next[cid] = {
          ...s,
          isRunning: false,
          messages: s.messages.map(m => m.isStreaming ? { ...m, isStreaming: false } : m),
        }
      }
      return { sessions: next }
    })
  },

  undo: async (conversationId: string) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId) return
    try {
      await agentApi.opencode.revert(sess.sessionId)
      await get().refreshMessages(conversationId)
      get()._patchSession(conversationId, s => ({ ...s, canRedo: true }))
    } catch { /* ignore */ }
  },

  redo: async (conversationId: string) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId) return
    try {
      await agentApi.opencode.unrevert(sess.sessionId)
      await get().refreshMessages(conversationId)
      get()._patchSession(conversationId, s => ({ ...s, canUndo: true, canRedo: false }))
    } catch { /* ignore */ }
  },

  revertToMessage: async (conversationId: string, messageId: string) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId) return
    try {
      await agentApi.opencode.revert(sess.sessionId, messageId)
      await get().refreshMessages(conversationId)
      get()._patchSession(conversationId, s => ({ ...s, canUndo: false, canRedo: true }))
    } catch (err) {
      console.warn('[OC] revertToMessage failed', err)
    }
  },

  refreshMessages: async (conversationId: string) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId) return
    const sessionId = sess.sessionId
    try {
      const [ocMessages, sessionData] = await Promise.all([
        agentApi.opencode.getMessages(sessionId),
        agentApi.opencode.getSession(sessionId).catch(() => null),
      ])
      const revertMessageId = sessionData?.revert?.messageID || null
      const mapped: OCMessage[] = []
      for (const msg of ocMessages) {
        const info = msg.info || {}
        if (revertMessageId && info.id === revertMessageId) break
        const rawParts = msg.parts || []
        const role = info.role
        if (role === 'user') {
          const parts: Part[] = []
          for (const p of rawParts) {
            if (p.type === 'text') {
              // Preserve OC's `synthetic` marker so fire-injected user
              // bubbles keep their system badge after a refreshMessages
              // overwrite (handleFiredEvent calls refreshMessages on
              // finish=stop; without this the flag we just streamed in is wiped).
              parts.push({
                type: 'text',
                id: p.id || `p-${++_msgCounter}`,
                text: p.text || '',
                ...(p.synthetic === true ? { synthetic: true } : {}),
              })
            }
          }
          if (parts.length > 0) {
            mapped.push({ id: info.id, role: 'user', parts, model: info.modelID })
          }
        } else if (role === 'assistant') {
          const parts: Part[] = []
          for (const p of rawParts) {
            if (p.type === 'text' && p.text) {
              parts.push({ type: 'text', id: p.id || `p-${++_msgCounter}`, text: p.text })
            } else if (p.type === 'tool') {
              const ts = p.state || {}
              const toolOutput = typeof ts.output === 'string' ? ts.output : JSON.stringify(ts.output || '', null, 2)
              const rawStatus = ts.status || 'completed'
              const effectiveStatus = (p.tool === 'question' || p.tool === 'ask_user')
                ? (rawStatus === 'running' ? 'running' : rawStatus === 'error' ? 'error' : 'completed')
                : (rawStatus === 'error' ? 'error' : rawStatus === 'running' ? 'running' : 'completed')
              parts.push({
                type: 'tool',
                id: p.id || `p-${++_msgCounter}`,
                tool: p.tool || '',
                callID: p.callID || '',
                title: p.title,
                state: { status: effectiveStatus, input: ts.input, output: toolOutput },
              })
            } else if (p.type === 'thinking' && p.text) {
              parts.push({ type: 'thinking', id: p.id || `p-${++_msgCounter}`, text: p.text })
            }
          }
          mapped.push({ id: info.id, role: 'assistant', parts, model: info.modelID, cost: info.cost, tokens: info.tokens })
        }
      }

      // Preserve frontend-injected SOP/Doc parts across refreshes. OpenCode
      // doesn't know about them — without this merge, every refresh would
      // wipe out automation/documentation cards the user generated earlier
      // in the conversation.
      const prevById = new Map(sess.messages.map(m => [m.id, m]))
      for (let i = 0; i < mapped.length; i++) {
        const prev = prevById.get(mapped[i].id)
        if (!prev) continue
        const injected = prev.parts.filter(p => p.type === 'sop' || p.type === 'doc')
        if (injected.length > 0) {
          mapped[i] = { ...mapped[i], parts: [...mapped[i].parts, ...injected] }
        }
      }

      // Preserve legacy messages (loaded from DB; from prior sessions or
      // pre-OpenCode history), but drop any whose underlying OC part ids also
      // appear in the live OC response — those are the same content, just
      // duplicated by the DB sync.
      const liveIds = new Set<string>()
      for (const m of mapped) {
        liveIds.add(m.id)
        for (const p of m.parts) {
          if (p.id) liveIds.add(p.id)
        }
      }
      const legacy = sess.messages.filter(m => {
        if (!m.isLegacy) return false
        if (m.role === 'user') {
          // Strip the `db-` prefix that loadFromDb adds to user-message ids.
          const baseId = m.id.startsWith('db-') ? m.id.slice(3) : m.id
          return !liveIds.has(baseId)
        }
        // Assistant: any matching part id means OC owns this message — drop legacy.
        for (const p of m.parts) {
          if (p.id && liveIds.has(p.id)) return false
        }
        return true
      })

      get()._patchSession(conversationId, s => ({
        ...s,
        messages: [...legacy, ...mapped],
        canUndo: !revertMessageId && mapped.length > 0,
        canRedo: !!revertMessageId,
      }))
    } catch { /* ignore */ }
  },

  fetchCost: async (conversationId: string) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId) return
    try {
      const cost = await agentApi.opencode.getCost(sess.sessionId)
      get()._patchSession(conversationId, s => ({ ...s, sessionCost: cost }))
    } catch { /* ignore */ }
  },

  clearSession: (conversationId: string) => {
    _cancelStream(conversationId)
    set(state => {
      const { [conversationId]: _removed, ...rest } = state.sessions
      return {
        sessions: rest,
        activeConversationId: state.activeConversationId === conversationId ? null : state.activeConversationId,
      }
    })
  },

  forkSession: async (conversationId: string, messageId?: string) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId) return null
    try {
      const forked = await agentApi.opencode.forkSession(sess.sessionId, messageId)
      const newSessionId: string | null = forked?.id || null
      const newConversationId: string | null = forked?.conversation_id || null
      if (newSessionId && newConversationId) {
        // The backend created a new Conversation row pointing at the forked
        // OC session. Add a matching SessionState so the UI can address it,
        // then make it the active conversation.
        const parent = get().sessions[conversationId]
        set(state => ({
          sessions: {
            ...state.sessions,
            [newConversationId]: {
              ...makeSession(newConversationId, parent?.agent || 'build'),
              sessionId: newSessionId,
            },
          },
          activeConversationId: newConversationId,
        }))
        // Load the forked session's messages from OC.
        await get().refreshMessages(newConversationId)
      }
      return { sessionId: newSessionId, conversationId: newConversationId }
    } catch { return null }
  },

  compactSession: async (conversationId: string) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId) return
    try {
      await agentApi.opencode.compactSession(sess.sessionId)
      await get().refreshMessages(conversationId)
    } catch { /* ignore */ }
  },

  shareSession: async (conversationId: string) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId) return null
    try {
      const result = await agentApi.opencode.shareSession(sess.sessionId)
      return result?.url || null
    } catch (err) {
      console.warn('[OC] shareSession failed', err)
      return null
    }
  },

  unshareSession: async (conversationId: string) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId) return
    try {
      await agentApi.opencode.unshareSession(sess.sessionId)
    } catch (err) {
      console.warn('[OC] unshareSession failed', err)
    }
  },

  summarizeSession: async (conversationId: string) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId) return null
    try {
      const result = await agentApi.opencode.summarizeSession(sess.sessionId)
      // Refresh in case OC injected the summary into the message stream.
      await get().refreshMessages(conversationId)
      // OC's summarize response shape varies; try a few likely fields.
      if (typeof result === 'string') return result
      if (result?.summary) return String(result.summary)
      if (result?.text) return String(result.text)
      return null
    } catch (err) {
      console.warn('[OC] summarizeSession failed', err)
      return null
    }
  },

  injectPart: (conversationId, part) => {
    get()._patchSession(conversationId, s => {
      const msgs = [...s.messages]
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === 'assistant' && !msgs[i].isStreaming) {
          msgs[i] = { ...msgs[i], parts: [...msgs[i].parts, part] }
          break
        }
      }
      return { ...s, messages: msgs }
    })
  },

  updatePart: (conversationId, partId, updates) => {
    get()._patchSession(conversationId, s => ({
      ...s,
      messages: s.messages.map(m => {
        if (m.role !== 'assistant') return m
        const hasPart = m.parts.some(p => p.id === partId)
        if (!hasPart) return m
        return { ...m, parts: m.parts.map(p => p.id === partId ? { ...p, ...updates } as Part : p) }
      }),
    }))
  },

  respondPermission: async (conversationId, response) => {
    const sess = get().sessions[conversationId]
    if (!sess?.sessionId || !sess.pendingPermission) return
    const permId = sess.pendingPermission.id
    const sessionId = sess.sessionId
    get()._patchSession(conversationId, s => ({ ...s, pendingPermission: null }))
    try {
      await agentApi.opencode.respondPermission(sessionId, permId, response)
      console.log('[OC] permission responded', { permId, response, conversationId })
    } catch (err) {
      console.error('[OC] permission respond error', err)
    }
  },

  replyToQuestion: async (conversationId, answers) => {
    const sess = get().sessions[conversationId]
    if (!sess?.pendingQuestion) return
    const requestId = sess.pendingQuestion.id
    // Optimistic clear — backend will also emit question.replied, but we clear
    // immediately so the UI feels responsive and double-submits are blocked.
    get()._patchSession(conversationId, s => ({ ...s, pendingQuestion: null }))
    try {
      await agentApi.opencode.replyToQuestion(requestId, answers)
      console.log('[OC] question replied', { requestId, answers, conversationId })
    } catch (err) {
      console.error('[OC] question reply error', err)
      // Restore pending state so the user can retry — server didn't accept.
      get()._patchSession(conversationId, s => (
        s.pendingQuestion ? s : { ...s, pendingQuestion: sess.pendingQuestion }
      ))
    }
  },

  rejectQuestion: async (conversationId) => {
    const sess = get().sessions[conversationId]
    if (!sess?.pendingQuestion) return
    const requestId = sess.pendingQuestion.id
    get()._patchSession(conversationId, s => ({ ...s, pendingQuestion: null }))
    try {
      await agentApi.opencode.rejectQuestion(requestId)
      console.log('[OC] question rejected', { requestId, conversationId })
    } catch (err) {
      console.error('[OC] question reject error', err)
    }
  },

  setAgent: (conversationId, agent) => {
    // Agent mode is set at session creation; changing it requires a new
    // OpenCode session. We just update the stored mode here — caller is
    // expected to recreate the session if it already exists.
    get()._patchSession(conversationId, s => ({ ...s, agent }))
  },

  setReasoning: (conversationId, reasoning) => {
    // Reasoning mode only affects the next message sent (threaded into the
    // request body), so unlike agent mode it can change mid-session without
    // recreating the OpenCode session.
    get()._patchSession(conversationId, s => ({ ...s, reasoning }))
  },
}))

// ---------------------------------------------------------------------------
// Convenience selectors
// ---------------------------------------------------------------------------

/** Returns the session for the currently-active conversation, or null. */
export function useActiveSession(): SessionState | null {
  return useOpenCodeStore(s => {
    if (!s.activeConversationId) return null
    return s.sessions[s.activeConversationId] || null
  })
}

/** Returns a set of conversation IDs that currently have a pending permission. */
export function useConversationsWithPendingPermission(): Set<string> {
  return useOpenCodeStore(s => {
    const set = new Set<string>()
    for (const [cid, sess] of Object.entries(s.sessions)) {
      if (sess.pendingPermission) set.add(cid)
    }
    return set
  })
}

/** Returns a set of conversation IDs that are currently streaming. */
export function useRunningConversations(): Set<string> {
  return useOpenCodeStore(s => {
    const set = new Set<string>()
    for (const [cid, sess] of Object.entries(s.sessions)) {
      if (sess.isRunning) set.add(cid)
    }
    return set
  })
}
