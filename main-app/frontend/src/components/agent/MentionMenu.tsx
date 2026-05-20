import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Bot, Sparkles } from 'lucide-react'

/**
 * `@` typeahead popover for the composer — mirrors OC's `@<agent_name>` menu
 * (which surfaces `@build`, `@plan`, etc.). Picking an item inserts the agent
 * name into the textarea and stages an `{type:"agent", name, source}` part
 * for the next `sendMessage`, matching the wire shape OC uses.
 *
 * Anchored ABOVE the composer (bottom-up), unlike the right-click menu which
 * anchors at the pointer. Closes on Esc, blur, click-outside, or selection.
 */

export interface MentionAgent {
  name: string
  description?: string
}

interface Props {
  /** Filtered (or full) list of agents to render. */
  agents: MentionAgent[]
  /** Currently-highlighted index. Parent owns this so Enter / ArrowKeys can
   * be intercepted at the textarea level and forwarded as `onMove`. */
  highlightedIdx: number
  onHighlight: (idx: number) => void
  onSelect: (agent: MentionAgent) => void
  onClose: () => void
  /** Anchor element — the textarea. The popover positions itself just above
   * the bottom of the form so it appears to "grow up" from the composer. */
  anchorRef: React.RefObject<HTMLElement>
}

export default function MentionMenu({
  agents, highlightedIdx, onHighlight, onSelect, onClose, anchorRef,
}: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number; width: number; opacity: number }>(
    { left: 0, top: 0, width: 360, opacity: 0 },
  )

  // Position the menu directly above the anchor (composer textarea) so it
  // grows upward, OC-style. Recompute on agent-list size changes (the menu
  // height changes with the filtered set).
  useLayoutEffect(() => {
    const anchor = anchorRef.current
    const menu = ref.current
    if (!anchor || !menu) return
    const aRect = anchor.getBoundingClientRect()
    const mRect = menu.getBoundingClientRect()
    const margin = 8
    setPos({
      left: aRect.left,
      top: Math.max(margin, aRect.top - mRect.height - 6),
      width: Math.max(280, aRect.width),
      opacity: 1,
    })
  }, [anchorRef, agents.length])

  // Click-outside / blur / Esc / scroll dismissal.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    const onScroll = () => onClose()
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onScroll, { capture: true, passive: true })
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onScroll, { capture: true } as any)
    }
  }, [onClose])

  if (agents.length === 0) {
    return (
      <div
        ref={ref}
        className="fixed z-[9998] bg-rt-bg-light border border-rt-border rounded-md shadow-xl px-3 py-2 transition-opacity"
        style={{ left: pos.left, top: pos.top, width: pos.width, opacity: pos.opacity }}
      >
        <div className="text-xs text-rt-text-muted/60">No matching agents</div>
      </div>
    )
  }

  return (
    <div
      ref={ref}
      role="menu"
      className="fixed z-[9998] max-h-[280px] overflow-auto bg-rt-bg-light border border-rt-border rounded-md shadow-xl py-1 transition-opacity"
      style={{ left: pos.left, top: pos.top, width: pos.width, opacity: pos.opacity }}
    >
      <div className="px-3 py-1 text-[10px] uppercase tracking-wide text-rt-text-muted/60">Agents</div>
      {agents.map((agent, i) => {
        const isHi = i === highlightedIdx
        return (
          <button
            key={agent.name}
            type="button"
            role="menuitem"
            onMouseEnter={() => onHighlight(i)}
            onMouseDown={(e) => { e.preventDefault() /* keep textarea focus */ }}
            onClick={() => onSelect(agent)}
            className={`w-full flex items-start gap-2 px-3 py-2 text-left transition-colors ${
              isHi ? 'bg-rt-surface/60' : 'hover:bg-rt-surface/40'
            }`}
          >
            <div className={`mt-0.5 w-4 h-4 flex items-center justify-center rounded ${
              isHi ? 'text-blue-300' : 'text-rt-text-muted/70'
            }`}>
              {agent.name === 'plan' ? <Sparkles className="w-3.5 h-3.5" /> : <Bot className="w-3.5 h-3.5" />}
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium text-rt-text">@{agent.name}</div>
              {agent.description && (
                <div className="text-[10px] text-rt-text-muted/60 truncate">{agent.description}</div>
              )}
            </div>
          </button>
        )
      })}
    </div>
  )
}
