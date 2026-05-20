import { useEffect, useMemo, useRef, useState } from 'react'
import { Database } from 'lucide-react'
import { agentApi } from '@/utils/api'

/** A markdown slash command exposed by OC's `/command` endpoint. */
export interface PaletteCommand {
  /** Slash-form name without the leading `/` — e.g. `commit`, `spellcheck`. */
  name: string
  description?: string
}

/**
 * Inline dropdown that appears above the chat input when the user types `/`.
 * Renders OC's markdown slash commands (the .md files under
 * `.opencode/command/`). When the user accepts one, the parent drops the
 * `/name args` text into the textarea and submits it — OC's session-prompt
 * handler then expands the markdown template into the full LLM prompt.
 *
 * ReTrace does not add its own slash commands here. Session actions
 * (share / compact / fork / revert / etc.) and ReTrace-specific actions
 * (learn / automation / documentation / todos) all have dedicated UI
 * buttons elsewhere (header overflow menu, chat-input undo/redo, inline
 * action bar on each assistant message, workspace tab bar). Adding slash
 * variants of those just clutters the palette.
 */
export default function CommandPalette({
  query,
  onSelect,
  onClose,
}: {
  /** The raw textarea value. Starts with `/` if the palette should show. */
  query: string
  /** Called when the user accepts a command. The parent should clear the
   * input and run the command. */
  onSelect: (command: PaletteCommand, args: string) => void
  /** Called when the user dismisses the palette (Esc). */
  onClose: () => void
}) {
  const [highlight, setHighlight] = useState(0)
  const [commands, setCommands] = useState<PaletteCommand[]>([])
  const fetched = useRef(false)

  // Lazy-load OC's markdown command list once when the palette first opens.
  // OC down → empty list → "no commands available" placeholder.
  useEffect(() => {
    if (fetched.current) return
    fetched.current = true
    agentApi.opencode.listCommands().then((list: any) => {
      if (Array.isArray(list)) setCommands(list as PaletteCommand[])
    }).catch(() => { /* backend down — palette renders empty */ })
  }, [])

  // Filter by what the user typed after the leading `/`. Match the name
  // prefix or substring (case-insensitive). Anything after whitespace is
  // treated as args.
  const filter = query.startsWith('/') ? query.slice(1).trim().toLowerCase() : ''
  const matches = useMemo(() => {
    if (!filter) return commands
    const word = filter.split(/\s+/)[0]
    return commands.filter(c => c.name.toLowerCase().includes(word))
  }, [commands, filter])

  // Clamp the highlight whenever the match set changes.
  useEffect(() => {
    setHighlight(h => (matches.length === 0 ? 0 : Math.min(h, matches.length - 1)))
  }, [matches.length])

  // Listen for ↑ / ↓ / Enter / Esc / Tab at the window level so the user can
  // drive the palette from their keyboard while the textarea retains focus.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setHighlight(h => Math.min(h + 1, matches.length - 1))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setHighlight(h => Math.max(h - 1, 0))
      } else if (e.key === 'Enter' && !e.shiftKey && matches[highlight]) {
        e.preventDefault()
        const cmd = matches[highlight]
        const slashForm = `/${cmd.name}`
        const args = query.length > slashForm.length ? query.slice(slashForm.length).trim() : ''
        onSelect(cmd, args)
      } else if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      } else if (e.key === 'Tab' && matches[highlight]) {
        // Tab auto-completes to the full slash form (no args).
        e.preventDefault()
        onSelect(matches[highlight], '')
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [matches, highlight, query, onSelect, onClose])

  if (matches.length === 0) {
    return (
      <div className="absolute bottom-full left-0 right-0 mb-2 mx-auto max-w-3xl px-4 py-2 text-xs text-rt-text-muted/60 italic">
        {commands.length === 0 ? 'No commands available' : `No commands match "${filter}"`}
      </div>
    )
  }

  return (
    <div className="absolute bottom-full left-0 right-0 mb-2 max-w-3xl mx-auto bg-rt-bg-light border border-rt-border rounded-lg shadow-xl overflow-hidden">
      <div className="px-3 py-1.5 text-[10px] font-semibold text-rt-text-muted/60 uppercase tracking-wide border-b border-rt-border/40">
        Commands · {matches.length}
      </div>
      <div className="max-h-[280px] overflow-y-auto py-1">
        {matches.map((cmd, i) => {
          const isHi = i === highlight
          return (
            <button
              key={cmd.name}
              type="button"
              onMouseEnter={() => setHighlight(i)}
              onClick={() => {
                const slashForm = `/${cmd.name}`
                const args = query.length > slashForm.length ? query.slice(slashForm.length).trim() : ''
                onSelect(cmd, args)
              }}
              className={`w-full flex items-center gap-3 px-3 py-1.5 text-left transition-colors ${
                isHi ? 'bg-rt-primary/15' : 'hover:bg-rt-surface/60'
              }`}
            >
              <Database className="w-3.5 h-3.5 flex-shrink-0 text-rt-text-muted" />
              <div className="flex-1 min-w-0">
                <span className={`text-xs font-mono font-medium ${isHi ? 'text-rt-primary' : 'text-rt-text'}`}>
                  /{cmd.name}
                </span>
                {cmd.description && (
                  <div className="text-[11px] text-rt-text-muted/60 truncate">{cmd.description}</div>
                )}
              </div>
              {isHi && (
                <span className="text-[10px] text-rt-text-muted/50 flex-shrink-0">↵</span>
              )}
            </button>
          )
        })}
      </div>
      <div className="px-3 py-1 border-t border-rt-border/40 text-[10px] text-rt-text-muted/40 flex gap-3">
        <span>↑↓ navigate</span>
        <span>↵ run</span>
        <span>Tab complete</span>
        <span>Esc dismiss</span>
      </div>
    </div>
  )
}
