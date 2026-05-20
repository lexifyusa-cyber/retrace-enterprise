import { useEffect, useRef, useState } from 'react'
import { GitFork, Loader2, X, ArrowRight } from 'lucide-react'
import { agentApi } from '@/utils/api'

/**
 * Modal-style panel listing children of the current session — surfaces the
 * fork tree OpenCode exposes via `GET /session/{id}/children`. Each row
 * shows the forked session's title + timestamp + model; clicking navigates
 * the chat panel to that conversation (when the fork has a linked ReTrace
 * conversation_id) or shows a notice when it doesn't.
 *
 * Anchored to the header overflow menu (caller manages open/close).
 */

export interface ForkChild {
  id: string
  title?: string
  slug?: string
  time?: { created?: number; updated?: number }
  parentID?: string
  model?: { id?: string; providerID?: string }
  agent?: string
  conversation_id?: string | null
}

export default function ForksPanel({
  sessionId,
  onClose,
  onPick,
}: {
  sessionId: string
  onClose: () => void
  /** Called with the child's conversation_id when the user picks a fork that
   * we can navigate to in ReTrace. Caller switches the active conversation. */
  onPick: (conversationId: string) => void
}) {
  const [loading, setLoading] = useState(true)
  const [children, setChildren] = useState<ForkChild[]>([])
  const [error, setError] = useState<string | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  // Load child list on mount.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    agentApi.opencode.sessionChildren(sessionId)
      .then(rows => {
        if (cancelled) return
        setChildren(Array.isArray(rows) ? rows : [])
        setLoading(false)
      })
      .catch(err => {
        if (cancelled) return
        console.warn('[ForksPanel] fetch failed', err)
        setError('Failed to load forks')
        setLoading(false)
      })
    return () => { cancelled = true }
  }, [sessionId])

  // Dismiss on outside-click or Esc.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const fmtTime = (ms?: number) => {
    if (!ms) return ''
    const d = new Date(ms)
    const today = new Date()
    const isToday = d.toDateString() === today.toDateString()
    return isToday
      ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  }

  return (
    <div className="fixed inset-0 z-[9000] bg-black/30 flex items-center justify-center p-4" onMouseDown={onClose}>
      <div
        ref={ref}
        className="bg-rt-bg-light border border-rt-border rounded-lg shadow-2xl max-w-md w-full max-h-[70vh] flex flex-col"
        onMouseDown={e => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-4 py-3 border-b border-rt-border/60">
          <GitFork className="w-4 h-4 text-rt-text-muted" />
          <span className="text-sm font-medium text-rt-text flex-1">Forks of this session</span>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded text-rt-text-muted/60 hover:text-rt-text hover:bg-rt-surface transition-colors"
            title="Close"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
        <div className="flex-1 overflow-auto p-2">
          {loading ? (
            <div className="flex items-center gap-2 px-3 py-4 text-xs text-rt-text-muted/70">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              Loading forks…
            </div>
          ) : error ? (
            <div className="px-3 py-4 text-xs text-red-300">{error}</div>
          ) : children.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-rt-text-muted/60">
              No forks yet. Use the right-click menu on a message to fork from here.
            </div>
          ) : (
            <div className="space-y-1">
              {children.map(c => {
                const navigable = !!c.conversation_id
                return (
                  <button
                    key={c.id}
                    type="button"
                    disabled={!navigable}
                    onClick={() => { if (c.conversation_id) onPick(c.conversation_id) }}
                    title={navigable ? 'Open this fork' : 'Forked session — not linked to a ReTrace conversation'}
                    className={`w-full flex items-start gap-2 px-3 py-2 rounded text-left transition-colors ${
                      navigable
                        ? 'hover:bg-rt-surface/60 cursor-pointer'
                        : 'opacity-60 cursor-not-allowed'
                    }`}
                  >
                    <GitFork className="w-3.5 h-3.5 text-rt-text-muted/60 flex-shrink-0 mt-0.5" />
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-medium text-rt-text truncate">{c.title || c.slug || c.id}</div>
                      <div className="text-[10px] text-rt-text-muted/60 flex items-center gap-2 mt-0.5">
                        {c.agent && <span>{c.agent}</span>}
                        {c.model?.id && <span className="truncate">{c.model.id}</span>}
                        {c.time?.created && <span>· {fmtTime(c.time.created)}</span>}
                      </div>
                    </div>
                    {navigable && <ArrowRight className="w-3 h-3 text-rt-text-muted/40 flex-shrink-0 mt-1" />}
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
