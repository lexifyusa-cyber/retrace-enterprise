import { useState } from 'react'
import {
  ChevronRight, ChevronDown, Activity, Clock, Wrench, Webhook,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

/**
 * Renders a "synthetic" (system-injected) turn — a monitor/stream fire, a
 * background-task completion, or a scheduled / webhook injection — as a
 * compact, amber-tinted tool-call-style chip instead of a full user bubble.
 *
 * Deliberately mirrors `ToolCallBlock`'s markup so it reads as "a result the
 * system produced," not "a message you typed." One glanceable line by default
 * (kind + target → value); click to expand the raw injected payload.
 *
 * Self-classifies from `content` (the exact fire-text formats are built in
 * `tool_monitor.py` / `stream_monitor.py` / `followup_scheduler.py`), so it
 * doesn't depend on the caller labelling the source.
 */

type AccentKey = 'monitor' | 'ended' | 'scheduled' | 'remote-trigger' | 'system' | 'failed'

const ACCENT: Record<AccentKey, { ring: string; text: string; icon: LucideIcon }> = {
  // amber = routine automation (the default); red = a failure; others semantic
  monitor:          { ring: 'border-amber-500/30 bg-amber-500/5',   text: 'text-amber-300',    icon: Activity },
  ended:            { ring: 'border-amber-500/20 bg-amber-500/5',   text: 'text-amber-300/70', icon: Clock },
  scheduled:        { ring: 'border-sky-500/30 bg-sky-500/5',       text: 'text-sky-300',      icon: Clock },
  'remote-trigger': { ring: 'border-violet-500/30 bg-violet-500/5', text: 'text-violet-300',   icon: Webhook },
  system:           { ring: 'border-sky-500/30 bg-sky-500/5',       text: 'text-sky-300',      icon: Wrench },
  failed:           { ring: 'border-red-500/30 bg-red-500/5',       text: 'text-red-300',      icon: Wrench },
}

/** Parse the injected text into a key (→ accent), a label, and a one-line summary. */
function classify(content: string): { key: AccentKey; label: string; summary: string } {
  // monitor / stream window ended
  if (/^\[(?:monitor|stream):[^\]]+\]\s*The watch/.test(content)) {
    const stats = content.match(/Stats: \d+ checks[^,]*,\s*(\d+) change/)
    return { key: 'ended', label: 'Monitor', summary: stats ? `ended · ${stats[1]} changes` : 'window ended' }
  }
  // monitor fire: [monitor:ID] '...' fired. \n Command|URL|Tool: ... \n ... Current output: ```VAL```
  if (content.startsWith('[monitor:')) {
    const tgt = content.match(/(?:Command|URL|Tool): `?([^\n`]+)`?/)?.[1]?.trim() ?? ''
    const val = content.match(/Current output:\n```\n([\s\S]*?)\n```/)?.[1]?.trim().split('\n')[0] ?? ''
    return { key: 'monitor', label: 'Monitor', summary: [tgt, val].filter(Boolean).join(' → ') }
  }
  // stream fire: [stream:ID] '...' matched N message(s). \n Source: `CMD`
  if (content.startsWith('[stream:')) {
    const src = content.match(/Source: `?([^\n`]+)`?/)?.[1]?.trim() ?? ''
    const n = content.match(/matched (\d+) message/)?.[1]
    return { key: 'monitor', label: 'Stream', summary: [src, n && `${n} matched`].filter(Boolean).join(' · ') }
  }
  // webhook
  if (content.startsWith('[remote-trigger:')) {
    const first = content.replace(/^\[[^\]]+\]\s*/, '').split('\n').find(l => l.trim()) ?? ''
    return { key: 'remote-trigger', label: 'Webhook', summary: first.slice(0, 80) }
  }
  // background task
  if (content.startsWith('Background task')) {
    const failed = content.startsWith('Background task failed:')
    const desc = content.match(/Background task (?:completed|failed): (.+)/)?.[1] ?? ''
    const out = content.match(/<task_result>\n([\s\S]*?)\n<\/task_result>/)?.[1]
      ?.trim().split('\n').filter(Boolean).pop() ?? ''
    return {
      key: failed ? 'failed' : 'system',
      label: 'Background task',
      summary: [desc, out].filter(Boolean).join(' · ').slice(0, 80),
    }
  }
  // scheduled / anything else — first non-empty line
  const first = content.split('\n').find(l => l.trim()) ?? ''
  return { key: 'scheduled', label: 'Scheduled', summary: first.slice(0, 80) }
}

export default function SyntheticEventChip({ content }: { content: string }) {
  const [open, setOpen] = useState(false)
  const { key, label, summary } = classify(content)
  const a = ACCENT[key] ?? ACCENT.monitor
  const Icon = a.icon

  return (
    <div className={`my-1.5 rounded-md border overflow-hidden ${a.ring}`}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-rt-surface/40 transition-colors"
      >
        {open ? (
          <ChevronDown className="w-3 h-3 text-rt-text-muted/60 flex-shrink-0" />
        ) : (
          <ChevronRight className="w-3 h-3 text-rt-text-muted/60 flex-shrink-0" />
        )}
        <Icon className={`w-3.5 h-3.5 flex-shrink-0 ${a.text}`} />
        <span className={`text-xs font-medium ${a.text}`}>{label}</span>
        {summary && (
          <span className="text-[11px] text-rt-text-muted/70 font-mono truncate flex-1 min-w-0">· {summary}</span>
        )}
        <span className="ml-auto text-[10px] text-rt-text-muted/50 flex-shrink-0" title="System-injected">⚙</span>
      </button>
      {open && (
        <div className="border-t border-rt-border/20 px-3 py-2">
          <pre className="text-xs text-rt-text-muted bg-rt-bg/50 rounded p-2 max-h-[300px] overflow-auto whitespace-pre-wrap break-all font-mono">
            {content}
          </pre>
        </div>
      )}
    </div>
  )
}
