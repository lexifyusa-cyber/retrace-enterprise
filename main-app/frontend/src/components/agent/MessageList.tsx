import { useState, useMemo } from 'react'
import {
  Bot, GraduationCap, ClipboardList, FileText,
  CheckCircle2, Loader2, Play, Edit2, Copy, Check, Undo2,
  File as FileIcon, GitFork, Wrench,
} from 'lucide-react'
import MessageContextMenu, { type ContextMenuItem } from './MessageContextMenu'
import { useAuth } from '@/context/AuthContext'

/**
 * Six avatar palettes matching OC's `--avatar-background-{cyan,lime,mint,
 * orange,pink,purple}` design tokens. Picked deterministically from a hash
 * of the user's email so a user keeps the same color across reloads + every
 * conversation. CSS values are picked to read on both light and dark themes.
 */
const _AVATAR_PALETTES = [
  { bg: '#bff5e6', fg: '#0d5a4a' }, // mint
  { bg: '#c7eafe', fg: '#0c4b6e' }, // cyan
  { bg: '#ddf3a8', fg: '#3a5b00' }, // lime
  { bg: '#fed7aa', fg: '#7c2d12' }, // orange
  { bg: '#fbcfe8', fg: '#831843' }, // pink
  { bg: '#e9d5ff', fg: '#5b21b6' }, // purple
] as const

function _hashStr(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return Math.abs(h)
}

function _avatarFor(emailOrName: string): { bg: string; fg: string; initials: string } {
  const base = emailOrName || ''
  const palette = _AVATAR_PALETTES[_hashStr(base) % _AVATAR_PALETTES.length]
  // Initials: first letter of each whitespace-separated chunk (up to 2),
  // falling back to the first letter of the email's local part.
  const parts = base.split(/[\s._-]+/).filter(Boolean)
  let initials = parts.slice(0, 2).map(p => p[0]?.toUpperCase() || '').join('')
  if (!initials) {
    const at = base.indexOf('@')
    initials = (at > 0 ? base[0] : base[0] || '?').toUpperCase()
  }
  return { ...palette, initials }
}
import ReactMarkdown from 'react-markdown'
import type { OCMessage, SOPPart, DocPart } from '@/stores/opencodeStore'
import ThinkingBlock from './ThinkingBlock'
import ToolCallBlock from './ToolCallBlock'
import SyntheticEventChip from './SyntheticEventChip'
import SchedulePicker from './SchedulePicker'

export interface MessageListActions {
  onLearnThis?: (question: string, answer: string) => Promise<void>
  onCreateSOP?: (goal: string) => void
  onCreateDoc?: (goal: string) => void
  onApproveSOP?: (sopId: string, scheduleType?: string, scheduleConfig?: Record<string, any>) => Promise<void>
  onEditSOP?: (sopId: string, editInstructions: string) => Promise<{ sop_markdown: string; title: string }>
  onApproveDoc?: (docId: string) => Promise<void>
  onEditDoc?: (docId: string, editInstructions: string) => Promise<{ doc_markdown: string; title: string }>
  onUpdatePart?: (partId: string, updates: Partial<SOPPart> | Partial<DocPart>) => void
  /** Revert the conversation to before this message (removes it + everything
   * after, reversibly via Redo). Wired to the inline hover-revealed Revert
   * icon on user messages. */
  onRevertTo?: (messageId: string) => Promise<void> | void
  /** Fork the conversation at this message into a new conversation. Wired to
   * the right-click "Fork to new session" menu item on either role. */
  onForkAt?: (messageId: string) => Promise<void> | void
  /** Drop a starter prompt into the input box. Fired by clicking a sample
   * chip on the empty state. */
  onSelectPrompt?: (text: string) => void
  allowLearnThis?: boolean
  isCreatingSOP?: boolean
  isCreatingDoc?: boolean
}

/** Best-effort clipboard copy with a graceful fallback for non-HTTPS contexts. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* fall through */ }
  // Fallback for http:// or older browsers — hidden textarea + execCommand.
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.left = '-9999px'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch { return false }
}

interface MessageListProps {
  messages: OCMessage[]
  actions?: MessageListActions
}

/**
 * A "turn" is either a single user message or a run of consecutive assistant
 * messages between user prompts. OpenCode creates multiple `AssistantMessage`
 * rows per turn (one per LLM round-trip in a tool-use loop), and rendering
 * each with its own avatar/toolbar made a single conversation feel like five.
 * We collapse the run into a single visual turn here.
 */
type Turn =
  | { kind: 'user'; msg: OCMessage; index: number }
  | { kind: 'assistant'; msgs: OCMessage[]; firstIndex: number; lastIndex: number }

function computeTurns(messages: OCMessage[]): Turn[] {
  const turns: Turn[] = []
  let i = 0
  while (i < messages.length) {
    const m = messages[i]
    if (m.role === 'user') {
      turns.push({ kind: 'user', msg: m, index: i })
      i++
    } else {
      const start = i
      const msgs: OCMessage[] = []
      while (i < messages.length && messages[i].role === 'assistant') {
        msgs.push(messages[i])
        i++
      }
      turns.push({ kind: 'assistant', msgs, firstIndex: start, lastIndex: i - 1 })
    }
  }
  return turns
}

// ReTrace-flavored starter prompts shown on the empty conversation state.
// Steered toward the platform's value-add features (KB, automations, docs)
// so first-time users discover what the agent can do beyond generic dev tasks.
const SAMPLE_PROMPTS: string[] = [
  "What's in this team's knowledge base?",
  'Find documentation about our deployment process',
  'Create an automation that summarizes new emails',
  'Draft documentation from this conversation',
]

export default function MessageList({ messages, actions }: MessageListProps) {
  if (messages.length === 0) {
    return (
      <div className="text-center py-12 px-4">
        <div className="w-16 h-16 mx-auto mb-5 rounded-2xl bg-gradient-to-br from-purple-500/20 to-blue-500/20 flex items-center justify-center">
          <Bot className="w-8 h-8 text-purple-400" />
        </div>
        <h3 className="text-xl font-display font-semibold mb-2">What would you like the agent to do?</h3>
        <p className="text-rt-text-muted/70 max-w-md mx-auto text-sm">
          Tip: type <span className="font-mono text-rt-text-muted">/</span> for commands like <span className="font-mono">/revert</span>, <span className="font-mono">/retry</span>, <span className="font-mono">/share</span>.
        </p>
        {actions?.onSelectPrompt && (
          <div className="mt-6 flex flex-wrap gap-2 justify-center max-w-2xl mx-auto">
            {SAMPLE_PROMPTS.map(p => (
              <button
                key={p}
                type="button"
                onClick={() => actions.onSelectPrompt?.(p)}
                className="px-3 py-1.5 text-xs rounded-full border border-rt-border/60 text-rt-text-muted hover:text-rt-text hover:bg-rt-surface/60 transition-colors"
              >
                {p}
              </button>
            ))}
          </div>
        )}
      </div>
    )
  }

  const turns = computeTurns(messages)
  // The latest assistant turn that's part of the live conversation (not legacy
  // DB-loaded). Retry only renders on that one.
  let latestLiveTurnIdx = -1
  for (let i = turns.length - 1; i >= 0; i--) {
    const t = turns[i]
    if (t.kind === 'assistant' && !t.msgs.every(m => m.isLegacy)) {
      latestLiveTurnIdx = i
      break
    }
  }

  return (
    <div className="space-y-6 max-w-3xl mx-auto px-4 py-6">
      {turns.map((turn, ti) => {
        if (turn.kind === 'user') {
          return <UserMessage key={turn.msg.id} msg={turn.msg} actions={actions} />
        }
        return (
          <AssistantTurn
            key={turn.msgs[0]?.id || `turn-${ti}`}
            turn={turn}
            messages={messages}
            actions={actions}
            isLatestLiveTurn={ti === latestLiveTurnIdx}
          />
        )
      })}
    </div>
  )
}

/**
 * Renders a user message bubble plus hover-revealed inline action buttons,
 * matching OpenCode's `UserMessageDisplay` layout from
 * `packages/ui/src/components/message-part.tsx`. The buttons sit beneath the
 * bubble (Revert + Copy), invisible by default and fade in when the group is
 * hovered or focused — exactly OC's `[data-slot="user-message-copy-wrapper"]`
 * opacity transition pattern.
 *
 * Revert only renders when an `onRevertTo` action is wired; legacy DB-loaded
 * messages and optimistic local ids can't be reverted upstream so the action
 * is disabled for them.
 */
function UserMessage({ msg, actions }: { msg: OCMessage; actions?: MessageListActions }) {
  const text = msg.parts.find(p => p.type === 'text')
  const fileParts = msg.parts.filter(p => p.type === 'file') as Array<{
    type: 'file'; id: string; mime: string; filename: string; url: string
  }>
  const [copied, setCopied] = useState(false)
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null)
  const { user } = useAuth()
  const avatar = useMemo(
    () => _avatarFor(user?.display_name || user?.email || 'you'),
    [user?.display_name, user?.email],
  )
  if (!text || text.type !== 'text') return null
  const content = text.text

  // A "synthetic" user message was injected by retrace's fire handlers
  // (scheduled followup / monitor change / remote-trigger webhook) or by
  // OC's own task.inject for background-task completion. Labelled visually
  // so the user can tell at a glance "this came from the system, not me."
  const isSynthetic = text.type === 'text' && (text as any).synthetic === true
  const syntheticSource: 'monitor' | 'remote-trigger' | 'scheduled' | 'system' =
    content.startsWith('[monitor:') || content.startsWith('[stream:') || content.startsWith('[browser-monitor:') ? 'monitor'
    : content.startsWith('[remote-trigger:') ? 'remote-trigger'
    : content.startsWith('Background task completed:') || content.startsWith('Background task failed:') ? 'system'
    : 'scheduled'

  // System-injected (synthetic) turns — monitor/stream fires, background-task
  // completions, scheduled + webhook injections — render as a compact amber
  // tool-call-style chip (one glanceable line; click to expand the raw
  // payload) instead of a full user bubble. The chip self-classifies from
  // `content`, so it also handles `[stream:` fires that the source-prefix
  // check above labels as 'scheduled'.
  if (isSynthetic) {
    return <SyntheticEventChip content={content} />
  }

  const canMutate = !msg.isLegacy && !msg.id.startsWith('msg-') && !msg.id.startsWith('db-')
  const canRevert = canMutate && !!actions?.onRevertTo
  const canFork = canMutate && !!actions?.onForkAt

  const handleCopy = async () => {
    const ok = await copyText(content)
    if (ok) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }

  const handleRevert = () => {
    if (!actions?.onRevertTo) return
    actions.onRevertTo(msg.id)
  }

  const handleFork = () => {
    if (!actions?.onForkAt) return
    actions.onForkAt(msg.id)
  }

  const menuItems: ContextMenuItem[] = [
    { label: 'Copy message', Icon: Copy, onClick: handleCopy },
    { label: 'Revert message', Icon: Undo2, onClick: handleRevert, disabled: !canRevert },
    { label: 'Fork to new session', Icon: GitFork, onClick: handleFork, disabled: !canFork, separator: true },
  ]

  return (
    <div
      className="group flex flex-col items-end"
      onContextMenu={e => {
        e.preventDefault()
        setMenu({ x: e.clientX, y: e.clientY })
      }}
    >
      {fileParts.length > 0 && (
        <div className="max-w-[80%] mb-1.5 flex flex-wrap gap-1.5 justify-end">
          {fileParts.map(fp => fp.mime.toLowerCase().startsWith('image/') ? (
            <img
              key={fp.id}
              src={fp.url}
              alt={fp.filename}
              className="w-12 h-12 rounded object-cover border border-rt-border/30"
              title={fp.filename}
            />
          ) : (
            <div
              key={fp.id}
              className="flex items-center gap-2 max-w-[220px] h-12 px-2.5 rounded border border-rt-border/30 bg-rt-surface/40"
              title={`${fp.filename} (${fp.mime})`}
            >
              <FileIcon className="w-4 h-4 text-rt-text-muted/70 flex-shrink-0" />
              <span className="text-xs text-rt-text truncate">{fp.filename}</span>
            </div>
          ))}
        </div>
      )}
      <div className="flex gap-2 items-end max-w-[85%]">
        <div
          className={
            isSynthetic
              ? "flex-1 min-w-0 rounded-2xl rounded-br-md bg-amber-500/10 border border-amber-500/30 px-4 py-3 text-[15px] leading-relaxed whitespace-pre-wrap"
              : "flex-1 min-w-0 rounded-2xl rounded-br-md bg-rt-primary/8 px-4 py-3 text-[15px] leading-relaxed whitespace-pre-wrap"
          }
        >
          {isSynthetic && (
            <div className="flex items-center gap-1.5 mb-1.5 text-[10px] uppercase tracking-wide font-semibold text-amber-400/80">
              <svg className="w-3 h-3" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                <path d="M8 1.5a1 1 0 0 1 1 1V3a4 4 0 0 1 3 3.874v3.376l1.6 1.6a.5.5 0 0 1-.354.854H2.754a.5.5 0 0 1-.354-.854L4 10.25V6.874A4 4 0 0 1 7 3V2.5a1 1 0 0 1 1-1Zm0 12.5a2 2 0 0 1-1.732-1H9.732A2 2 0 0 1 8 14Z" />
              </svg>
              <span>{
                syntheticSource === 'monitor' ? 'monitor'
                : syntheticSource === 'remote-trigger' ? 'webhook'
                : syntheticSource === 'system' ? 'background task'
                : 'scheduled'
              }</span>
            </div>
          )}
          {content}
        </div>
        <div
          className="w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-semibold flex-shrink-0 mb-1"
          style={isSynthetic
            ? { backgroundColor: 'rgb(245 158 11 / 0.2)', color: 'rgb(245 158 11)' }
            : { backgroundColor: avatar.bg, color: avatar.fg }
          }
          title={isSynthetic ? `System-injected (${syntheticSource})` : (user?.display_name || user?.email || 'You')}
        >
          {isSynthetic ? '⚙' : avatar.initials}
        </div>
      </div>
      {/* Hover-revealed inline buttons. Match OC exactly:
         - Revert (icon: reset/undo arrow), only when actions.onRevertTo is set
         - Copy (icon: copy → check briefly after success), always shown */}
      <div className="flex items-center gap-0.5 mt-1 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
        {canRevert && (
          <button
            type="button"
            onClick={handleRevert}
            title="Revert message"
            aria-label="Revert message"
            className="p-1.5 rounded text-rt-text-muted/60 hover:text-amber-400 hover:bg-amber-500/10 transition-colors"
          >
            <Undo2 className="w-3.5 h-3.5" />
          </button>
        )}
        <button
          type="button"
          onClick={handleCopy}
          title={copied ? 'Copied' : 'Copy message'}
          aria-label={copied ? 'Copied' : 'Copy message'}
          className="p-1.5 rounded text-rt-text-muted/60 hover:text-rt-text hover:bg-rt-surface transition-colors"
        >
          {copied ? <Check className="w-3.5 h-3.5 text-rt-success" /> : <Copy className="w-3.5 h-3.5" />}
        </button>
      </div>
      {menu && (
        <MessageContextMenu
          x={menu.x}
          y={menu.y}
          items={menuItems}
          onClose={() => setMenu(null)}
        />
      )}
    </div>
  )
}

function getQuestionForMessage(messages: OCMessage[], index: number): string | null {
  for (let i = index - 1; i >= 0; i--) {
    if (messages[i].role === 'user') {
      const text = messages[i].parts.find(p => p.type === 'text')
      if (text && text.type === 'text' && text.text) return text.text
    }
  }
  return null
}

function getAnswerText(msg: OCMessage): string {
  return msg.parts
    .filter(p => p.type === 'text' && p.text)
    .map(p => p.type === 'text' ? p.text : '')
    .join('\n')
}

function ActionBar({ msg, messages, index, actions }: {
  msg: OCMessage
  messages: OCMessage[]
  index: number
  actions?: MessageListActions
}) {
  const [activePrompt, setActivePrompt] = useState<'learn' | 'automation' | 'doc' | null>(null)
  const [promptInput, setPromptInput] = useState('')
  const [isLearning, setIsLearning] = useState(false)
  const [learned, setLearned] = useState(false)

  if (!actions || msg.isStreaming) return null

  const question = getQuestionForMessage(messages, index)
  const answer = getAnswerText(msg)
  const hasText = msg.parts.some(p => p.type === 'text' && p.text)
  if (!hasText || !question) return null

  const handleLearn = async () => {
    if (!actions.onLearnThis) return
    setIsLearning(true)
    try {
      await actions.onLearnThis(promptInput.trim() || question, answer)
      setLearned(true)
    } finally {
      setIsLearning(false)
    }
    setActivePrompt(null)
    setPromptInput('')
  }

  return (
    <div className="mt-2">
      <div className="flex flex-wrap items-center gap-1.5 opacity-0 group-hover:opacity-60 hover:!opacity-100 transition-opacity">
        {actions.allowLearnThis !== false && (
          learned ? (
            <span className="inline-flex items-center gap-1.5 text-xs text-rt-success">
              <CheckCircle2 className="w-3.5 h-3.5" />Added to knowledge base
            </span>
          ) : (
            <button
              onClick={() => { setActivePrompt(activePrompt === 'learn' ? null : 'learn'); setPromptInput('') }}
              disabled={isLearning}
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium transition-colors disabled:opacity-30 ${
                activePrompt === 'learn'
                  ? 'bg-amber-500/25 text-amber-300 ring-1 ring-amber-500/40'
                  : 'text-rt-text-muted/50 hover:text-amber-400 hover:bg-amber-500/10'
              }`}
              title="Train this Q&A as expert knowledge"
            >
              {isLearning ? <Loader2 className="w-3 h-3 animate-spin" /> : <GraduationCap className="w-3 h-3" />}
              Learn
            </button>
          )
        )}
        <button
          onClick={() => { setActivePrompt(activePrompt === 'automation' ? null : 'automation'); setPromptInput('') }}
          disabled={actions.isCreatingSOP || !actions.onCreateSOP}
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium transition-colors disabled:opacity-30 ${
            activePrompt === 'automation'
              ? 'bg-emerald-500/25 text-emerald-300 ring-1 ring-emerald-500/40'
              : 'text-rt-text-muted/50 hover:text-emerald-400 hover:bg-emerald-500/10'
          }`}
          title="Build an automation workflow from this Q&A"
        >
          {actions.isCreatingSOP ? <Loader2 className="w-3 h-3 animate-spin" /> : <ClipboardList className="w-3 h-3" />}
          Build Automation
        </button>
        <button
          onClick={() => { setActivePrompt(activePrompt === 'doc' ? null : 'doc'); setPromptInput('') }}
          disabled={actions.isCreatingDoc || !actions.onCreateDoc}
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium transition-colors disabled:opacity-30 ${
            activePrompt === 'doc'
              ? 'bg-blue-500/25 text-blue-300 ring-1 ring-blue-500/40'
              : 'text-rt-text-muted/50 hover:text-blue-400 hover:bg-blue-500/10'
          }`}
          title="Build a knowledge article from this Q&A"
        >
          {actions.isCreatingDoc ? <Loader2 className="w-3 h-3 animate-spin" /> : <FileText className="w-3 h-3" />}
          Build Documentation
        </button>
      </div>
      {activePrompt && (
        <div className="mt-2 pt-2 border-t border-rt-border/30">
          <p className="text-xs text-rt-text-muted mb-1.5">
            {activePrompt === 'learn' && 'Optionally refine the question before saving to knowledge base:'}
            {activePrompt === 'automation' && 'Optionally describe what the automation should focus on:'}
            {activePrompt === 'doc' && 'Optionally describe what the documentation should cover:'}
          </p>
          <div className="flex gap-2 items-end">
            <textarea
              value={promptInput}
              onChange={e => setPromptInput(e.target.value)}
              placeholder="Leave empty to use defaults..."
              rows={2}
              className="input flex-1 text-sm min-h-[2.5rem] resize-y"
              disabled={isLearning || actions.isCreatingSOP || actions.isCreatingDoc}
              autoFocus
            />
            <button
              type="button"
              onClick={() => {
                if (activePrompt === 'learn') handleLearn()
                else if (activePrompt === 'automation') actions.onCreateSOP?.(promptInput.trim() || question)
                else if (activePrompt === 'doc') actions.onCreateDoc?.(promptInput.trim() || question)
                setActivePrompt(null)
                setPromptInput('')
              }}
              disabled={isLearning || actions.isCreatingSOP || actions.isCreatingDoc}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium flex items-center gap-1.5 transition-colors disabled:opacity-50 ${
                activePrompt === 'learn'
                  ? 'bg-amber-500/15 text-amber-400 hover:bg-amber-500/25'
                  : activePrompt === 'automation'
                    ? 'bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25'
                    : 'bg-blue-500/15 text-blue-400 hover:bg-blue-500/25'
              }`}
            >
              {(isLearning || actions.isCreatingSOP || actions.isCreatingDoc) ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
              Go
            </button>
            <button
              type="button"
              onClick={() => { setActivePrompt(null); setPromptInput('') }}
              className="px-3 py-1.5 rounded-lg text-xs font-medium text-rt-text-muted hover:bg-rt-surface transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * Renders a turn of assistant messages as a single visual block: one avatar,
 * parts from all member messages flattened in order, one cost summary, one
 * Retry (if this is the latest turn), one ActionBar.
 *
 * Context-menu actions on this turn target the FIRST message in the chain —
 * "Revert message" rolls back to before the entire turn started, which
 * matches how a user thinks about it ("undo this whole response").
 */
function AssistantTurn({ turn, messages, actions, isLatestLiveTurn }: {
  turn: Extract<Turn, { kind: 'assistant' }>
  messages: OCMessage[]
  actions?: MessageListActions
  isLatestLiveTurn: boolean
}) {
  const { msgs } = turn
  const firstMsg = msgs[0]
  const lastMsg = msgs[msgs.length - 1]
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null)
  if (!firstMsg || !lastMsg) return null

  const allEmpty = msgs.every(m => m.parts.length === 0)
  const anyStreaming = msgs.some(m => m.isStreaming)

  // Pure thinking turn (no parts yet but actively streaming) — show the
  // typing-dots pulse. This is the only time we render an empty turn.
  if (allEmpty) {
    if (!anyStreaming) return null
    return (
      <div className="flex gap-3">
        <div className="w-7 h-7 rounded-full bg-purple-500/20 flex items-center justify-center flex-shrink-0 mt-1">
          <Bot className="w-3.5 h-3.5 text-purple-400" />
        </div>
        <div className="flex-1 text-sm text-rt-text-muted animate-pulse">Thinking...</div>
      </div>
    )
  }

  // Aggregate cost across the whole turn.
  const turnCost = msgs.reduce((acc, m) => acc + (m.cost || 0), 0)
  const turnTokens = msgs.reduce(
    (acc, m) => acc + ((m.tokens?.input || 0) + (m.tokens?.output || 0)),
    0,
  )

  // Count tool calls across the whole turn. We only render the count chip
  // when 3+ tools fired — for shorter turns the chip would be visual noise.
  const turnToolCount = msgs.reduce(
    (acc, m) => acc + m.parts.filter(p => p.type === 'tool').length,
    0,
  )

  // Concatenate all rendered text parts from the turn for "Copy response".
  const turnText = msgs
    .flatMap(m => m.parts.filter(p => p.type === 'text').map(p => (p.type === 'text' ? p.text : '')))
    .filter(Boolean)
    .join('\n\n')

  const canForkTurn = !firstMsg.isLegacy && !firstMsg.id.startsWith('msg-') && !firstMsg.id.startsWith('db-') && !!actions?.onForkAt

  const turnMenuItems: ContextMenuItem[] = [
    { label: 'Copy response', Icon: Copy, onClick: async () => { await copyText(turnText) } },
    { label: 'Fork to new session', Icon: GitFork, onClick: () => actions?.onForkAt?.(firstMsg.id), disabled: !canForkTurn, separator: true },
  ]

  return (
    <div
      className="flex gap-3"
      onContextMenu={e => {
        // Only intercept right-clicks on the bubble proper, not on nested
        // tool-call blocks / SOP cards which may have their own menus.
        if ((e.target as HTMLElement).closest('button, [role="menu"]')) return
        e.preventDefault()
        setMenu({ x: e.clientX, y: e.clientY })
      }}
    >
      <div className="w-7 h-7 rounded-full bg-purple-500/20 flex items-center justify-center flex-shrink-0 mt-1">
        <Bot className="w-3.5 h-3.5 text-purple-400" />
      </div>
      <div className="flex-1 min-w-0 space-y-2">
        {/* Parts from every message in the turn, in order. */}
        {msgs.flatMap(m =>
          m.parts.map(p => {
            if (p.type === 'thinking' && p.text) {
              return <ThinkingBlock key={p.id} text={p.text} />
            }
            if (p.type === 'tool') {
              return <ToolCallBlock key={p.id} part={p} />
            }
            if (p.type === 'text' && p.text) {
              return <TextPart key={p.id} text={p.text} />
            }
            if (p.type === 'sop') {
              return <SOPCard key={p.id} part={p} actions={actions} />
            }
            if (p.type === 'doc') {
              return <DocCard key={p.id} part={p} actions={actions} />
            }
            return null
          })
        )}
        {(turnCost > 0 || turnToolCount >= 3) && (
          <div className="flex items-center gap-2 text-[10px] text-rt-text-muted/40 flex-wrap">
            {turnToolCount >= 3 && (
              <span className="flex items-center gap-1">
                <Wrench className="w-2.5 h-2.5" />
                {turnToolCount} tools used
              </span>
            )}
            {turnCost > 0 && turnToolCount >= 3 && <span className="text-rt-text-muted/30">·</span>}
            {turnCost > 0 && (
              <span>
                Cost: ${turnCost.toFixed(4)}
                {turnTokens > 0 && ` · ${turnTokens} tokens`}
              </span>
            )}
          </div>
        )}
        {/* Suppress unused-var warnings until isLatestLiveTurn is needed
            again. Kept as a prop so the callsite (MessageList default export)
            can flag it for future inline affordances if needed. */}
        {false && isLatestLiveTurn && null}
        {/* ActionBar (Learn/SOP/Doc) is anchored to the LAST message of the
            turn so it sits below the final assistant text. */}
        <ActionBar msg={lastMsg} messages={messages} index={turn.lastIndex} actions={actions} />
      </div>
      {menu && (
        <MessageContextMenu
          x={menu.x}
          y={menu.y}
          items={turnMenuItems}
          onClose={() => setMenu(null)}
        />
      )}
    </div>
  )
}

/**
 * A single assistant text part rendered as markdown, with a small Copy icon
 * that appears in the top-right corner on hover. The icon copies *just this
 * text part* — useful for grabbing a single code block or paragraph without
 * pulling the whole assistant turn.
 */
/**
 * Markdown `<pre>` renderer that adds a hover-revealed copy button to every
 * code block, mirroring OC's `markdown-copy-button` data-slot pattern. The
 * copy button targets just the code-block content (children's text), not
 * the surrounding paragraph.
 */
function CodeBlockPre({ children, ...rest }: any) {
  const [copied, setCopied] = useState(false)
  const codeText = useMemo(() => {
    // ReactMarkdown wraps the block in `<pre><code>{text}</code></pre>` so
    // we drill down to the leaf-text. Defensive: if shape changes, fall
    // back to coercing children to a string.
    try {
      const codeEl: any = Array.isArray(children) ? children[0] : children
      const inner = codeEl?.props?.children
      if (typeof inner === 'string') return inner
      if (Array.isArray(inner)) return inner.filter(c => typeof c === 'string').join('')
    } catch { /* ignore */ }
    return ''
  }, [children])

  return (
    <pre {...rest} className={`relative group/code ${rest.className || ''}`}>
      {children}
      <button
        type="button"
        onClick={async (e) => {
          e.stopPropagation()
          const ok = await copyText(codeText)
          if (ok) {
            setCopied(true)
            setTimeout(() => setCopied(false), 1500)
          }
        }}
        title={copied ? 'Copied' : 'Copy code'}
        aria-label={copied ? 'Copied' : 'Copy code'}
        className="absolute top-1.5 right-1.5 p-1.5 rounded bg-rt-bg/80 border border-rt-border/40 text-rt-text-muted/70 hover:text-rt-text hover:bg-rt-bg opacity-0 group-hover/code:opacity-100 transition-opacity backdrop-blur-sm z-10"
      >
        {copied ? <Check className="w-3 h-3 text-rt-success" /> : <Copy className="w-3 h-3" />}
      </button>
    </pre>
  )
}

const _MARKDOWN_COMPONENTS = { pre: CodeBlockPre } as const

function TextPart({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const onCopy = async (e: React.MouseEvent) => {
    e.stopPropagation()
    const ok = await copyText(text)
    if (ok) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }
  return (
    <div className="relative group/textpart">
      <div className="prose prose-agent max-w-none text-rt-text">
        <ReactMarkdown components={_MARKDOWN_COMPONENTS}>{text}</ReactMarkdown>
      </div>
      <button
        type="button"
        onClick={onCopy}
        title={copied ? 'Copied' : 'Copy this block'}
        className="absolute top-0 right-0 p-1 rounded text-rt-text-muted/40 hover:text-rt-text hover:bg-rt-surface opacity-0 group-hover/textpart:opacity-100 transition-opacity"
      >
        {copied ? <Check className="w-3 h-3 text-rt-success" /> : <Copy className="w-3 h-3" />}
      </button>
    </div>
  )
}

// MessageWithContextMenu / MessageContextMenu were removed in favor of
// inline hover-revealed buttons on the user message (matching OpenCode's
// `UserMessageDisplay` from packages/ui/src/components/message-part.tsx).
// Assistant messages have no extra buttons here — the per-text-part Copy
// icon inside `TextPart` covers the assistant copy affordance.

function SOPCard({ part, actions }: { part: SOPPart; actions?: MessageListActions }) {
  const [sopApproved, setSopApproved] = useState(part.approved || false)
  const [isApproving, setIsApproving] = useState(false)
  const [showSchedulePicker, setShowSchedulePicker] = useState(false)
  const [showEditInput, setShowEditInput] = useState(false)
  const [editInstructions, setEditInstructions] = useState('')
  const [isEditing, setIsEditing] = useState(false)

  const handleApprove = async (scheduleType: string, scheduleConfig?: Record<string, any>) => {
    if (!actions?.onApproveSOP || !part.sopId) return
    setIsApproving(true)
    try {
      await actions.onApproveSOP(part.sopId, scheduleType, scheduleConfig)
      setSopApproved(true)
      setShowSchedulePicker(false)
      actions.onUpdatePart?.(part.id, { approved: true } as Partial<SOPPart>)
    } catch { /* toast handled by parent */ }
    finally { setIsApproving(false) }
  }

  const handleEdit = async () => {
    if (!actions?.onEditSOP || !part.sopId || !editInstructions.trim()) return
    setIsEditing(true)
    try {
      const result = await actions.onEditSOP(part.sopId, editInstructions.trim())
      actions.onUpdatePart?.(part.id, { content: result.sop_markdown, title: result.title } as Partial<SOPPart>)
      setShowEditInput(false)
      setEditInstructions('')
    } catch { /* toast handled by parent */ }
    finally { setIsEditing(false) }
  }

  return (
    <div className="card border-emerald-500/30 bg-emerald-500/5">
      <div className="flex items-center gap-2 mb-3 text-xs text-emerald-400">
        <ClipboardList className="w-3.5 h-3.5" />
        <span className="font-medium">Automation</span>
        {part.title && <span className="text-rt-text-muted">— {part.title}</span>}
        {sopApproved && <span className="ml-auto inline-flex items-center gap-1 text-rt-success"><CheckCircle2 className="w-3.5 h-3.5" />Approved</span>}
      </div>
      <div className="prose prose-sm max-w-none">
        <ReactMarkdown>{part.content}</ReactMarkdown>
      </div>
      <div className="mt-3 pt-2 border-t border-emerald-500/20 flex flex-wrap items-center gap-2">
        {sopApproved ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-rt-success"><CheckCircle2 className="w-3.5 h-3.5" />Approved</span>
        ) : showSchedulePicker ? (
          <SchedulePicker
            onConfirm={handleApprove}
            onCancel={() => setShowSchedulePicker(false)}
            isApproving={isApproving}
          />
        ) : (
          <button
            onClick={() => setShowSchedulePicker(true)}
            disabled={isApproving || !actions?.onApproveSOP}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors disabled:opacity-50"
          >
            {isApproving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
            Approve Automation
          </button>
        )}
        {!showSchedulePicker && (
          <button
            onClick={() => setShowEditInput(!showEditInput)}
            disabled={isEditing || sopApproved}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 transition-colors disabled:opacity-50"
          >
            <Edit2 className="w-3.5 h-3.5" />
            Edit Automation
          </button>
        )}
      </div>
      {showEditInput && (
        <div className="mt-2 pt-2 border-t border-rt-border/30">
          <textarea
            value={editInstructions}
            onChange={e => setEditInstructions(e.target.value)}
            placeholder="Describe changes..."
            rows={2}
            className="input flex-1 text-sm min-h-[2.5rem] resize-y w-full"
            disabled={isEditing}
          />
          <div className="flex gap-2 mt-2">
            <button onClick={handleEdit} disabled={isEditing || !editInstructions.trim()}
              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-blue-500/15 text-blue-400 hover:bg-blue-500/25 transition-colors disabled:opacity-50 flex items-center gap-1.5">
              {isEditing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
              Apply Edit
            </button>
            <button onClick={() => { setShowEditInput(false); setEditInstructions('') }}
              className="px-3 py-1.5 rounded-lg text-xs font-medium text-rt-text-muted hover:bg-rt-surface transition-colors">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function DocCard({ part, actions }: { part: DocPart; actions?: MessageListActions }) {
  const [docApproved, setDocApproved] = useState(part.approved || false)
  const [isApproving, setIsApproving] = useState(false)
  const [showEditInput, setShowEditInput] = useState(false)
  const [editInstructions, setEditInstructions] = useState('')
  const [isEditing, setIsEditing] = useState(false)

  const handleApprove = async () => {
    if (!actions?.onApproveDoc || !part.docId) return
    setIsApproving(true)
    try {
      await actions.onApproveDoc(part.docId)
      setDocApproved(true)
      actions.onUpdatePart?.(part.id, { approved: true } as Partial<DocPart>)
    } catch { /* toast handled by parent */ }
    finally { setIsApproving(false) }
  }

  const handleEdit = async () => {
    if (!actions?.onEditDoc || !part.docId || !editInstructions.trim()) return
    setIsEditing(true)
    try {
      const result = await actions.onEditDoc(part.docId, editInstructions.trim())
      actions.onUpdatePart?.(part.id, { content: result.doc_markdown, title: result.title } as Partial<DocPart>)
      setShowEditInput(false)
      setEditInstructions('')
    } catch { /* toast handled by parent */ }
    finally { setIsEditing(false) }
  }

  return (
    <div className="card border-blue-500/30 bg-blue-500/5">
      <div className="flex items-center gap-2 mb-3 text-xs text-blue-400">
        <FileText className="w-3.5 h-3.5" />
        <span className="font-medium">Documentation</span>
        {part.title && <span className="text-rt-text-muted">— {part.title}</span>}
        {docApproved && <span className="ml-auto inline-flex items-center gap-1 text-rt-success"><CheckCircle2 className="w-3.5 h-3.5" />Approved</span>}
      </div>
      <div className="prose prose-sm max-w-none">
        <ReactMarkdown>{part.content}</ReactMarkdown>
      </div>
      <div className="mt-3 pt-2 border-t border-blue-500/20 flex flex-wrap items-center gap-2">
        {docApproved ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-rt-success"><CheckCircle2 className="w-3.5 h-3.5" />Approved</span>
        ) : (
          <button
            onClick={handleApprove}
            disabled={isApproving || !actions?.onApproveDoc}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 transition-colors disabled:opacity-50"
          >
            {isApproving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
            Approve Documentation
          </button>
        )}
        <button
          onClick={() => setShowEditInput(!showEditInput)}
          disabled={isEditing || docApproved}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 transition-colors disabled:opacity-50"
        >
          <Edit2 className="w-3.5 h-3.5" />
          Edit Documentation
        </button>
      </div>
      {showEditInput && (
        <div className="mt-2 pt-2 border-t border-rt-border/30">
          <textarea
            value={editInstructions}
            onChange={e => setEditInstructions(e.target.value)}
            placeholder="Describe changes..."
            rows={2}
            className="input flex-1 text-sm min-h-[2.5rem] resize-y w-full"
            disabled={isEditing}
          />
          <div className="flex gap-2 mt-2">
            <button onClick={handleEdit} disabled={isEditing || !editInstructions.trim()}
              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-blue-500/15 text-blue-400 hover:bg-blue-500/25 transition-colors disabled:opacity-50 flex items-center gap-1.5">
              {isEditing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
              Apply Edit
            </button>
            <button onClick={() => { setShowEditInput(false); setEditInstructions('') }}
              className="px-3 py-1.5 rounded-lg text-xs font-medium text-rt-text-muted hover:bg-rt-surface transition-colors">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
