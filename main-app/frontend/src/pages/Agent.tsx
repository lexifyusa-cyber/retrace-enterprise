import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Bot, Loader2, Terminal, FileText, Globe, Monitor,
  Package, Check, ChevronDown, Code, Play, X,
  Plus, MessageSquare, Trash2, Edit2,
  ClipboardList,
  FolderOpen, ListTodo,
  Paperclip, File as FileIcon,
} from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import toast from 'react-hot-toast'
import { teamApi, agentApi } from '@/utils/api'
import { Team, AgentTool, Conversation } from '@/types'
import {
  useOpenCodeStore,
  useActiveSession,
  useConversationsWithPendingPermission,
  useRunningConversations,
} from '@/stores/opencodeStore'
import type { SOPPart, DocPart } from '@/stores/opencodeStore'
import {
  fileToDataUrl,
  filesFromClipboard,
  isImageMime,
  formatBytes,
  AttachmentTooLargeError,
  type FilePart,
} from '@/utils/attachments'
import TerminalPanel from '@/components/TerminalPanel'
import BrowserWorkspace from '@/components/BrowserWorkspace'
import SandboxFileExplorer from '@/components/SandboxFileExplorer'
import MessageList from '@/components/agent/MessageList'
import { getToolIcon, getToolLabel } from '@/components/agent/ToolCallBlock'
import CostBadge from '@/components/agent/CostBadge'
import TodosPanel from '@/components/agent/TodosPanel'
import CommandPalette, { type PaletteCommand } from '@/components/agent/CommandPalette'
import MentionMenu, { type MentionAgent } from '@/components/agent/MentionMenu'
import HeaderOverflowMenu from '@/components/agent/HeaderOverflowMenu'
import McpStatusPill from '@/components/agent/McpStatusPill'

// ---------------------------------------------------------------------------
// Session persistence helpers (follows MCP Builder pattern)
// ---------------------------------------------------------------------------
const AGENT_SESSION_KEY = 'agent_session_state'
function loadAgentSession(): Record<string, any> {
  try {
    const raw = sessionStorage.getItem(AGENT_SESSION_KEY)
    return raw ? JSON.parse(raw) : {}
  } catch { return {} }
}
function saveAgentSession(state: Record<string, any>) {
  try { sessionStorage.setItem(AGENT_SESSION_KEY, JSON.stringify(state)) } catch {}
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function Agent() {
  const queryClient = useQueryClient()
  const _saved = loadAgentSession()
  const [task, setTask] = useState('')
  const [selectedTeamId, setSelectedTeamId] = useState<string>(_saved.selectedTeamId ?? '')
  const [showTeamPicker, setShowTeamPicker] = useState(false)
  const [streamingSOP, setStreamingSOP] = useState<string | null>(null)
  const [streamingDoc, setStreamingDoc] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const autoFollowRef = useRef(true)
  const isInitialMount = useRef(true)
  // Up/Down-arrow prompt history cursor. -1 = not navigating (Up will start at
  // the most recent prompt). Reset on any typing or successful submit.
  const [historyIdx, setHistoryIdx] = useState(-1)
  // Attachments the user has staged for the next send. Cleared on submit.
  const [pendingAttachments, setPendingAttachments] = useState<FilePart[]>([])

  // @-mention typeahead. `mentionQuery` is the substring between the trigger
  // `@` and the cursor; null when no `@` is active. `pendingMentions` collects
  // every agent the user has accepted in this composer turn — they ride
  // alongside attachments into `sendMessage.parts` and clear on submit.
  type AgentPart = { type: 'agent'; name: string; source: { value: string; start: number; end: number } }
  const [mentionQuery, setMentionQuery] = useState<string | null>(null)
  const [mentionStart, setMentionStart] = useState(0)
  const [mentionHi, setMentionHi] = useState(0)
  const [pendingMentions, setPendingMentions] = useState<AgentPart[]>([])
  const [availableAgents, setAvailableAgents] = useState<MentionAgent[]>([])
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Load agent list once when the page mounts; refresh after a model/team
  // regen (the agent list lives in the freshly-generated opencode.json).
  useEffect(() => {
    let cancelled = false
    agentApi.opencode.getConfig().then((cfg: any) => {
      if (cancelled) return
      const agentMap = cfg?.agent || {}
      const list: MentionAgent[] = Object.keys(agentMap).map(name => {
        const def = agentMap[name] || {}
        // Use the first non-empty line of the agent prompt as the description.
        const firstLine = typeof def.prompt === 'string'
          ? def.prompt.split('\n').map((l: string) => l.trim()).find(Boolean) || ''
          : ''
        return { name, description: firstLine.slice(0, 80) }
      })
      setAvailableAgents(list)
    }).catch(() => { /* config not yet generated — list stays empty */ })
    return () => { cancelled = true }
    // Pull a fresh agent list whenever the team changes; we also refetch
    // implicitly on team/model regen via the effect downstream.
  }, [selectedTeamId])

  // Compute filtered agent list each render (cheap — agents.length <= ~5).
  const filteredAgents = mentionQuery == null
    ? []
    : availableAgents.filter(a => a.name.toLowerCase().startsWith(mentionQuery.toLowerCase())).slice(0, 10)
  const mentionOpen = mentionQuery !== null && filteredAgents.length > 0

  // Parse the textarea around the cursor to figure out whether the user is
  // currently in a `@<query>` mention context. Called on every text/cursor
  // change. Sets `mentionQuery` to the substring after `@`, or null.
  const refreshMentionState = useCallback((value: string, cursor: number) => {
    // Walk backwards from cursor looking for the closest `@` that's preceded
    // by whitespace or BOF, with no whitespace between `@` and the cursor.
    let i = cursor - 1
    while (i >= 0 && /\S/.test(value[i])) i--
    const start = i + 1
    if (value[start] !== '@') {
      if (mentionQuery !== null) setMentionQuery(null)
      return
    }
    const before = start === 0 ? '' : value[start - 1]
    if (before && !/\s/.test(before)) {
      if (mentionQuery !== null) setMentionQuery(null)
      return
    }
    const q = value.slice(start + 1, cursor)
    if (mentionQuery !== q) setMentionQuery(q)
    setMentionStart(start)
    setMentionHi(0)
  }, [mentionQuery])

  const acceptMention = useCallback((agent: MentionAgent) => {
    const ta = textareaRef.current
    if (!ta) return
    const value = task
    const cursor = ta.selectionStart ?? value.length
    const replacement = `@${agent.name} `
    const newValue = value.slice(0, mentionStart) + replacement + value.slice(cursor)
    setTask(newValue)
    setPendingMentions(prev => [
      ...prev,
      { type: 'agent', name: agent.name, source: { value: `@${agent.name}`, start: mentionStart, end: mentionStart + replacement.length - 1 } },
    ])
    setMentionQuery(null)
    // Move cursor to just after the inserted name (after the trailing space).
    requestAnimationFrame(() => {
      const pos = mentionStart + replacement.length
      ta.setSelectionRange(pos, pos)
      ta.focus()
    })
  }, [task, mentionStart])
  // Visual drag-over feedback ring around the chat panel. Counter pattern
  // — onDragEnter/leave fire many times on child elements, so we count.
  const dragDepthRef = useRef(0)
  const [isDragOver, setIsDragOver] = useState(false)
  // Hidden file input ref for the paperclip button.
  const fileInputRef = useRef<HTMLInputElement>(null)

  const ingestFiles = useCallback(async (files: FileList | File[] | null | undefined) => {
    if (!files) return
    const arr = Array.from(files)
    if (arr.length === 0) return
    for (const f of arr) {
      try {
        const part = await fileToDataUrl(f)
        setPendingAttachments(prev => [...prev, part])
      } catch (err) {
        if (err instanceof AttachmentTooLargeError) {
          toast.error(err.message)
        } else {
          toast.error(`Couldn't read ${f.name}`)
          console.warn('[attachments] read failed', f.name, err)
        }
      }
    }
  }, [])

  // Conversation state
  const [activeConversationId, setActiveConversationId] = useState<string | null>(_saved.activeConversationId ?? null)
  const [editingTitle, setEditingTitle] = useState<string | null>(null)
  const [editTitleValue, setEditTitleValue] = useState('')

  // Workspace state (browser + terminal split view)
  const [showWorkspace, setShowWorkspace] = useState(true)
  const [workspaceTab, setWorkspaceTab] = useState<'browser' | 'terminal' | 'files' | 'todos'>('browser')
  const [isSandboxed] = useState(false)

  // OpenCode store — global slice (server status, model picker) + per-conversation slice
  const ocStore = useOpenCodeStore()
  const activeSession = useActiveSession()
  const conversationsWithPerm = useConversationsWithPendingPermission()
  const runningConversations = useRunningConversations()
  const ocMessages = activeSession?.messages ?? []
  const ocSessionCost = activeSession?.sessionCost ?? null
  const pendingPermission = activeSession?.pendingPermission ?? null
  const isRunning = activeSession?.isRunning ?? false

  // Agent mode: set at OpenCode session create time and locked thereafter.
  // `preferredAgent` is the mode applied to the NEXT session we create when
  // the active conversation doesn't have an OC session yet.
  const [preferredAgent, setPreferredAgent] = useState<'build' | 'plan'>('build')
  const displayAgent: 'build' | 'plan' = activeSession?.agent ?? preferredAgent
  const agentLocked = !!activeSession?.sessionId

  const handleSetAgent = useCallback((mode: 'build' | 'plan') => {
    if (isRunning || agentLocked || mode === displayAgent) return
    setPreferredAgent(mode)
    if (activeConversationId) ocStore.setAgent(activeConversationId, mode)
  }, [isRunning, agentLocked, displayAgent, activeConversationId])

  // Resizable split: chatWidthPct is the left panel's percentage (30–80)
  const [chatWidthPct, setChatWidthPct] = useState(50)
  const isDraggingRef = useRef(false)
  const splitContainerRef = useRef<HTMLDivElement>(null)

  // Drag-to-resize workspace split
  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!isDraggingRef.current || !splitContainerRef.current) return
      const rect = splitContainerRef.current.getBoundingClientRect()
      const pct = Math.min(80, Math.max(30, ((e.clientX - rect.left) / rect.width) * 100))
      setChatWidthPct(pct)
    }
    const onMouseUp = () => { isDraggingRef.current = false; document.body.style.cursor = ''; document.body.style.userSelect = '' }
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    return () => { window.removeEventListener('mousemove', onMouseMove); window.removeEventListener('mouseup', onMouseUp) }
  }, [])

  // Pre-start MCP extensions and OpenCode server in background on page load
  useEffect(() => {
    agentApi.warmupMcp()
    ocStore.checkStatus().then(({ running, healthy }) => {
      if (!running || !healthy) {
        ocStore.startServer(selectedTeamId || undefined)
      }
    })
  }, [])

  // Mirror the page's active conversation into the store so per-session selectors
  // (isRunning / messages / pendingPermission / diff / cost / undo-redo / agent)
  // resolve against the right SessionState.
  useEffect(() => {
    ocStore.setActiveConversation(activeConversationId)
  }, [activeConversationId])

  // On unmount: close every local SSE reader. OpenCode keeps generating in the
  // background; on remount the conversation will sync via refreshMessages.
  useEffect(() => {
    return () => {
      useOpenCodeStore.getState().abortAllStreams()
    }
  }, [])

  const { data: teams = [] } = useQuery({
    queryKey: ['teams'],
    queryFn: () => teamApi.list(),
  })

  const { data: tools = [] } = useQuery<AgentTool[]>({
    queryKey: ['agent-tools'],
    queryFn: () => agentApi.listTools(),
  })

  // Tool toggle — LIVE update, no agent restart.
  //
  // Hits the new PATCH /agent/opencode/tools endpoint which:
  //   * Persists `disabled_tools` to settings.agent_tools_config.
  //   * For native tools: PATCHes every open OC session of the user with the
  //     freshly-built Permission.Ruleset (deny rules per disabled tool).
  //   * For MCP-provided tools: connect/disconnect on OC.
  // No config-file rewrite, no watcher fire, no `regenerate-config`. The
  // optimistic `onMutate` updates the chip cache immediately so the dim/light
  // is instant; the round-trip is just for persistence + fan-out.
  const updateToolsMutation = useMutation({
    mutationFn: (disabledTools: string[]) => agentApi.opencode.setTools(disabledTools),
    onMutate: async (disabledTools: string[]) => {
      await queryClient.cancelQueries({ queryKey: ['agent-tools'] })
      const prev = queryClient.getQueryData<AgentTool[]>(['agent-tools'])
      if (prev) {
        const disabledSet = new Set(disabledTools)
        queryClient.setQueryData<AgentTool[]>(
          ['agent-tools'],
          prev.map(t => ({ ...t, enabled: t.available && !disabledSet.has(t.name) })),
        )
      }
      return { prev }
    },
    onError: (err: any, _vars, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(['agent-tools'], ctx.prev)
      toast.error(err?.response?.data?.detail || 'Failed to update tools')
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-tools'] })
    },
  })

  const handleToolToggle = (toolName: string, currentlyEnabled: boolean) => {
    const currentDisabled = tools.filter(t => t.enabled === false).map(t => t.name)
    let newDisabled: string[]
    if (currentlyEnabled) {
      const wouldBeEnabled = tools.filter(t => t.name !== toolName && (t.enabled !== false)).length
      if (wouldBeEnabled === 0) {
        toast.error('At least one tool must be enabled')
        return
      }
      newDisabled = [...currentDisabled, toolName]
    } else {
      newDisabled = currentDisabled.filter(n => n !== toolName)
    }
    updateToolsMutation.mutate(newDisabled)
  }

  // Conversations for the selected team (or team-less)
  const { data: conversations = [], refetch: refetchConversations } = useQuery<Conversation[]>({
    queryKey: ['conversations', selectedTeamId || '__none__'],
    queryFn: () => agentApi.listConversations(selectedTeamId || '__none__'),
  })

  const trainedTeams = teams.filter((p: Team) =>
    p.folder_groups?.some(g => g.training_status === 'completed') === true
  )

  // When team changes: deselect active conversation, abort local streams,
  // regenerate the OC config (so RETRACE_TEAM_ID + system prompt update), and
  // mark every cached session's sessionId as stale. The OC sidecar restarts
  // server-side — old session ids are now dead upstream; the next click on
  // any of them falls through Group 3's SESSION_NOT_FOUND path to load DB
  // history and a fresh session.
  //
  // Confirmation for the streams-running case is handled at the picker
  // click site (handleSelectTeam below) before selectedTeamId actually changes.
  useEffect(() => {
    if (isInitialMount.current) {
      isInitialMount.current = false
      return
    }
    setActiveConversationId(null)
    useOpenCodeStore.getState().abortAllStreams()
    ocStore.regenerateConfig(selectedTeamId || undefined).catch(err => {
      console.warn('[Team change] regenerate-config failed', err)
      toast.error('Failed to switch team context. Try refreshing.')
    })
    useOpenCodeStore.setState(state => ({
      sessions: Object.fromEntries(
        Object.entries(state.sessions).map(([cid, s]) => [cid, { ...s, sessionId: null }])
      ),
    }))
  }, [selectedTeamId])

  // Wrap team picker clicks with a confirmation when streams are running so
  // the user knows mid-flight work will be cut off by the sidecar restart.
  const handleSelectTeam = useCallback((newTeamId: string) => {
    if (newTeamId === selectedTeamId) {
      setShowTeamPicker(false)
      return
    }
    const runningCount = Object.values(useOpenCodeStore.getState().sessions)
      .filter(s => s.isRunning).length
    if (runningCount > 0) {
      const noun = runningCount === 1 ? 'conversation' : 'conversations'
      const ok = window.confirm(
        `Switching team will end ${runningCount} active ${noun} and restart the agent. Continue?`
      )
      if (!ok) {
        setShowTeamPicker(false)
        return
      }
    }
    setSelectedTeamId(newTeamId)
    setShowTeamPicker(false)
  }, [selectedTeamId])

  // Auto-scroll: follow streaming output. Pauses when the user manually
  // scrolls up by more than 80px from the bottom; resumes when they scroll
  // back near the bottom. Uses 'instant' so rapid deltas don't get cancelled
  // by overlapping smooth-scroll animations.
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    const onScroll = () => {
      const fromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      autoFollowRef.current = fromBottom < 80
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  // Track the last part's text length so the effect re-fires on every streaming
  // delta (the parts array reference does change, but useEffect's reference
  // diffing on an object dep would still re-fire — this is belt-and-braces).
  const lastPartLen = (() => {
    const last = ocMessages[ocMessages.length - 1]
    if (!last) return 0
    return last.parts.reduce((sum, p) => sum + ((p as any).text?.length ?? 0), 0)
  })()

  useEffect(() => {
    if (!autoFollowRef.current) return
    scrollRef.current?.scrollIntoView({ behavior: 'instant' as ScrollBehavior, block: 'end' })
  }, [ocMessages, lastPartLen, streamingSOP, streamingDoc])

  // Persist key state to sessionStorage on every change
  useEffect(() => {
    const sessionId = activeSession?.sessionId ?? null
    saveAgentSession({ selectedTeamId, activeConversationId, opencodeSessionId: sessionId })
  }, [selectedTeamId, activeConversationId, activeSession?.sessionId])

  // Auto-restore last session on mount
  useEffect(() => {
    const saved = loadAgentSession()
    const savedSessionId = saved.opencodeSessionId as string | undefined
    const savedConvoId = saved.activeConversationId as string | undefined
    if (!savedConvoId) return
    ocStore.checkStatus().then(({ running }) => {
      if (!running) {
        // OC server is down. Show DB history at least, so the user isn't
        // staring at an empty chat.
        ocStore.loadFromDb(savedConvoId).catch(() => {})
        return
      }
      if (savedSessionId) {
        ocStore.loadSession(savedConvoId, savedSessionId).catch((err: any) => {
          // Saved session is gone (sidecar restart, manual cleanup, etc.).
          // Show DB history so the user can read prior context, clear the
          // stale id from sessionStorage, and wait for the user to send a
          // new message (which will create a fresh OC session).
          if (err?.message === 'SESSION_NOT_FOUND') {
            saveAgentSession({ ...saved, opencodeSessionId: null })
            ocStore.loadFromDb(savedConvoId).catch(() => {})
          }
        })
      } else {
        // Conversation selected but no OC session id ever saved — load DB.
        ocStore.loadFromDb(savedConvoId).catch(() => {})
      }
    })
  }, [])

  // Create new conversation. We do NOT touch other conversations' sessions —
  // those keep streaming in the background.
  const handleNewConversation = useCallback(async () => {
    try {
      const convo = await agentApi.createConversation(selectedTeamId || '', 'New conversation')
      setActiveConversationId(convo.conversation_id)
      refetchConversations()
      queryClient.invalidateQueries({ queryKey: ['dashboard-stats'] })
    } catch {
      toast.error('Failed to create conversation')
    }
  }, [selectedTeamId, refetchConversations])

  // Delete conversation — cancels its stream (if any) and drops its session.
  const handleDeleteConversation = useCallback(async (conversationId: string, e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await agentApi.deleteConversation(conversationId)
      ocStore.clearSession(conversationId)
      if (activeConversationId === conversationId) {
        setActiveConversationId(null)
      }
      refetchConversations()
      queryClient.invalidateQueries({ queryKey: ['dashboard-stats'] })
      toast.success('Conversation deleted')
    } catch {
      toast.error('Failed to delete conversation')
    }
  }, [activeConversationId, refetchConversations])

  // Delete all conversations for the current team — cancel every stream we know about.
  const handleDeleteAllConversations = useCallback(async () => {
    if (!confirm('Delete all conversations? This cannot be undone.')) return
    try {
      await agentApi.deleteAllConversations(selectedTeamId || '__none__')
      const known = Object.keys(useOpenCodeStore.getState().sessions)
      for (const cid of known) ocStore.clearSession(cid)
      setActiveConversationId(null)
      refetchConversations()
      toast.success('All conversations deleted')
    } catch {
      toast.error('Failed to delete conversations')
    }
  }, [selectedTeamId, refetchConversations])

  // Rename conversation
  const handleRenameConversation = useCallback(async (conversationId: string) => {
    if (!editTitleValue.trim()) return
    try {
      await agentApi.renameConversation(conversationId, editTitleValue.trim())
      setEditingTitle(null)
      refetchConversations()
    } catch {
      toast.error('Failed to rename conversation')
    }
  }, [editTitleValue, refetchConversations])

  // Learn This
  const handleLearnThis = useCallback(async (question: string, answer: string) => {
    if (!selectedTeamId) {
      toast.error('Select a team to save knowledge')
      return
    }
    try {
      await agentApi.learnThis(selectedTeamId, question, answer)
      toast.success('Added to knowledge base!', { icon: '🎓' })
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to learn Q&A')
      throw err
    }
  }, [selectedTeamId])

  // Execute task via OpenCode. We gate on the ACTIVE session's isRunning so the
  // user can send messages in conversation B while conversation A is still
  // streaming in the background.
  const handleExecute = useCallback(async () => {
    if (!task.trim()) return

    // If a run is already in flight, skip server/session setup — we know
    // they're already wired — and let the store enqueue this message.
    // The drain on the current turn's completion fires it as the next turn.
    if (isRunning && activeConversationId) {
      const currentTask = task
      const currentAttachments = pendingAttachments
      const currentMentions = pendingMentions
      setTask('')
      setHistoryIdx(-1)
      setPendingAttachments([])
      setPendingMentions([])
      const sendParts = [...currentAttachments, ...currentMentions]
      try { await ocStore.sendMessage(activeConversationId, currentTask, sendParts.length ? sendParts : undefined) }
      catch (err: any) {
        if (err?.name !== 'AbortError') toast.error(err?.message || 'Failed to queue message')
      }
      return
    }

    // Ensure OpenCode server is running. `isStarting` is the global gate; the
    // server is shared across all this user's conversations.
    const status = await ocStore.checkStatus()
    if (!status.running) {
      try {
        await ocStore.startServer(selectedTeamId || undefined)
      } catch {
        toast.error('Failed to start OpenCode server')
        return
      }
    }

    // Resolve the conversation we're sending to — create one if needed.
    let conversationId = activeConversationId
    if (!conversationId) {
      try {
        const convo = await agentApi.createConversation(selectedTeamId || '', task.slice(0, 80))
        conversationId = convo.conversation_id
        setActiveConversationId(conversationId)
        refetchConversations()
        queryClient.invalidateQueries({ queryKey: ['dashboard-stats'] })
      } catch {
        toast.error('Failed to create conversation')
        return
      }
    }

    // Ensure this conversation has an OpenCode session linked.
    const existingSess = useOpenCodeStore.getState().sessions[conversationId]
    if (!existingSess?.sessionId) {
      try {
        // Look up a previously-linked OC session from the conversation list.
        const cachedConvos = queryClient.getQueryData<Conversation[]>(
          ['conversations', selectedTeamId || '__none__']
        )
        const cached = cachedConvos?.find((c: Conversation) => c.conversation_id === conversationId)
        const linkedOcSessionId = cached?.opencode_session_id
        if (linkedOcSessionId) {
          try {
            await ocStore.loadSession(conversationId, linkedOcSessionId)
          } catch (err: any) {
            // Linked OC session is dead — load DB history (so the user keeps
            // the visible context) and then start a fresh OC session. The
            // backend's link_session replaces the dead DB pointer.
            if (err?.message === 'SESSION_NOT_FOUND') {
              await ocStore.loadFromDb(conversationId)
              await ocStore.createSession(conversationId, selectedTeamId || undefined, displayAgent)
            } else {
              throw err
            }
          }
        } else {
          await ocStore.createSession(conversationId, selectedTeamId || undefined, displayAgent)
        }
      } catch {
        toast.error('Failed to create OpenCode session')
        return
      }
    }

    const currentTask = task
    const currentAttachments = pendingAttachments
    const currentMentions = pendingMentions
    setTask('')
    setHistoryIdx(-1)
    setPendingAttachments([])
    setPendingMentions([])
    const sendParts = [...currentAttachments, ...currentMentions]

    try {
      await ocStore.sendMessage(conversationId, currentTask, sendParts.length ? sendParts : undefined)
    } catch (err: any) {
      if (err?.name !== 'AbortError') {
        toast.error(err?.message || 'Connection failed')
      }
    } finally {
      // Cost is a post-stream view, refresh it once.
      // Message persistence is handled by the backend's sync_messages_from_opencode
      // call in the SSE relay's finally block — we no longer write messages to
      // ReTrace's DB from the frontend (which was incomplete and N²-growing).
      ocStore.fetchCost(conversationId)
      // Refetch the conversation list so the title (auto-derived from first user
      // message during sync) and message_count update without a manual reload.
      refetchConversations()
    }
  }, [task, selectedTeamId, isRunning, activeConversationId, displayAgent, refetchConversations, pendingAttachments, pendingMentions])

  const handleSubmit = (e: React.FormEvent) => { e.preventDefault(); handleExecute() }

  // ── Command palette ─────────────────────────────────────────────────
  // Appears whenever the input starts with `/`. Drives slash commands like
  // /revert, /retry, /share, etc. — same set OpenCode's TUI exposes, plus a
  // handful of ReTrace-specific ones (/learn, /automation, /documentation).
  const paletteOpen = task.trimStart().startsWith('/')

  const paletteHandlers = useMemo(() => ({
    onRevert: async (messageId?: string) => {
      if (!activeConversationId) return
      if (messageId) await ocStore.revertToMessage(activeConversationId, messageId)
      else await ocStore.undo(activeConversationId)
    },
    onRedo: async () => {
      if (!activeConversationId) return
      await ocStore.redo(activeConversationId)
    },
    // No /retry — OpenCode has no user-driven retry. Users Revert and re-prompt.
    onShare: async () => {
      if (!activeConversationId) return
      const url = await ocStore.shareSession(activeConversationId)
      if (url) {
        try { await navigator.clipboard.writeText(url) } catch { /* ignore */ }
        toast.success('Share URL copied to clipboard')
      } else {
        toast.error('Failed to create share URL')
      }
    },
    onSummarize: async () => {
      if (!activeConversationId) return
      toast('Summarizing...', { icon: '📝' })
      await ocStore.summarizeSession(activeConversationId)
    },
    onFork: async () => {
      if (!activeConversationId) return
      const result = await ocStore.forkSession(activeConversationId)
      if (result?.conversationId) {
        setActiveConversationId(result.conversationId)
        refetchConversations()
        toast.success('Forked into a new conversation')
      }
    },
    onCompact: async () => {
      if (!activeConversationId) return
      toast('Compacting context...', { icon: '🗜️' })
      await ocStore.compactSession(activeConversationId)
    },
    onLearn: async () => {
      // Defer to the inline Learn action on the latest assistant message.
      // The button is part of the ActionBar in MessageList; user can click it.
      // (Slash-command shortcut just nudges them there.)
      toast('Use the Learn button on the latest answer', { icon: '🎓' })
    },
    onBuildAutomation: async () => {
      toast('Use the Build Automation button on the latest answer', { icon: '⚙️' })
    },
    onBuildDoc: async () => {
      toast('Use the Build Documentation button on the latest answer', { icon: '📄' })
    },
    onTodos: () => {
      setWorkspaceTab('todos')
      if (!showWorkspace) setShowWorkspace(true)
    },
    onHelp: () => {
      toast(
        'Slash commands: /revert /redo /share /summarize /fork /compact /todos /help',
        { duration: 6000 }
      )
    },
  }), [activeConversationId, refetchConversations, showWorkspace])

  const handlePaletteSelect = useCallback(async (cmd: PaletteCommand, args: string) => {
    setTask('')
    // OC-source commands without a wired handler — fall back to sending the
    // raw slash text as a prompt; OC's agent will interpret it.
    if (cmd.source === 'oc') {
      if (!activeConversationId) return
      const full = args ? `${cmd.slash} ${args}` : cmd.slash
      // Send through the normal execute path so streaming UI works.
      setTask(full)
      // Defer to next tick so setTask propagates before submit.
      setTimeout(() => { handleExecute() }, 0)
      return
    }
    await cmd.run(args)
  }, [activeConversationId, handleExecute])
  const handleStop = () => {
    if (activeConversationId) {
      ocStore.stopMessage(activeConversationId)
      toast('Stopped by user', { icon: '⏹' })
    }
  }

  // SOP creation
  const [isCreatingSOP, setIsCreatingSOP] = useState(false)

  // Documentation creation
  const [isCreatingDoc, setIsCreatingDoc] = useState(false)

  const handleCreateSOP = useCallback(async (goalOverride?: string) => {
    if (isCreatingSOP) return
    if (!activeConversationId) {
      toast.error('No conversation selected')
      return
    }
    const currentMsgs = useOpenCodeStore.getState().sessions[activeConversationId]?.messages ?? []
    const lastUserMsg = [...currentMsgs].reverse().find(m => m.role === 'user')
    const goal = goalOverride ?? lastUserMsg?.parts.find(p => p.type === 'text')?.text
    if (!goal) {
      toast.error('No conversation to create automation from')
      return
    }
    const saveable = currentMsgs.flatMap(m =>
      m.parts
        .filter(p => (p.type === 'text' && p.text) || p.type === 'tool')
        .flatMap((p): Array<{ type: string; content: string }> => {
          if (m.role === 'user') {
            return [{ type: 'user', content: p.type === 'text' ? p.text! : '' }]
          }
          if (p.type === 'tool') {
            const entries: Array<{ type: string; content: string }> = []
            const inputStr = p.state.input ? JSON.stringify(p.state.input, null, 2) : ''
            if (inputStr) entries.push({ type: 'code', content: `Tool: ${p.tool}\nInput: ${inputStr}` })
            const outputStr = typeof p.state.output === 'string' ? p.state.output : JSON.stringify(p.state.output || '')
            if (outputStr) entries.push({ type: 'output', content: outputStr })
            return entries
          }
          return [{ type: 'answer', content: p.type === 'text' ? p.text! : '' }]
        })
        .filter((e): e is { type: string; content: string } => !!e.content)
    )

    setIsCreatingSOP(true)
    try {
      const response = await fetch('/api/v1/agent/create-sop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${localStorage.getItem('rt_access_token') || ''}` },
        body: JSON.stringify({ team_id: selectedTeamId || null, goal, messages: saveable, conversation_id: activeConversationId || undefined }),
      })
      if (!response.ok) {
        const err = await response.text()
        toast.error(err)
        return
      }
      const reader = response.body?.getReader()
      if (!reader) { toast.error('No stream reader'); return }
      const decoder = new TextDecoder()
      let buffer = ''
      let sopResult: { sop_id?: string; title?: string; sop_markdown?: string } = {}
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        let currentEvent = ''
        for (const line of lines) {
          if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim() }
          else if (line.startsWith('data: ') && currentEvent) {
            try {
              const eventData = JSON.parse(line.slice(6))
              if (currentEvent === 'sop_chunk') {
                setStreamingSOP(eventData.chunk)
              } else if (currentEvent === 'sop_done') {
                sopResult = eventData
                setStreamingSOP(null)
              } else if (currentEvent === 'sop_error') {
                toast.error(eventData.detail || 'Automation generation failed')
                setStreamingSOP(null)
              }
            } catch { /* ignore */ }
            currentEvent = ''
          }
        }
      }
      if (sopResult.sop_id) {
        const sopPart: SOPPart = {
          type: 'sop',
          id: `sop-${sopResult.sop_id}`,
          sopId: sopResult.sop_id,
          title: sopResult.title || '',
          content: sopResult.sop_markdown || '',
        }
        ocStore.injectPart(activeConversationId, sopPart)
        toast.success('Automation created!')
      }
    } catch (err: any) {
      toast.error(err?.message || 'Failed to create automation')
      setStreamingSOP(null)
    } finally {
      setIsCreatingSOP(false)
    }
  }, [selectedTeamId, isCreatingSOP, activeConversationId])

  const handleApproveSOP = useCallback(async (sopId: string, scheduleType?: string, scheduleConfig?: Record<string, any>): Promise<void> => {
    try {
      await agentApi.approveSOP(sopId, scheduleType || 'none', scheduleConfig)
      toast.success('Automation approved & scheduled!')
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to approve automation')
      throw err
    }
  }, [])

  const handleEditSOP = useCallback(async (sopId: string, editInstructions: string): Promise<{ sop_markdown: string; title: string }> => {
    const response = await fetch('/api/v1/agent/edit-sop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${localStorage.getItem('rt_access_token') || ''}` },
      body: JSON.stringify({ sop_id: sopId, edit_instructions: editInstructions }),
    })
    if (!response.ok) throw new Error(await response.text())
    const reader = response.body?.getReader()
    if (!reader) throw new Error('No stream reader')
    const decoder = new TextDecoder()
    let buffer = ''
    let editResult: { sop_markdown: string; title: string } = { sop_markdown: '', title: '' }
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      let currentEvent = ''
      for (const line of lines) {
        if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim() }
        else if (line.startsWith('data: ') && currentEvent) {
          try {
            const eventData = JSON.parse(line.slice(6))
            if (currentEvent === 'sop_chunk') {
              setStreamingSOP(eventData.chunk)
            } else if (currentEvent === 'sop_done') {
              editResult = { sop_markdown: eventData.sop_markdown, title: eventData.title }
              setStreamingSOP(null)
            } else if (currentEvent === 'sop_error') {
              setStreamingSOP(null)
              throw new Error(eventData.detail || 'SOP edit failed')
            }
          } catch (e) { if (e instanceof Error && e.message !== 'SOP edit failed') { /* ignore parse errors */ } else throw e }
          currentEvent = ''
        }
      }
    }
    toast.success('Automation updated!')
    return editResult
  }, [])

  // Documentation creation
  const handleCreateDoc = useCallback(async (goalOverride?: string) => {
    if (isCreatingDoc) return
    if (!activeConversationId) {
      toast.error('No conversation selected')
      return
    }
    const currentMsgs = useOpenCodeStore.getState().sessions[activeConversationId]?.messages ?? []
    const lastUserMsg = [...currentMsgs].reverse().find(m => m.role === 'user')
    const goal = goalOverride ?? lastUserMsg?.parts.find(p => p.type === 'text')?.text
    if (!goal) {
      toast.error('No conversation to create documentation from')
      return
    }
    const saveable = currentMsgs.flatMap(m =>
      m.parts
        .filter(p => (p.type === 'text' && p.text) || p.type === 'tool')
        .flatMap((p): Array<{ type: string; content: string }> => {
          if (m.role === 'user') {
            return [{ type: 'user', content: p.type === 'text' ? p.text! : '' }]
          }
          if (p.type === 'tool') {
            const entries: Array<{ type: string; content: string }> = []
            const inputStr = p.state.input ? JSON.stringify(p.state.input, null, 2) : ''
            if (inputStr) entries.push({ type: 'code', content: `Tool: ${p.tool}\nInput: ${inputStr}` })
            const outputStr = typeof p.state.output === 'string' ? p.state.output : JSON.stringify(p.state.output || '')
            if (outputStr) entries.push({ type: 'output', content: outputStr })
            return entries
          }
          return [{ type: 'answer', content: p.type === 'text' ? p.text! : '' }]
        })
        .filter((e): e is { type: string; content: string } => !!e.content)
    )

    setIsCreatingDoc(true)
    try {
      const response = await fetch('/api/v1/agent/create-doc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${localStorage.getItem('rt_access_token') || ''}` },
        body: JSON.stringify({ team_id: selectedTeamId || null, goal, messages: saveable, conversation_id: activeConversationId || undefined }),
      })
      if (!response.ok) {
        const err = await response.text()
        toast.error(err)
        return
      }
      const reader = response.body?.getReader()
      if (!reader) { toast.error('No stream reader'); return }
      const decoder = new TextDecoder()
      let buffer = ''
      let docResult: { doc_id?: string; title?: string; doc_markdown?: string } = {}
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        let currentEvent = ''
        for (const line of lines) {
          if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim() }
          else if (line.startsWith('data: ') && currentEvent) {
            try {
              const eventData = JSON.parse(line.slice(6))
              if (currentEvent === 'doc_chunk') {
                setStreamingDoc(eventData.chunk)
              } else if (currentEvent === 'doc_done') {
                docResult = eventData
                setStreamingDoc(null)
              } else if (currentEvent === 'doc_error') {
                toast.error(eventData.detail || 'Documentation generation failed')
                setStreamingDoc(null)
              }
            } catch { /* ignore */ }
            currentEvent = ''
          }
        }
      }
      if (docResult.doc_id) {
        const docPart: DocPart = {
          type: 'doc',
          id: `doc-${docResult.doc_id}`,
          docId: docResult.doc_id,
          title: docResult.title || '',
          content: docResult.doc_markdown || '',
        }
        ocStore.injectPart(activeConversationId, docPart)
        toast.success('Documentation created!')
      }
    } catch (err: any) {
      toast.error(err?.message || 'Failed to create documentation')
      setStreamingDoc(null)
    } finally {
      setIsCreatingDoc(false)
    }
  }, [selectedTeamId, isCreatingDoc, activeConversationId])

  const handleApproveDoc = useCallback(async (docId: string): Promise<void> => {
    try {
      await agentApi.approveDoc(docId)
      toast.success('Documentation approved!')
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to approve documentation')
      throw err
    }
  }, [])

  const handleEditDoc = useCallback(async (docId: string, editInstructions: string): Promise<{ doc_markdown: string; title: string }> => {
    const response = await fetch('/api/v1/agent/edit-doc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${localStorage.getItem('rt_access_token') || ''}` },
      body: JSON.stringify({ doc_id: docId, edit_instructions: editInstructions }),
    })
    if (!response.ok) throw new Error(await response.text())
    const reader = response.body?.getReader()
    if (!reader) throw new Error('No stream reader')
    const decoder = new TextDecoder()
    let buffer = ''
    let editResult: { doc_markdown: string; title: string } = { doc_markdown: '', title: '' }
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      let currentEvent = ''
      for (const line of lines) {
        if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim() }
        else if (line.startsWith('data: ') && currentEvent) {
          try {
            const eventData = JSON.parse(line.slice(6))
            if (currentEvent === 'doc_chunk') {
              setStreamingDoc(eventData.chunk)
            } else if (currentEvent === 'doc_done') {
              editResult = { doc_markdown: eventData.doc_markdown, title: eventData.title }
              setStreamingDoc(null)
            } else if (currentEvent === 'doc_error') {
              setStreamingDoc(null)
              throw new Error(eventData.detail || 'Documentation edit failed')
            }
          } catch (e) { if (e instanceof Error && e.message !== 'Documentation edit failed') { /* ignore parse errors */ } else throw e }
          currentEvent = ''
        }
      }
    }
    toast.success('Documentation updated!')
    return editResult
  }, [])

  const selectedTeam = trainedTeams.find((p: Team) => p.team_id === selectedTeamId)

  const [fileOpsOpen, setFileOpsOpen] = useState(false)

  const isToolEffectivelyOn = (name: string, available: boolean, enabledFlag: boolean | undefined) => {
    const kbNoTeam = (name === 'knowledge_base' || name === 'search_knowledge_base') && !selectedTeamId
    return !kbNoTeam && available && (enabledFlag !== false)
  }
  const enabledToolsCount = tools.filter(t => isToolEffectivelyOn(t.name, t.available, t.enabled)).length
  const fileOpsToolNames = new Set(['read', 'write', 'edit', 'multiEdit', 'glob', 'grep'])
  const fileOpsTools = tools.filter(t => fileOpsToolNames.has(t.name))
  const standaloneTools = tools.filter(t => !fileOpsToolNames.has(t.name))

  // ── Render ───────────────────────────────────────────────────────
  return (
    <div className="h-full flex flex-col">
      {/* Agent header — click title 6 times to toggle debug mode */}
      <div className="px-8 py-3 border-b border-rt-border flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-purple-500/20 to-blue-500/20 flex items-center justify-center">
          <Bot className="w-4 h-4 text-purple-400" />
        </div>
        <h1 className="text-lg font-display font-bold cursor-default select-none">Agent</h1>
        <div className="flex items-center rounded-lg border border-rt-border overflow-hidden">
          <button type="button" onClick={() => handleSetAgent('build')}
            disabled={isRunning || agentLocked}
            title={agentLocked ? 'Agent mode is locked for this conversation. Start a new conversation to change it.' : 'Build mode'}
            className={`flex items-center gap-1 px-2.5 py-1 text-xs font-medium transition-colors ${
              displayAgent === 'build'
                ? 'bg-rt-primary/15 text-rt-primary'
                : 'bg-rt-surface/50 text-rt-text-muted/50 hover:text-rt-text-muted'
            }`}>
            <Bot className="w-3 h-3" /> Build
          </button>
          <button type="button" onClick={() => handleSetAgent('plan')}
            disabled={isRunning || agentLocked}
            title={agentLocked ? 'Agent mode is locked for this conversation. Start a new conversation to change it.' : 'Plan mode (read-only analysis)'}
            className={`flex items-center gap-1 px-2.5 py-1 text-xs font-medium transition-colors border-l border-rt-border ${
              displayAgent === 'plan'
                ? 'bg-blue-500/15 text-blue-400'
                : 'bg-rt-surface/50 text-rt-text-muted/50 hover:text-rt-text-muted'
            }`}>
            <ClipboardList className="w-3 h-3" /> Plan
          </button>
        </div>
        <CostBadge cost={ocSessionCost} />

        <HeaderOverflowMenu
          canFork={!!activeSession?.sessionId && !isRunning && !!activeConversationId}
          canShare={!!activeSession?.sessionId && !!activeConversationId}
          canSummarize={!!activeSession?.sessionId && !isRunning && !!activeConversationId}
          canStopServer={!!activeSession?.sessionId && !ocStore.externalMode}
          onFork={async () => {
            if (!activeConversationId) return
            const result = await ocStore.forkSession(activeConversationId)
            if (result?.conversationId) {
              setActiveConversationId(result.conversationId)
              refetchConversations()
              toast.success('Forked into a new conversation')
            } else { toast.error('Failed to fork session') }
          }}
          onShare={async () => {
            if (!activeConversationId) return
            const url = await ocStore.shareSession(activeConversationId)
            if (url) {
              try { await navigator.clipboard.writeText(url) } catch { /* ignore */ }
              toast.success('Share URL copied to clipboard')
            } else {
              toast.error('Failed to create share URL')
            }
          }}
          onSummarize={() => {
            if (!activeConversationId) return
            toast('Summarizing...', { icon: '📝' })
            ocStore.summarizeSession(activeConversationId)
          }}
          onStopServer={() => { ocStore.stopServer(); toast.success('OpenCode server stopped') }}
        />
      </div>

      {/* Team selector + tool status */}
      <div className="px-8 py-3 border-b border-rt-border bg-rt-bg-light flex items-center gap-4">
        <div className="relative">
          <button type="button" onClick={() => setShowTeamPicker(!showTeamPicker)}
            // Colorless glass dropdown — same recipe as the Browser/Terminal/
            // Files tabs (white tint, no theme color since this is a neutral
            // selector, not a state pill).
            style={{
              background:
                'linear-gradient(180deg, rgba(255,255,255,0.20) 0%, rgba(255,255,255,0.10) 100%)',
              boxShadow:
                'inset 0 1px 0 rgba(255,255,255,0.4), 0 6px 16px -6px rgba(0,0,0,0.12), 0 0 0 1px rgba(255,255,255,0.18)',
            }}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg backdrop-blur-md transition-colors text-sm">
            <Package className="w-4 h-4 text-rt-primary" />
            <span>{selectedTeam?.team_name || 'No team'}</span>
            <ChevronDown className={`w-4 h-4 text-rt-text-muted transition-transform ${showTeamPicker ? 'rotate-180' : ''}`} />
          </button>
          <AnimatePresence>
            {showTeamPicker && (
              <motion.div initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }}
                // Glass dropdown panel — translucent white tint, top-edge
                // gloss highlight, soft drop shadow, subtle white edge.
                style={{
                  background:
                    'linear-gradient(180deg, rgba(255,255,255,0.30) 0%, rgba(255,255,255,0.18) 100%)',
                  boxShadow:
                    'inset 0 1px 0 rgba(255,255,255,0.5), 0 16px 40px -8px rgba(0,0,0,0.18), 0 0 0 1px rgba(255,255,255,0.22)',
                }}
                className="absolute top-full left-0 mt-1 w-64 rounded-lg backdrop-blur-xl z-50 overflow-hidden">
                <button type="button"
                  onClick={() => handleSelectTeam('')}
                  // Hover = translucent glass film slides over (instead of
                  // solid white). bg-white/15 over the panel's already-
                  // translucent bg reads as a slightly brighter pane of
                  // glass passing across the row.
                  className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-white/15 ${!selectedTeamId ? 'bg-white/20 text-rt-primary' : ''}`}>
                  {!selectedTeamId && <Check className="w-3.5 h-3.5" />}
                  <span className="text-rt-text-muted italic">No team (general)</span>
                </button>
                {trainedTeams.map((p: Team) => (
                  <button key={p.team_id} type="button"
                    onClick={() => handleSelectTeam(p.team_id)}
                    className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-white/15 ${p.team_id === selectedTeamId ? 'bg-white/20 text-rt-primary' : ''}`}>
                    {p.team_id === selectedTeamId && <Check className="w-3.5 h-3.5" />}
                    <span className="truncate flex-1">{p.team_name}</span>
                    {(p as any).metadata?.is_prebuilt && (
                      <span className="ml-auto px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-500/10 text-amber-600 border border-amber-500/20 flex-shrink-0">
                        Sample
                      </span>
                    )}
                  </button>
                ))}
                {trainedTeams.length === 0 && <p className="px-3 py-2 text-sm text-rt-text-muted italic">No trained teams</p>}
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        {/* MCP server connection pill — lets the user see + reconnect failed
            MCP servers without going to Settings. */}
        <McpStatusPill />

        {/* Tool icons — click to enable/disable */}
        <div className="flex items-center gap-3 text-xs text-rt-text-muted">
          <span className="flex items-center gap-1.5">
            <div className={`w-2 h-2 rounded-full ${enabledToolsCount > 0 ? 'bg-rt-success' : 'bg-rt-warning'}`} />
            <span>{enabledToolsCount}/{tools.length} tools</span>
          </span>
          {isSandboxed && (
            <span
              // Glossy translucent-green pill, same recipe as the conversation
              // chips but in green-500 (#22c55e) for the "secure / sandboxed"
              // signal.
              style={{
                background:
                  'linear-gradient(180deg, rgba(34,197,94,0.18) 0%, rgba(34,197,94,0.09) 100%)',
                boxShadow:
                  'inset 0 1px 0 rgba(255,255,255,0.4), 0 12px 32px -8px rgba(34,197,94,0.35), 0 0 0 1px rgba(34,197,94,0.35)',
              }}
              className="flex items-center gap-1 px-2 py-0.5 rounded-full backdrop-blur-md text-green-500 text-[10px] font-medium">
              <div className="w-1.5 h-1.5 rounded-full bg-green-500" />
              Sandboxed
            </span>
          )}
          <button type="button" onClick={() => setShowWorkspace(prev => !prev)}
            title={showWorkspace ? 'Hide workspace' : 'Show workspace'}
            // Glossy translucent-orange pill, same recipe as the conversation
            // chips. Toggle state differentiated by font-weight + text color.
            style={{
              background:
                'linear-gradient(180deg, rgba(245,158,11,0.18) 0%, rgba(245,158,11,0.09) 100%)',
              boxShadow:
                'inset 0 1px 0 rgba(255,255,255,0.4), 0 12px 32px -8px rgba(245,158,11,0.35), 0 0 0 1px rgba(245,158,11,0.35)',
            }}
            className={`flex-shrink-0 flex items-center gap-1 px-2.5 py-1 rounded-md backdrop-blur-md text-xs transition-colors ${
              showWorkspace
                ? 'text-rt-primary font-semibold'
                : 'text-orange-500/70 font-medium hover:text-orange-500'
            }`}>
            <Monitor className="w-3.5 h-3.5" />
            <span>Workspace</span>
          </button>
          <div className="flex gap-1 items-center">
            {fileOpsTools.length > 0 && (
              <div className="relative">
                <button
                  type="button"
                  onClick={() => setFileOpsOpen((o) => !o)}
                  onBlur={() => setTimeout(() => setFileOpsOpen(false), 150)}
                  className={`flex items-center gap-1 p-1.5 rounded-lg transition-all text-xs font-medium ${
                    fileOpsTools.some((t) => t.available && t.enabled !== false)
                      ? 'text-rt-primary hover:bg-rt-primary/10'
                      : 'text-rt-text-muted hover:bg-rt-surface'
                  }`}
                  title="Files: read, write, delete, download"
                >
                  <FileText className="w-4 h-4" />
                  <span>File Operations</span>
                  <ChevronDown className={`w-3.5 h-3.5 transition-transform ${fileOpsOpen ? 'rotate-180' : ''}`} />
                </button>
                {fileOpsOpen && (
                  <div className="absolute top-full left-0 mt-1 py-2 px-2 min-w-[180px] bg-rt-bg-light border border-rt-border rounded-lg shadow-xl z-50">
                    <div className="text-[10px] font-semibold text-rt-text-muted uppercase tracking-wide mb-2 px-1.5">File Operations</div>
                    {fileOpsTools.map((t) => {
                      const displayName = t.name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
                      const isEnabled = t.available && (t.enabled !== false)
                      const canToggle = t.available
                      return (
                        <div key={t.name} className="flex items-center justify-between gap-2 py-1.5 px-1.5 rounded hover:bg-rt-surface/50">
                          <span className="text-xs text-rt-text">{displayName}</span>
                          <button
                            type="button"
                            onClick={() => canToggle && handleToolToggle(t.name, isEnabled)}
                            disabled={!canToggle || updateToolsMutation.isPending}
                            className={`text-xs px-2 py-0.5 rounded ${isEnabled ? 'bg-rt-primary/20 text-rt-primary' : 'bg-rt-surface text-rt-text-muted'}`}
                          >
                            {isEnabled ? 'On' : 'Off'}
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )}
            {standaloneTools.map((t) => {
              const Icon = getToolIcon(t.name)
              const displayName = getToolLabel(t.name)
              const shortHint = t.description ? `${t.description.slice(0, 42)}${t.description.length > 42 ? '…' : ''}` : ''
              const kbNoTeam = (t.name === 'knowledge_base' || t.name === 'search_knowledge_base') && !selectedTeamId
              const isEnabled = !kbNoTeam && t.available && (t.enabled !== false)
              const canToggle = t.available && !kbNoTeam
              return (
                <button key={t.name} type="button"
                  onClick={() => canToggle && handleToolToggle(t.name, isEnabled)}
                  disabled={!canToggle || updateToolsMutation.isPending}
                  title={`${displayName} (${isEnabled ? 'on' : 'off'})`}
                  className={`relative group p-1.5 rounded-lg transition-all ${canToggle ? isEnabled ? 'text-rt-primary hover:bg-rt-primary/10' : 'text-rt-text-muted/30 hover:text-rt-text-muted/50 hover:bg-rt-surface' : 'text-rt-text-muted/20 cursor-not-allowed'}`}>
                  <Icon className="w-4 h-4" />
                  <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 px-2.5 py-1.5 bg-rt-bg-light border border-rt-border rounded-md text-[11px] leading-snug text-rt-text w-max max-w-[min(280px,calc(100vw-2rem))] opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-50 shadow-lg whitespace-normal text-left">
                    <div className="font-medium text-rt-text">{displayName}</div>
                    {shortHint && <div className="text-rt-text-muted mt-1 break-words">{shortHint}</div>}
                    {canToggle && <div className="text-rt-text-muted/80 mt-0.5">{isEnabled ? 'Click: off' : 'Click: on'}</div>}
                    <div className="absolute top-full left-1/2 -translate-x-1/2 -mt-px"><div className="w-1.5 h-1.5 bg-rt-bg-light border-r border-b border-rt-border rotate-45" /></div>
                  </div>
                </button>
              )
            })}
          </div>
        </div>
      </div>

      {/* Conversations row — horizontal, below the team bar */}
      <div className="px-8 py-2 border-b border-rt-border bg-rt-bg-light/50 flex items-center gap-2 overflow-x-auto">
          <button onClick={handleNewConversation}
            className="flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-rt-primary/10 hover:bg-rt-primary/20 text-rt-primary text-xs font-medium transition-colors">
            <Plus className="w-3.5 h-3.5" /> New
          </button>
          {conversations.length > 0 && (
            <button onClick={handleDeleteAllConversations}
              // Glossy translucent-red pill, same recipe as the conversation
              // chips but in red-500 (#ef4444) for the destructive action.
              style={{
                background:
                  'linear-gradient(180deg, rgba(239,68,68,0.18) 0%, rgba(239,68,68,0.09) 100%)',
                boxShadow:
                  'inset 0 1px 0 rgba(255,255,255,0.4), 0 12px 32px -8px rgba(239,68,68,0.35), 0 0 0 1px rgba(239,68,68,0.35)',
              }}
              className="flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg backdrop-blur-md text-red-400 text-xs font-medium transition-colors hover:text-red-300"
              title="Delete all conversations">
              <Trash2 className="w-3.5 h-3.5" /> Clear All
            </button>
          )}
          {conversations.map((convo) => (
            <div key={convo.conversation_id}
              // Glossy translucent-orange pill, same recipe as the Ask AI card.
              // Active vs inactive only differs by font-weight + text color.
              style={{
                background:
                  'linear-gradient(180deg, rgba(245,158,11,0.18) 0%, rgba(245,158,11,0.09) 100%)',
                boxShadow:
                  'inset 0 1px 0 rgba(255,255,255,0.4), 0 12px 32px -8px rgba(245,158,11,0.35), 0 0 0 1px rgba(245,158,11,0.35)',
              }}
              className={`group flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg cursor-pointer transition-colors text-xs backdrop-blur-md ${
                activeConversationId === convo.conversation_id
                  ? 'text-rt-primary font-semibold'
                  : 'text-orange-500/80'
              }`}>
              {editingTitle === convo.conversation_id ? (
                <form className="flex items-center gap-1" onSubmit={(e) => { e.preventDefault(); handleRenameConversation(convo.conversation_id) }}>
                  <input value={editTitleValue} onChange={(e) => setEditTitleValue(e.target.value)}
                    className="w-24 bg-rt-surface border border-rt-border rounded px-1.5 py-0.5 text-xs focus:outline-none focus:border-rt-primary"
                    autoFocus onBlur={() => setEditingTitle(null)} onClick={(e) => e.stopPropagation()} />
                </form>
              ) : (
                <>
                  <div className="flex items-center gap-1.5 min-w-0" onClick={async () => {
                    setActiveConversationId(convo.conversation_id)
                    // Only fetch from the backend if this conversation isn't already
                    // present in our in-memory sessions map (e.g. first click after
                    // page load). If it's already in memory, switching is purely a
                    // UI change — any in-flight stream keeps running in the background.
                    const inMemory = useOpenCodeStore.getState().sessions[convo.conversation_id]
                    if (inMemory?.sessionId) return
                    if (convo.opencode_session_id) {
                      try {
                        await ocStore.loadSession(convo.conversation_id, convo.opencode_session_id)
                      } catch (err: any) {
                        if (err?.message === 'SESSION_NOT_FOUND') {
                          // Dead upstream session (sidecar restart, etc.). Load history
                          // from the ReTrace DB so the user can still see what was said
                          // before. We don't auto-create a fresh OC session here —
                          // handleExecute will do that when the user sends a new message.
                          await ocStore.loadFromDb(convo.conversation_id)
                          toast('Previous agent session expired. Showing saved history; send a message to start a fresh session.', { icon: 'ℹ️' })
                        } else {
                          toast.error('Failed to load conversation')
                        }
                      }
                    } else {
                      // No OC session ever linked — but DB may still have history
                      // (e.g. older conversations from before this integration).
                      await ocStore.loadFromDb(convo.conversation_id)
                    }
                  }}>
                    <MessageSquare className="w-3 h-3 flex-shrink-0 opacity-50" />
                    <span className="truncate max-w-[120px]">{convo.title}</span>
                    <span className="text-[10px] opacity-50 flex-shrink-0">({convo.message_count})</span>
                    {runningConversations.has(convo.conversation_id) && (
                      <Loader2 className="w-3 h-3 flex-shrink-0 text-rt-primary animate-spin" />
                    )}
                    {conversationsWithPerm.has(convo.conversation_id) && (
                      <div className="w-2 h-2 flex-shrink-0 rounded-full bg-amber-400 animate-pulse" title="Permission required" />
                    )}
                  </div>
                  <div className="flex gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
                    <button onClick={(e) => { e.stopPropagation(); setEditingTitle(convo.conversation_id); setEditTitleValue(convo.title) }}
                      className="p-0.5 rounded hover:bg-rt-border transition-colors" title="Rename"><Edit2 className="w-2.5 h-2.5" /></button>
                    <button onClick={(e) => handleDeleteConversation(convo.conversation_id, e)}
                      className="p-0.5 rounded hover:bg-red-500/20 text-red-400 transition-colors" title="Delete"><Trash2 className="w-2.5 h-2.5" /></button>
                  </div>
                </>
              )}
            </div>
          ))}
          {conversations.length === 0 && <span className="text-[11px] text-rt-text-muted/50 italic">No conversations yet — start typing or click New</span>}
        </div>

      {/* Main content area: resizable split when workspace is visible */}
      <div ref={splitContainerRef} className="flex-1 flex overflow-hidden">
      {/* Left: Chat panel */}
      <div
        className={`flex flex-col overflow-hidden relative ${isDragOver ? 'ring-2 ring-blue-400/60 ring-inset' : ''}`}
        style={{ width: showWorkspace ? `${chatWidthPct}%` : '100%' }}
        onDragEnter={e => {
          // Only react to file drags — typing text drags shouldn't trigger.
          if (!e.dataTransfer.types.includes('Files')) return
          e.preventDefault()
          dragDepthRef.current += 1
          if (dragDepthRef.current === 1) setIsDragOver(true)
        }}
        onDragOver={e => {
          if (!e.dataTransfer.types.includes('Files')) return
          // Prevent the browser's default "navigate to file" behavior.
          e.preventDefault()
          e.dataTransfer.dropEffect = 'copy'
        }}
        onDragLeave={e => {
          if (!e.dataTransfer.types.includes('Files')) return
          dragDepthRef.current = Math.max(0, dragDepthRef.current - 1)
          if (dragDepthRef.current === 0) setIsDragOver(false)
        }}
        onDrop={e => {
          if (!e.dataTransfer.files || e.dataTransfer.files.length === 0) return
          e.preventDefault()
          dragDepthRef.current = 0
          setIsDragOver(false)
          ingestFiles(e.dataTransfer.files)
        }}
      >
      {/* Conversation area */}
      <div ref={scrollContainerRef} className="flex-1 overflow-auto p-8">
        {/* Permission request banner — show whichever input field is most
            relevant for the requested tool so the user can decide informedly:
            bash → command, read/write/edit → filePath, webfetch → url, etc.
            Falls back to a JSON snippet for anything else. */}
        {pendingPermission && (() => {
          const input = pendingPermission.input || {}
          const summary = (
            input.command
              || input.filePath
              || input.file_path
              || input.path
              || input.url
              || input.pattern
              || (typeof input === 'object' && Object.keys(input).length > 0
                  ? JSON.stringify(input).slice(0, 200)
                  : '')
          )
          return (
            <div className="mb-4 px-4 py-3 rounded-lg border border-amber-500/30 bg-amber-500/10 flex items-center gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 text-sm font-medium text-amber-400">
                  <Code className="w-3.5 h-3.5" />
                  Permission Request
                </div>
                <p className="text-xs text-rt-text-muted mt-0.5 truncate">
                  <span className="font-mono text-amber-300">{pendingPermission.tool}</span>
                  {summary && (
                    <span className="ml-2 font-mono text-rt-text-muted/70" title={typeof summary === 'string' ? summary : ''}>
                      {summary}
                    </span>
                  )}
                </p>
              </div>
            <button type="button" onClick={() => activeConversationId && ocStore.respondPermission(activeConversationId, 'once')}
              className="px-3 py-1.5 text-xs font-medium rounded-md bg-amber-500/20 text-amber-300 border border-amber-500/30 hover:bg-amber-500/30 transition-colors">
              Allow Once
            </button>
            <button type="button" onClick={() => activeConversationId && ocStore.respondPermission(activeConversationId, 'always')}
              className="px-3 py-1.5 text-xs font-medium rounded-md bg-green-500/20 text-green-300 border border-green-500/30 hover:bg-green-500/30 transition-colors">
              Always Allow
            </button>
              <button type="button" onClick={() => activeConversationId && ocStore.respondPermission(activeConversationId, 'reject')}
                className="px-3 py-1.5 text-xs font-medium rounded-md bg-red-500/20 text-red-300 border border-red-500/30 hover:bg-red-500/30 transition-colors">
                Deny
              </button>
            </div>
          )
        })()}
        <MessageList
          messages={ocMessages}
          actions={{
            onLearnThis: handleLearnThis,
            onCreateSOP: handleCreateSOP,
            onCreateDoc: handleCreateDoc,
            onApproveSOP: handleApproveSOP,
            onEditSOP: handleEditSOP,
            onApproveDoc: handleApproveDoc,
            onEditDoc: handleEditDoc,
            onUpdatePart: (partId, updates) => { if (activeConversationId) ocStore.updatePart(activeConversationId, partId, updates) },
            onRevertTo: async (messageId) => {
              if (!activeConversationId) return
              await ocStore.revertToMessage(activeConversationId, messageId)
            },
            onForkAt: async (messageId) => {
              // Reuses the existing forkSession path. After fork, the store
              // returns the new conversation id and we navigate into it.
              if (!activeConversationId) return
              const result = await ocStore.forkSession(activeConversationId, messageId)
              if (result?.conversationId) {
                setActiveConversationId(result.conversationId)
                refetchConversations()
                toast.success('Forked to a new conversation')
              } else {
                toast.error('Fork failed')
              }
            },
            onSelectPrompt: (text) => setTask(text),
            allowLearnThis: !!selectedTeamId,
            isCreatingSOP,
            isCreatingDoc,
          }}
        />
        {/* Streaming indicator handled per-turn inside AssistantTurn — see
            MessageList. Tool-call pulse handled per-tool inside ToolCallBlock. */}
        {streamingSOP !== null && (
          <div className="card border-emerald-500/30 bg-emerald-500/5 mt-4">
            <div className="flex items-center gap-2 mb-3 text-xs text-emerald-400">
              <ClipboardList className="w-3.5 h-3.5" />
              <span className="font-medium">Automation</span>
              <Loader2 className="w-3 h-3 animate-spin ml-auto" />
            </div>
            <div className="prose prose-sm max-w-none"><ReactMarkdown>{streamingSOP}</ReactMarkdown></div>
          </div>
        )}
        {streamingDoc !== null && (
          <div className="card border-blue-500/30 bg-blue-500/5 mt-4">
            <div className="flex items-center gap-2 mb-3 text-xs text-blue-400">
              <FileText className="w-3.5 h-3.5" />
              <span className="font-medium">Documentation</span>
              <Loader2 className="w-3 h-3 animate-spin ml-auto" />
            </div>
            <div className="prose prose-sm max-w-none"><ReactMarkdown>{streamingDoc}</ReactMarkdown></div>
          </div>
        )}
        <div ref={scrollRef} />
      </div>

      {/* Input area */}
      <div className="px-4 py-3 border-t border-rt-border/50 bg-rt-bg-light relative">
        {paletteOpen && (
          <CommandPalette
            query={task}
            onSelect={handlePaletteSelect}
            onClose={() => { /* let value drive — re-open when task starts with `/` again */ }}
            handlers={paletteHandlers}
          />
        )}
        {mentionOpen && (
          <MentionMenu
            agents={filteredAgents}
            highlightedIdx={mentionHi}
            onHighlight={setMentionHi}
            onSelect={acceptMention}
            onClose={() => setMentionQuery(null)}
            anchorRef={textareaRef}
          />
        )}
        {pendingAttachments.length > 0 && (
          <div className="max-w-3xl mx-auto mb-2 flex flex-wrap gap-2">
            {pendingAttachments.map((att, idx) => (
              <div
                key={idx}
                className="flex items-center gap-2 rounded border border-rt-border/40 bg-rt-surface/40 px-2 py-1.5"
                title={`${att.filename} (${att.mime})`}
              >
                {isImageMime(att.mime) ? (
                  <img src={att.url} alt={att.filename} className="w-10 h-10 rounded object-cover" />
                ) : (
                  <div className="w-10 h-10 rounded bg-rt-bg/60 flex items-center justify-center">
                    <FileIcon className="w-4 h-4 text-rt-text-muted/70" />
                  </div>
                )}
                <div className="flex flex-col min-w-0 max-w-[180px]">
                  <span className="text-xs text-rt-text truncate">{att.filename}</span>
                  <span className="text-[10px] text-rt-text-muted/60">{formatBytes(Math.round((att.url.length - att.url.indexOf(',') - 1) * 0.75))}</span>
                </div>
                <button
                  type="button"
                  onClick={() => setPendingAttachments(prev => prev.filter((_, i) => i !== idx))}
                  className="text-rt-text-muted/50 hover:text-red-400 transition-colors"
                  title="Remove"
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            ))}
          </div>
        )}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={e => {
            ingestFiles(e.target.files)
            // Reset so picking the same file again still fires change.
            if (e.target) e.target.value = ''
          }}
        />
        {isDragOver && (
          <div className="absolute inset-0 z-10 pointer-events-none flex items-center justify-center bg-blue-500/10 backdrop-blur-[1px]">
            <div className="rounded-lg border-2 border-dashed border-blue-400/70 px-6 py-4 bg-rt-bg/90 text-sm text-blue-300">
              Drop files to attach
            </div>
          </div>
        )}
        {activeSession?.queuedMessages && activeSession.queuedMessages.length > 0 && (
          <div className="max-w-3xl mx-auto mb-2 flex flex-col gap-1">
            <div className="text-[10px] text-rt-text-muted/60 uppercase tracking-wide pl-1">
              {activeSession.queuedMessages.length} message{activeSession.queuedMessages.length === 1 ? '' : 's'} queued
            </div>
            {activeSession.queuedMessages.map((q) => (
              <div
                key={q.id}
                className="flex items-start gap-2 px-2.5 py-1.5 rounded border border-rt-border/40 bg-rt-surface/30 text-xs"
              >
                <span className="text-rt-text-muted/60 mt-0.5">↳</span>
                <span className="flex-1 text-rt-text-muted truncate" title={q.text}>{q.text}</span>
                <button
                  type="button"
                  onClick={() => activeConversationId && ocStore.cancelQueuedMessage(activeConversationId, q.id)}
                  className="text-rt-text-muted/50 hover:text-red-400 transition-colors"
                  title="Remove from queue"
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            ))}
          </div>
        )}
        <form onSubmit={handleSubmit} className="max-w-3xl mx-auto flex gap-2 items-end">
          <button
            type="button"
            onClick={() => handleSetAgent(displayAgent === 'build' ? 'plan' : 'build')}
            disabled={isRunning || agentLocked}
            title={
              agentLocked
                ? `Agent mode locked to ${displayAgent} for this conversation`
                : displayAgent === 'plan'
                  ? 'Plan mode — read-only analysis (click for Build)'
                  : 'Build mode — full access (click for Plan)'
            }
            className={`flex-shrink-0 p-2 rounded-lg border transition-all ${
              displayAgent === 'plan'
                ? 'bg-blue-500/20 border-blue-500/40 text-blue-400'
                : 'border-rt-border text-rt-text-muted hover:border-rt-text-muted hover:text-rt-text'
            }`}
          >
            {displayAgent === 'plan' ? <ClipboardList className="w-4 h-4" /> : <Bot className="w-4 h-4" />}
          </button>
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="flex-shrink-0 p-2 rounded-lg border border-rt-border text-rt-text-muted hover:border-rt-text-muted hover:text-rt-text transition-colors"
            title="Attach files (or drag-drop / paste)"
          >
            <Paperclip className="w-4 h-4" />
          </button>
          <textarea
            ref={textareaRef}
            value={task}
            onChange={e => {
              const v = e.target.value
              setTask(v)
              e.target.style.height = 'auto'
              e.target.style.height = Math.min(e.target.scrollHeight, 200) + 'px'
              if (historyIdx !== -1) setHistoryIdx(-1)
              refreshMentionState(v, e.target.selectionStart ?? v.length)
            }}
            onKeyUp={e => {
              // Cursor-position changes (arrow keys, click navigation) don't
              // fire onChange; sync the mention state here so e.g. clicking
              // mid-word after an `@` re-opens the menu.
              const ta = e.target as HTMLTextAreaElement
              refreshMentionState(ta.value, ta.selectionStart ?? ta.value.length)
            }}
            onClick={e => {
              const ta = e.target as HTMLTextAreaElement
              refreshMentionState(ta.value, ta.selectionStart ?? ta.value.length)
            }}
            onPaste={e => {
              const pastedFiles = filesFromClipboard(e.nativeEvent as ClipboardEvent)
              if (pastedFiles.length > 0) {
                e.preventDefault()
                ingestFiles(pastedFiles)
              }
            }}
            onKeyDown={e => {
              // Mention menu intercepts arrow keys + Enter + Tab when open.
              if (mentionOpen) {
                if (e.key === 'ArrowDown') {
                  e.preventDefault()
                  setMentionHi(i => Math.min(i + 1, filteredAgents.length - 1))
                  return
                }
                if (e.key === 'ArrowUp') {
                  e.preventDefault()
                  setMentionHi(i => Math.max(i - 1, 0))
                  return
                }
                if (e.key === 'Enter' || e.key === 'Tab') {
                  e.preventDefault()
                  const agent = filteredAgents[mentionHi]
                  if (agent) acceptMention(agent)
                  return
                }
                if (e.key === 'Escape') {
                  e.preventDefault()
                  setMentionQuery(null)
                  return
                }
              }
              if (e.key === 'Enter' && !e.shiftKey) {
                // When the input starts with `/`, the CommandPalette captures
                // Enter via its window keydown listener — just suppress the
                // form submit here so we don't double-fire.
                if (paletteOpen) {
                  e.preventDefault()
                  return
                }
                e.preventDefault()
                if (task.trim()) handleSubmit(e as any)
              } else if (e.key === 'Escape' && isRunning) {
                // Esc aborts the in-flight run, matching OC's UX.
                e.preventDefault()
                handleStop()
              } else if (e.key === 'ArrowUp') {
                // Recall a previous prompt — only when the composer is empty
                // OR we're already navigating history. Skip while the palette
                // is open (it owns up/down for command navigation).
                if (paletteOpen) return
                const history = activeSession?.recentPrompts ?? []
                if (history.length === 0) return
                if (historyIdx === -1 && task !== '') return
                e.preventDefault()
                const nextIdx = Math.min(historyIdx + 1, history.length - 1)
                setHistoryIdx(nextIdx)
                setTask(history[history.length - 1 - nextIdx])
              } else if (e.key === 'ArrowDown' && historyIdx !== -1) {
                if (paletteOpen) return
                e.preventDefault()
                const history = activeSession?.recentPrompts ?? []
                const nextIdx = historyIdx - 1
                if (nextIdx < 0) {
                  setHistoryIdx(-1)
                  setTask('')
                } else {
                  setHistoryIdx(nextIdx)
                  setTask(history[history.length - 1 - nextIdx])
                }
              }
            }}
            placeholder={isRunning ? 'Agent is working — your message will queue…' : 'Describe a task to perform...    (type / for commands)'}
            rows={1}
            style={{ borderColor: '#f59e0b' }}
            className="input flex-1 resize-none min-h-[44px] max-h-[200px]" />
          {isRunning && (
            <button type="button" onClick={handleStop} className="btn-secondary flex items-center gap-2 text-red-400 border-red-400/30 hover:bg-red-400/10" title="Stop (Esc)">
              <X className="w-4 h-4" /> Stop
            </button>
          )}
          <button type="submit" disabled={!task.trim()} className="btn-primary flex items-center gap-2" title={isRunning ? 'Queue message (sends after current turn)' : 'Send'}>
            <Play className="w-4 h-4" /> {isRunning ? 'Queue' : 'Go'}
          </button>
        </form>
        {displayAgent === 'plan' && (
          <p className="text-[11px] text-blue-400/70 mt-1.5 pl-1">Plan mode — read-only analysis (no file edits or commands)</p>
        )}
      </div>
      </div>{/* end chat panel */}

      {/* Resize handle */}
      {showWorkspace && (
        <div
          className="w-1 cursor-col-resize hover:bg-rt-primary-container/40 active:bg-rt-primary-container/60 transition-colors shrink-0 relative group"
          onMouseDown={() => { isDraggingRef.current = true; document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none' }}
        >
          <div className="absolute inset-y-0 -left-1 -right-1" />{/* wider hit area */}
          <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-1 h-8 rounded-full bg-rt-border group-hover:bg-rt-primary-container/60" />
        </div>
      )}

      {/* Right: Workspace panel (Browser + Terminal tabs) */}
      {showWorkspace && (
        <div className="flex flex-col overflow-hidden" style={{ width: `${100 - chatWidthPct}%` }}>
          {/* Tab bar */}
          <div className="flex items-center gap-1 px-3 py-1.5 border-b border-rt-border bg-rt-surface/30 shrink-0">
            {/* Colorless glass tab pills — same gloss mechanics (top
                highlight, soft shadow, faint edge) but no color tint
                since these tabs aren't a themed accent on their own.
                Active state = bold + brighter text only. */}
            {([
              { id: 'browser' as const, Icon: Globe, label: 'Browser' },
              { id: 'terminal' as const, Icon: Terminal, label: 'Terminal' },
              { id: 'files' as const, Icon: FolderOpen, label: 'Files' },
              { id: 'todos' as const, Icon: ListTodo, label: 'Todos' },
            ]).map(({ id, Icon, label }) => {
              const active = workspaceTab === id
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => setWorkspaceTab(id)}
                  style={{
                    background: active
                      ? 'linear-gradient(180deg, rgba(245,158,11,0.14) 0%, rgba(245,158,11,0.06) 100%)'
                      : 'linear-gradient(180deg, rgba(255,255,255,0.20) 0%, rgba(255,255,255,0.10) 100%)',
                    boxShadow: active
                      ? 'inset 0 1px 0 rgba(255,255,255,0.4), 0 6px 16px -6px rgba(245,158,11,0.2), 0 0 0 1px rgba(245,158,11,0.25)'
                      : 'inset 0 1px 0 rgba(255,255,255,0.4), 0 6px 16px -6px rgba(0,0,0,0.12), 0 0 0 1px rgba(255,255,255,0.18)',
                  }}
                  className={`flex items-center gap-1.5 px-3 py-1 rounded-md backdrop-blur-md text-xs font-medium transition-colors ${
                    active
                      ? 'text-rt-text'
                      : 'text-rt-text-muted hover:text-rt-text'
                  }`}
                >
                  <Icon className="w-3 h-3" />
                  {label}
                </button>
              )
            })}
          </div>
          {/* Stacked content — browser always renders; terminal overlays on top */}
          <div className="flex-1 relative overflow-hidden">
            {/* Browser — always visible underneath (isolate creates a stacking context
                so z-indices inside BrowserWorkspace don't leak above Terminal/Files) */}
            <div className="absolute inset-0 isolate">
              <BrowserWorkspace
                conversationId={activeConversationId}
                isVisible={showWorkspace}
                isAgentRunning={isRunning}
              />
            </div>
            {/* Terminal — overlays browser when selected */}
            <div className={`absolute inset-0 z-10 bg-rt-bg transition-opacity duration-200 ${
              workspaceTab === 'terminal' ? 'opacity-100 pointer-events-auto' : 'opacity-0 pointer-events-none'
            }`}>
              <TerminalPanel
                conversationId={activeConversationId}
                isVisible={showWorkspace && workspaceTab === 'terminal'}
                onToggle={() => setWorkspaceTab('browser')}
              />
            </div>
            {/* Files — overlays browser when selected */}
            <div className={`absolute inset-0 z-10 bg-rt-bg transition-opacity duration-200 ${
              workspaceTab === 'files' ? 'opacity-100 pointer-events-auto' : 'opacity-0 pointer-events-none'
            }`}>
              <SandboxFileExplorer
                conversationId={activeConversationId}
              />
            </div>
            {/* Todos — overlays browser when selected */}
            <div className={`absolute inset-0 z-10 bg-rt-bg transition-opacity duration-200 ${
              workspaceTab === 'todos' ? 'opacity-100 pointer-events-auto' : 'opacity-0 pointer-events-none'
            }`}>
              <TodosPanel
                opencodeSessionId={activeSession?.sessionId ?? null}
                isAgentRunning={isRunning}
              />
            </div>
          </div>
        </div>
      )}
      </div>{/* end split container */}
    </div>
  )
}

