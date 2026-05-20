import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Undo2, Redo2, Share2, FileText, GitFork, Minimize2, Database,
  ListChecks, ClipboardList, GraduationCap, Bot, Command,
} from 'lucide-react'
import { agentApi } from '@/utils/api'

/** A single command available in the palette. */
export interface PaletteCommand {
  /** Slash form, including the leading `/`, e.g. `/revert`. */
  slash: string
  /** Display label (capitalized). */
  label: string
  /** Optional short description shown in the dropdown. */
  description?: string
  /** Lucide icon component. */
  Icon: typeof Bot
  /** Action when the user selects this command. `args` is everything after
   * the command word; may be empty. */
  run: (args: string) => Promise<void> | void
  /** OC-side commands (vs. ReTrace-side). Helps with grouping. */
  source?: 'oc' | 'retrace'
}

export interface CommandPaletteHandlers {
  onRevert?: (messageId?: string) => Promise<void> | void
  onRedo?: () => Promise<void> | void
  onShare?: () => Promise<void> | void
  onSummarize?: () => Promise<void> | void
  onFork?: () => Promise<void> | void
  onCompact?: () => Promise<void> | void
  onLearn?: () => Promise<void> | void
  onBuildAutomation?: () => Promise<void> | void
  onBuildDoc?: () => Promise<void> | void
  onTodos?: () => void
  onHelp?: () => void
}

/**
 * Inline dropdown that appears above the chat input when the user types `/`.
 * Renders a filterable list of slash commands (both ReTrace-side and OC-side)
 * with keyboard navigation. The parent component owns the textarea value and
 * intercepts submission — this component only owns selection UX.
 */
export default function CommandPalette({
  query,
  onSelect,
  onClose,
  handlers,
}: {
  /** The raw textarea value. Starts with `/` if the palette should show. */
  query: string
  /** Called when the user accepts a command. The parent should clear the
   * input and run the command. */
  onSelect: (command: PaletteCommand, args: string) => void
  /** Called when the user dismisses the palette (Esc, click outside). */
  onClose: () => void
  /** Handlers wired from the page. Each command runs the corresponding
   * handler; if a handler is absent that command is hidden. */
  handlers: CommandPaletteHandlers
}) {
  const [highlight, setHighlight] = useState(0)
  const [ocCommands, setOcCommands] = useState<Array<{ name: string; description?: string }>>([])
  const fetched = useRef(false)

  // Lazy-load OC's command list once when the palette first opens.
  useEffect(() => {
    if (fetched.current) return
    fetched.current = true
    agentApi.opencode.listCommands().then(list => {
      if (Array.isArray(list)) setOcCommands(list)
    }).catch(() => { /* OC down — palette still works with ReTrace commands */ })
  }, [])

  // Built-in ReTrace-side commands. Each maps to a single handler.
  const builtins = useMemo<PaletteCommand[]>(() => {
    const cmds: PaletteCommand[] = []
    if (handlers.onRevert) cmds.push({
      slash: '/revert', label: 'Revert', description: 'Roll back the last assistant turn (or `/revert <messageID>`)',
      Icon: Undo2, source: 'retrace', run: (args) => handlers.onRevert?.(args || undefined),
    })
    if (handlers.onRedo) cmds.push({
      slash: '/redo', label: 'Redo', description: 'Restore the last reverted turn',
      Icon: Redo2, source: 'retrace', run: () => handlers.onRedo?.(),
    })
    if (handlers.onShare) cmds.push({
      slash: '/share', label: 'Share', description: 'Generate a public URL for this conversation',
      Icon: Share2, source: 'retrace', run: () => handlers.onShare?.(),
    })
    if (handlers.onSummarize) cmds.push({
      slash: '/summarize', label: 'Summarize', description: 'Generate a digest of the whole session',
      Icon: FileText, source: 'retrace', run: () => handlers.onSummarize?.(),
    })
    if (handlers.onFork) cmds.push({
      slash: '/fork', label: 'Fork', description: 'Start a new conversation branched from here',
      Icon: GitFork, source: 'retrace', run: () => handlers.onFork?.(),
    })
    if (handlers.onCompact) cmds.push({
      slash: '/compact', label: 'Compact', description: 'Trim conversation context to keep it under the model limit',
      Icon: Minimize2, source: 'retrace', run: () => handlers.onCompact?.(),
    })
    if (handlers.onLearn) cmds.push({
      slash: '/learn', label: 'Learn', description: 'Train the team knowledge base on the latest Q&A',
      Icon: GraduationCap, source: 'retrace', run: () => handlers.onLearn?.(),
    })
    if (handlers.onBuildAutomation) cmds.push({
      slash: '/automation', label: 'Build Automation', description: 'Turn this conversation into a reusable SOP',
      Icon: ClipboardList, source: 'retrace', run: () => handlers.onBuildAutomation?.(),
    })
    if (handlers.onBuildDoc) cmds.push({
      slash: '/documentation', label: 'Build Documentation', description: 'Generate a knowledge article from this conversation',
      Icon: FileText, source: 'retrace', run: () => handlers.onBuildDoc?.(),
    })
    if (handlers.onTodos) cmds.push({
      slash: '/todos', label: 'Show Todos', description: "Switch the workspace pane to the agent's TODO list",
      Icon: ListChecks, source: 'retrace', run: () => handlers.onTodos?.(),
    })
    if (handlers.onHelp) cmds.push({
      slash: '/help', label: 'Help', description: 'List all available commands',
      Icon: Command, source: 'retrace', run: () => handlers.onHelp?.(),
    })
    return cmds
  }, [handlers])

  // Map OC's slash commands into the same shape. Each just inserts the OC
  // command name into the current prompt for the agent to handle, since OC
  // commands typically expand to longer prompts via its agent system.
  const ocBuiltins = useMemo<PaletteCommand[]>(() => {
    return ocCommands.map(c => ({
      slash: `/${c.name}`,
      label: c.name,
      description: c.description,
      Icon: Database,
      source: 'oc' as const,
      // For OC-side commands we just let the parent send the text as-is to
      // the agent. OC's session command endpoint takes care of the rest.
      run: () => { /* parent handles via raw send */ },
    }))
  }, [ocCommands])

  // Filter by what the user typed after the leading `/`. Match the slash
  // form or the label substring (case-insensitive).
  const filter = query.startsWith('/') ? query.slice(1).trim().toLowerCase() : ''
  const matches = useMemo(() => {
    const all = [...builtins, ...ocBuiltins]
    if (!filter) return all
    // Take only the word before any whitespace — anything after is the args.
    const word = filter.split(/\s+/)[0]
    return all.filter(c =>
      c.slash.slice(1).toLowerCase().startsWith(word) ||
      c.label.toLowerCase().includes(word)
    )
  }, [builtins, ocBuiltins, filter])

  // Clamp the highlight whenever the match set changes.
  useEffect(() => {
    setHighlight(h => (matches.length === 0 ? 0 : Math.min(h, matches.length - 1)))
  }, [matches.length])

  // Listen for ↑ / ↓ / Enter / Esc at the window level so the user can drive
  // the palette from their keyboard while the textarea retains focus.
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
        const args = query.slice(cmd.slash.length).trim()
        onSelect(cmd, args)
      } else if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      } else if (e.key === 'Tab' && matches[highlight]) {
        // Tab auto-completes to the full slash form.
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
        No commands match "{filter}"
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
          const Icon = cmd.Icon
          const isHi = i === highlight
          return (
            <button
              key={cmd.slash}
              type="button"
              onMouseEnter={() => setHighlight(i)}
              onClick={() => {
                const args = query.slice(cmd.slash.length).trim()
                onSelect(cmd, args)
              }}
              className={`w-full flex items-center gap-3 px-3 py-1.5 text-left transition-colors ${
                isHi ? 'bg-rt-primary/15' : 'hover:bg-rt-surface/60'
              }`}
            >
              <Icon className="w-3.5 h-3.5 flex-shrink-0 text-rt-text-muted" />
              <div className="flex-1 min-w-0">
                <div className="flex items-baseline gap-2">
                  <span className={`text-xs font-mono font-medium ${isHi ? 'text-rt-primary' : 'text-rt-text'}`}>
                    {cmd.slash}
                  </span>
                  {cmd.source === 'oc' && (
                    <span className="text-[9px] text-rt-text-muted/50 uppercase tracking-wider">oc</span>
                  )}
                </div>
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
