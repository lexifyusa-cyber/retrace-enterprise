import { useEffect, useState } from 'react'
import { ListTodo, Loader2, CheckCircle2, Circle, Clock } from 'lucide-react'
import { agentApi } from '@/utils/api'

interface Todo {
  id?: string
  content?: string
  status?: string  // OC values: "pending" | "in_progress" | "completed" | "cancelled"
  priority?: string
}

interface TodosPanelProps {
  /** OpenCode session id. When null/undefined the panel renders an empty
   * state — the agent has no live session yet. */
  opencodeSessionId: string | null
  /** Whether the agent is currently streaming. Used to decide poll cadence. */
  isAgentRunning: boolean
}

/**
 * Reads OpenCode's per-session TODO list and renders it as a checklist.
 *
 * OC's `GET /session/{id}/todo` is the canonical source — the agent is told
 * (via the `todoread` / `todowrite` tools) to maintain this list as it plans
 * and works. Surfacing it here gives the user visibility into the agent's
 * intent without forcing them to read the full tool-call stream.
 *
 * Poll strategy: 3s while the agent is actively streaming, 15s while idle —
 * BUT only when the tab is visible and the user has interacted recently.
 * An open-but-idle/background tab stops polling entirely, so it can't keep
 * the (scale-to-zero) backend awake. Polling resumes on return.
 */
export default function TodosPanel({ opencodeSessionId, isAgentRunning }: TodosPanelProps) {
  const [todos, setTodos] = useState<Todo[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!opencodeSessionId) {
      setTodos([])
      return
    }
    let cancelled = false
    let lastActivity = Date.now()
    const IDLE_MS = 60_000 // after 60s of no interaction (and agent idle), stop polling

    const fetchTodos = async () => {
      try {
        const data = await agentApi.opencode.getTodos(opencodeSessionId)
        if (cancelled) return
        // OC may return the list directly, or wrapped — handle both.
        const list: Todo[] = Array.isArray(data) ? data : Array.isArray((data as any)?.todos) ? (data as any).todos : []
        setTodos(list)
        setError(null)
      } catch (err: any) {
        if (cancelled) return
        setError(err?.message || 'Failed to load todos')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    // Only hit the backend when it's worth it. A forever-polling idle tab kept
    // fly-proxy from ever seeing the machine idle (defeating scale-to-zero).
    // Poll while the agent streams; otherwise only while visible AND recently active.
    const shouldPoll = () => {
      if (cancelled || document.hidden) return false
      if (isAgentRunning) return true
      return Date.now() - lastActivity < IDLE_MS
    }
    const tick = () => { if (shouldPoll()) fetchTodos() }

    const markActivity = () => { lastActivity = Date.now() }
    const onVisibility = () => { if (!document.hidden) { markActivity(); fetchTodos() } }
    const activityEvents = ['mousemove', 'keydown', 'pointerdown', 'wheel'] as const
    activityEvents.forEach((e) => window.addEventListener(e, markActivity, { passive: true }))
    document.addEventListener('visibilitychange', onVisibility)

    setLoading(true)
    if (shouldPoll()) fetchTodos()
    const interval = setInterval(tick, isAgentRunning ? 3000 : 15000)

    return () => {
      cancelled = true
      clearInterval(interval)
      activityEvents.forEach((e) => window.removeEventListener(e, markActivity))
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [opencodeSessionId, isAgentRunning])

  if (!opencodeSessionId) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center px-6 text-rt-text-muted">
        <ListTodo className="w-10 h-10 mb-4 opacity-40" />
        <p className="text-sm">No agent session yet.</p>
        <p className="text-xs mt-1 opacity-60">Start a conversation to see the agent's working plan.</p>
      </div>
    )
  }

  if (loading && todos.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-rt-text-muted">
        <Loader2 className="w-5 h-5 animate-spin" />
      </div>
    )
  }

  if (error && todos.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full px-6 text-center text-rt-text-muted">
        <p className="text-sm text-red-400">Couldn't load todos</p>
        <p className="text-xs mt-1 opacity-60">{error}</p>
      </div>
    )
  }

  if (todos.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center px-6 text-rt-text-muted">
        <ListTodo className="w-10 h-10 mb-4 opacity-40" />
        <p className="text-sm">No todos yet.</p>
        <p className="text-xs mt-1 opacity-60">
          The agent will populate this as it plans and executes.
        </p>
      </div>
    )
  }

  // Partition: in-progress first, then pending, then completed. Cancelled last.
  const rank: Record<string, number> = { in_progress: 0, pending: 1, completed: 2, cancelled: 3 }
  const sorted = [...todos].sort(
    (a, b) => (rank[a.status || 'pending'] ?? 1) - (rank[b.status || 'pending'] ?? 1)
  )
  const remaining = todos.filter(t => t.status !== 'completed' && t.status !== 'cancelled').length

  return (
    <div className="h-full overflow-y-auto px-4 py-4">
      <div className="flex items-center justify-between mb-3 px-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <ListTodo className="w-4 h-4 text-rt-primary" />
          <span>Agent Todos</span>
          <span className="text-xs text-rt-text-muted">({remaining} remaining)</span>
        </div>
        {isAgentRunning && (
          <Loader2 className="w-3.5 h-3.5 animate-spin text-rt-text-muted" />
        )}
      </div>
      <ul className="space-y-1.5">
        {sorted.map((t, i) => (
          <TodoRow key={t.id || `todo-${i}`} todo={t} />
        ))}
      </ul>
    </div>
  )
}

function TodoRow({ todo }: { todo: Todo }) {
  const status = todo.status || 'pending'
  let Icon = Circle
  let iconClass = 'text-rt-text-muted/50'
  let textClass = 'text-rt-text'
  if (status === 'in_progress') {
    Icon = Clock
    iconClass = 'text-rt-primary animate-pulse'
    textClass = 'text-rt-text font-medium'
  } else if (status === 'completed') {
    Icon = CheckCircle2
    iconClass = 'text-rt-success'
    textClass = 'text-rt-text-muted line-through'
  } else if (status === 'cancelled') {
    Icon = Circle
    iconClass = 'text-rt-text-muted/30'
    textClass = 'text-rt-text-muted/50 line-through'
  }

  return (
    <li className="flex items-start gap-2 px-2 py-1.5 rounded-md hover:bg-rt-surface/50 transition-colors">
      <Icon className={`w-3.5 h-3.5 mt-0.5 shrink-0 ${iconClass}`} />
      <span className={`text-sm leading-snug ${textClass}`}>{todo.content || '(no description)'}</span>
    </li>
  )
}
