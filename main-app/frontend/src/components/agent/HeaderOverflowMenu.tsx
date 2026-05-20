import { useEffect, useRef, useState } from 'react'
import { MoreHorizontal, GitFork, Share2, FileText, X } from 'lucide-react'

/**
 * A small `⋯` button that opens a popover with secondary header actions
 * (Share, Summarize, Fork, Stop server) so the prominent header stays focused
 * on title + mode toggle + cost + model.
 *
 * Items are conditionally enabled based on session state so the menu doesn't
 * surface actions that would no-op. Each entry closes the menu on click.
 */
export default function HeaderOverflowMenu({
  canFork, canShare, canSummarize, canStopServer,
  onFork, onShare, onSummarize, onStopServer,
}: {
  canFork: boolean
  canShare: boolean
  canSummarize: boolean
  canStopServer: boolean
  onFork: () => void
  onShare: () => void
  onSummarize: () => void
  onStopServer: () => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  const items: Array<{
    label: string
    Icon: typeof GitFork
    onClick: () => void
    enabled: boolean
    danger?: boolean
  }> = [
    { label: 'Share session', Icon: Share2, onClick: onShare, enabled: canShare },
    { label: 'Summarize session', Icon: FileText, onClick: onSummarize, enabled: canSummarize },
    { label: 'Fork session', Icon: GitFork, onClick: onFork, enabled: canFork },
    { label: 'Stop OpenCode server', Icon: X, onClick: onStopServer, enabled: canStopServer, danger: true },
  ]

  // Hide the button entirely if every item is disabled (no session).
  if (!items.some(it => it.enabled)) return null

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        title="More actions"
        className={`p-1.5 rounded border border-rt-border/30 transition-colors ${
          open
            ? 'text-rt-text bg-rt-surface/60'
            : 'text-rt-text-muted/50 hover:text-rt-text hover:border-rt-text-muted/40'
        }`}
      >
        <MoreHorizontal className="w-3.5 h-3.5" />
      </button>
      {open && (
        <div className="absolute top-full right-0 mt-1 min-w-[200px] bg-rt-bg-light border border-rt-border rounded-md shadow-xl py-1 z-50">
          {items.map((it, i) => {
            const Icon = it.Icon
            if (!it.enabled) return null
            return (
              <button
                key={i}
                type="button"
                onClick={() => { setOpen(false); it.onClick() }}
                className={`w-full text-left px-3 py-1.5 text-xs flex items-center gap-2 transition-colors ${
                  it.danger
                    ? 'text-rt-text hover:bg-red-500/15 hover:text-red-400'
                    : 'text-rt-text hover:bg-rt-surface'
                }`}
              >
                <Icon className="w-3.5 h-3.5 opacity-70" />
                {it.label}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
