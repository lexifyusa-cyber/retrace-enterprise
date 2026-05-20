import { useEffect, useState, useMemo } from 'react'
import {
  ChevronDown, ChevronRight, Terminal, FileEdit, Search, Globe, Wrench,
  Database, Monitor, Camera, MousePointer, Keyboard,
  GraduationCap,
  FileText, FolderOpen, ExternalLink,
  HelpCircle, MessageCircle, Eye, Circle, X,
  FileDiff,
  Replace, ScanSearch, FolderSearch, Link2, ListTodo,
  Sparkles, Code, ArrowLeft, ArrowRight, RefreshCw, MousePointer2,
  Clock, BarChart3, Layers, ChevronsUpDown, MapPin,
} from 'lucide-react'
import type { ToolPart } from '@/stores/opencodeStore'
import { useActiveSession, useOpenCodeStore } from '@/stores/opencodeStore'

const _toolIconMap: Record<string, typeof Terminal> = {
  bash: Terminal,
  edit: Replace,
  write: FileEdit,
  read: FileText,
  multiEdit: Replace,
  glob: FolderSearch,
  grep: ScanSearch,
  webfetch: Link2,
  web_fetch: Link2,  // backend /tools list uses underscored alias
  websearch: Search,
  web_search: Search,
  web_research: Globe,
  web_advanced: Sparkles,
  task: Wrench,
  todowrite: ListTodo,
  todo_write: ListTodo,  // backend /tools list uses underscored alias
  skill: GraduationCap,
  apply_patch: FileDiff,
  question: HelpCircle,
  ask_user: MessageCircle,
  list_dir: FolderOpen,
  // MCP server entries (toolbar lists by server name; chat-message tool calls
  // come through with per-action names below)
  'retrace-kb': Database,
  'retrace-browser': Globe,
  'retrace-screenops': Monitor,
  'retrace-kb_search': Search,
  'retrace-kb_browse': FolderOpen,
  'retrace-kb_list_entries': Database,
  'retrace-kb_read_file': FileText,
  'retrace-kb_learn': GraduationCap,
  'retrace-browser_navigate': Globe,
  'retrace-browser_click': MousePointer,
  'retrace-browser_vision_click': Eye,
  'retrace-browser_type_text': Keyboard,
  'retrace-browser_read_page': FileText,
  'retrace-browser_take_screenshot': Camera,
  'retrace-browser_scroll': ChevronDown,
  'retrace-browser_press_key': Keyboard,
  'retrace-browser_evaluate_js': Code,
  'retrace-browser_select_option': ChevronsUpDown,
  'retrace-browser_hover': MousePointer2,
  'retrace-browser_back': ArrowLeft,
  'retrace-browser_forward': ArrowRight,
  'retrace-browser_refresh': RefreshCw,
  'retrace-browser_get_url': MapPin,
  'retrace-browser_wait': Clock,
  'retrace-browser_screenshot_full_page': Layers,
  'retrace-browser_analyze_page': BarChart3,
  'retrace-screenops_screenshot': Monitor,
  'retrace-screenops_click': MousePointer,
  'retrace-screenops_type_text': Keyboard,
  'retrace-screenops_mouse_move': MousePointer,
  'retrace-screenops_press_key': Keyboard,
  'retrace-search_web_search': Search,
  'retrace-search_web_research': Globe,
  'retrace-search_web_advanced': Sparkles,
}

const _toolLabelMap: Record<string, string> = {
  'retrace-kb_search': 'Search Knowledge Base',
  'retrace-kb_browse': 'Browse Knowledge Base',
  'retrace-kb_list_entries': 'List KB Entries',
  'retrace-kb_read_file': 'Read KB File',
  'retrace-kb_learn': 'Learn to KB',
  'retrace-browser_navigate': 'Navigate Browser',
  'retrace-browser_click': 'Click Element',
  'retrace-browser_vision_click': 'Vision Click',
  'retrace-browser_type_text': 'Type in Browser',
  'retrace-browser_read_page': 'Read Page',
  'retrace-browser_take_screenshot': 'Take Screenshot',
  'retrace-browser_scroll': 'Scroll Page',
  'retrace-browser_press_key': 'Press Key',
  'retrace-browser_evaluate_js': 'Run JavaScript',
  'retrace-browser_select_option': 'Select Option',
  'retrace-browser_hover': 'Hover',
  'retrace-browser_back': 'Go Back',
  'retrace-browser_forward': 'Go Forward',
  'retrace-browser_refresh': 'Refresh Page',
  'retrace-browser_get_url': 'Get URL',
  'retrace-browser_wait': 'Wait',
  'retrace-browser_screenshot_full_page': 'Full Page Screenshot',
  'retrace-browser_analyze_page': 'Analyze Page',
  'retrace-screenops_screenshot': 'Desktop Screenshot',
  'retrace-screenops_click': 'Desktop Click',
  'retrace-screenops_type_text': 'Desktop Type',
  'retrace-screenops_mouse_move': 'Move Mouse',
  'retrace-screenops_press_key': 'Desktop Press Key',
  bash: 'Terminal',
  edit: 'Str Replace',
  write: 'Write File',
  read: 'Read File',
  multiEdit: 'Multi-Edit',
  glob: 'Glob',
  grep: 'Grep',
  webfetch: 'Web Fetch',
  web_fetch: 'Web Fetch',
  websearch: 'Web Search',
  web_search: 'Web Search',
  web_research: 'Web Research',
  web_advanced: 'Web Advanced',
  task: 'Sub-Agent',
  todowrite: 'Todos',
  todo_write: 'Todos',
  skill: 'Load Skill',
  apply_patch: 'Apply Patch',
  question: 'Ask User',
  ask_user: 'Ask User',
  list_dir: 'List Directory',
  'retrace-kb': 'Knowledge Base',
  'retrace-browser': 'Browser',
  'retrace-screenops': 'ScreenOps',
  'retrace-search_web_search': 'Web Search',
  'retrace-search_web_research': 'Web Research',
  'retrace-search_web_advanced': 'Web Advanced',
}

export function getToolLabel(toolName: string): string {
  return _toolLabelMap[toolName] || toolName.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

export function getToolIcon(toolName: string): typeof Terminal {
  return _toolIconMap[toolName] || Wrench
}

function _tryParseJson(str: string): unknown | null {
  try { return JSON.parse(str) }
  catch { return null }
}

function _isBase64Image(str: string): boolean {
  return /^[A-Za-z0-9+/=]{100,}$/.test(str.trim()) && str.length > 500
}

function _detectImageFormat(str: string): string {
  if (str.startsWith('/9j/')) return 'image/jpeg'
  if (str.startsWith('iVBOR')) return 'image/png'
  if (str.startsWith('R0lGOD')) return 'image/gif'
  if (str.startsWith('UklGR')) return 'image/webp'
  return 'image/png'
}

function ScreenshotOutput({ dataUrl, alt }: { dataUrl: string; alt: string }) {
  const [loaded, setLoaded] = useState(false)
  return (
    <div className="rounded overflow-hidden border border-rt-border/20">
      <img
        src={dataUrl}
        alt={alt}
        className={`max-w-full max-h-[400px] object-contain bg-black/20 transition-opacity ${loaded ? 'opacity-100' : 'opacity-0'}`}
        onLoad={() => setLoaded(true)}
      />
      {!loaded && (
        <div className="flex items-center justify-center h-[200px] text-rt-text-muted text-xs">
          Loading screenshot...
        </div>
      )}
    </div>
  )
}

function BashOutput({ output }: { output: string }) {
  const lines = output.split('\n')
  return (
    <div className="bg-black/30 rounded p-2 max-h-[300px] overflow-auto font-mono text-xs">
      {lines.map((line, i) => (
        <div key={i} className={`${line.startsWith('+') ? 'text-green-400' : line.startsWith('-') && !line.startsWith('---') ? 'text-red-400' : line.startsWith('error') || line.startsWith('Error') || line.startsWith('FAIL') ? 'text-red-400' : 'text-rt-text-muted'}`}>
          {line || '\u00A0'}
        </div>
      ))}
    </div>
  )
}

function FileEditOutput({ input, output }: { input?: Record<string, unknown>; output: string }) {
  const filePath = (input?.file_path || input?.path || '') as string
  return (
    <div className="space-y-1">
      {filePath && (
        <div className="flex items-center gap-1.5 text-xs">
          <FileEdit className="w-3 h-3 text-blue-400" />
          <span className="font-mono text-blue-300">{filePath}</span>
        </div>
      )}
      <pre className="text-xs text-rt-text-muted bg-rt-bg/50 rounded p-2 max-h-[200px] overflow-auto whitespace-pre-wrap break-all font-mono">
        {output.length > 3000 ? output.slice(0, 3000) + '\n... (truncated)' : output}
      </pre>
    </div>
  )
}

function KBSearchOutput({ output }: { output: string }) {
  const parsed = _tryParseJson(output)
  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
    const results = (parsed as Record<string, unknown>).results
    if (Array.isArray(results)) {
      return (
        <div className="space-y-1.5">
          <div className="text-[10px] font-semibold text-rt-text-muted/60 uppercase tracking-wide">
            {results.length} result{results.length !== 1 ? 's' : ''}
          </div>
          {results.map((r: any, i: number) => (
            <div key={i} className="rounded bg-rt-bg/50 border border-rt-border/20 p-2">
              <div className="text-xs font-medium text-rt-text">{r.title || r.file_path || r.name || `Result ${i + 1}`}</div>
              {r.content && <div className="text-[11px] text-rt-text-muted mt-1 line-clamp-3">{r.content}</div>}
              {r.score != null && <div className="text-[10px] text-rt-text-muted/40 mt-1">Relevance: {(r.score * 100).toFixed(1)}%</div>}
            </div>
          ))}
        </div>
      )
    }
  }
  const lines = output.split('\n').filter(l => l.trim())
  return (
    <div className="space-y-1">
      {lines.map((line, i) => (
        <div key={i} className="flex items-start gap-1.5 text-xs">
          <span className="text-rt-primary mt-0.5">&#8226;</span>
          <span className="text-rt-text-muted">{line}</span>
        </div>
      ))}
    </div>
  )
}

function KBBrowseOutput({ output }: { output: string }) {
  const parsed = _tryParseJson(output)
  if (parsed && typeof parsed === 'object') {
    const entries = (parsed as Record<string, unknown>).entries || (parsed as Record<string, unknown>).children || parsed
    if (Array.isArray(entries)) {
      return (
        <div className="space-y-0.5">
          {entries.map((e: any, i: number) => (
            <div key={i} className="flex items-center gap-1.5 text-xs py-0.5">
              {e.type === 'directory' || e.type === 'folder' ? (
                <FolderOpen className="w-3 h-3 text-yellow-400 flex-shrink-0" />
              ) : (
                <FileText className="w-3 h-3 text-rt-text-muted flex-shrink-0" />
              )}
              <span className={e.type === 'directory' || e.type === 'folder' ? 'text-yellow-300' : 'text-rt-text-muted'}>
                {e.name || e.path || e}
              </span>
            </div>
          ))}
        </div>
      )
    }
  }
  const lines = output.split('\n').filter(l => l.trim())
  return (
    <div className="space-y-0.5">
      {lines.map((line, i) => (
        <div key={i} className="flex items-center gap-1.5 text-xs">
          <span className="text-rt-text-muted">{line}</span>
        </div>
      ))}
    </div>
  )
}

function BrowserNavigateOutput({ input, output }: { input?: Record<string, unknown>; output: string }) {
  const url = (input?.url || '') as string
  return (
    <div className="space-y-1">
      {url && (
        <div className="flex items-center gap-1.5 text-xs">
          <ExternalLink className="w-3 h-3 text-blue-400" />
          <span className="font-mono text-blue-300 truncate max-w-full">{url}</span>
        </div>
      )}
      <pre className="text-xs text-rt-text-muted bg-rt-bg/50 rounded p-2 max-h-[200px] overflow-auto whitespace-pre-wrap font-mono">
        {output.length > 2000 ? output.slice(0, 2000) + '...' : output}
      </pre>
    </div>
  )
}

type AskUserQuestion = {
  question: string
  header?: string
  options?: Array<{ label: string; description?: string }>
  multiple?: boolean
  /** If true, the question accepts a free-text answer alongside the options. */
  custom?: boolean
}

function AskUserOutput({
  input,
  isRunning,
  callID,
}: {
  input?: Record<string, unknown>
  isRunning: boolean
  callID?: string
}) {
  const activeSession = useActiveSession()
  const replyToQuestion = useOpenCodeStore(s => s.replyToQuestion)
  const rejectQuestion = useOpenCodeStore(s => s.rejectQuestion)

  // The question is "live" when the active conversation has a pendingQuestion
  // whose tool callID matches THIS tool block — otherwise we're rendering a
  // historical/non-active tool call and the form should be display-only.
  const pendingQuestion = activeSession?.pendingQuestion
  const isLiveForThisTool = !!(
    pendingQuestion &&
    callID &&
    pendingQuestion.tool?.callID === callID
  )

  // Prefer the live request's question list — it has the canonical OC shape
  // including `custom` flags. Fall back to the tool input for display-only
  // historical renders.
  const questions: AskUserQuestion[] | undefined = useMemo(() => {
    if (isLiveForThisTool && pendingQuestion?.questions) return pendingQuestion.questions
    const fromInput = input?.questions
    if (Array.isArray(fromInput)) return fromInput as AskUserQuestion[]
    return undefined
  }, [isLiveForThisTool, pendingQuestion, input])

  const questionText = input?.question as string | undefined

  // Per-question selection state. `answers[i]` is the list of selected option
  // labels for question i. `custom[i]` is the literal text the user typed
  // into the "Other (specify)" field for question i (added to answers[i] on
  // submit when non-empty).
  const [answers, setAnswers] = useState<string[][]>(() => (questions ?? []).map(() => []))
  const [customText, setCustomText] = useState<string[]>(() => (questions ?? []).map(() => ''))
  const [submitting, setSubmitting] = useState(false)

  // Reset selections when the request changes (different requestID = new question).
  useEffect(() => {
    setAnswers((questions ?? []).map(() => []))
    setCustomText((questions ?? []).map(() => ''))
    setSubmitting(false)
  }, [pendingQuestion?.id, questions?.length])

  if (!questions || questions.length === 0) {
    // Fallback: single free-text question shape (`input.question` only).
    if (!questionText) return null
    return (
      <div className="rounded-lg border border-blue-500/30 bg-blue-500/5 p-3">
        <div className="flex items-center gap-2 text-xs text-blue-400 mb-2">
          <HelpCircle className="w-3.5 h-3.5" />
          <span className="font-medium">{isRunning ? 'Waiting for your response…' : 'Question'}</span>
        </div>
        <div className="text-sm text-rt-text">{questionText}</div>
      </div>
    )
  }

  const interactive = isLiveForThisTool && !!activeSession
  const conversationId = activeSession?.conversationId

  const toggleOption = (qi: number, label: string, multiple: boolean) => {
    setAnswers(prev => {
      const next = prev.map(a => [...a])
      if (multiple) {
        const idx = next[qi].indexOf(label)
        if (idx >= 0) next[qi].splice(idx, 1)
        else next[qi].push(label)
      } else {
        next[qi] = next[qi][0] === label ? [] : [label]
      }
      return next
    })
  }

  const setCustom = (qi: number, value: string) => {
    setCustomText(prev => {
      const next = [...prev]
      next[qi] = value
      return next
    })
  }

  const canSubmit =
    interactive &&
    !submitting &&
    questions.every((q, i) => {
      const hasOption = (answers[i]?.length ?? 0) > 0
      const hasCustom = q.custom && (customText[i] ?? '').trim().length > 0
      return hasOption || hasCustom
    })

  const handleSubmit = async () => {
    if (!canSubmit || !conversationId) return
    setSubmitting(true)
    const payload = questions.map((q, i) => {
      const picks = [...(answers[i] ?? [])]
      const custom = (customText[i] ?? '').trim()
      if (q.custom && custom) picks.push(custom)
      return picks
    })
    try {
      await replyToQuestion(conversationId, payload)
    } finally {
      setSubmitting(false)
    }
  }

  const handleReject = async () => {
    if (!interactive || !conversationId) return
    setSubmitting(true)
    try {
      await rejectQuestion(conversationId)
    } finally {
      setSubmitting(false)
    }
  }

  // Active-question index = the first one with no selection AND no custom
  // text. When all are answered, points to the last so the bar shows full.
  // Matches OC's `question-progress-segment` model where users walk through
  // questions one at a time.
  const activeIdx = (() => {
    for (let i = 0; i < questions.length; i++) {
      const hasOption = (answers[i]?.length ?? 0) > 0
      const hasCustom = (customText[i] ?? '').trim().length > 0
      if (!hasOption && !hasCustom) return i
    }
    return Math.max(0, questions.length - 1)
  })()

  return (
    <div className="rounded-lg border border-blue-500/30 bg-blue-500/5 p-3 space-y-3">
      <div className="flex items-center gap-2 text-xs text-blue-400">
        <MessageCircle className="w-3.5 h-3.5" />
        <span className="font-medium flex-1">
          {interactive ? 'Waiting for your response' : isRunning ? 'Waiting for your response…' : 'Agent Question'}
        </span>
        {questions.length > 1 && (
          <span className="text-[10px] text-blue-400/70 font-mono">
            {Math.min(activeIdx + 1, questions.length)} of {questions.length}
          </span>
        )}
      </div>
      {questions.length > 1 && (
        <div className="flex gap-1" role="progressbar" aria-label={`Question ${activeIdx + 1} of ${questions.length}`}>
          {questions.map((_q, i) => {
            const answered = (answers[i]?.length ?? 0) > 0 || (customText[i] ?? '').trim().length > 0
            const isActive = i === activeIdx && !answered
            return (
              <div
                key={i}
                className={`flex-1 h-1 rounded-sm transition-colors ${
                  answered ? 'bg-blue-400/80' : isActive ? 'bg-blue-400/40' : 'bg-blue-400/15'
                }`}
              />
            )
          })}
        </div>
      )}
      {questions.map((q, i) => (
        <div key={i} className="space-y-1.5">
          {q.header && (
            <div className="text-[10px] text-rt-text-muted/60 uppercase tracking-wide">{q.header}</div>
          )}
          <div className="text-sm text-rt-text font-medium">{q.question}</div>
          {q.options && q.options.length > 0 && (
            <div className="space-y-1">
              {q.options.map((opt, j) => {
                const selected = (answers[i] ?? []).includes(opt.label)
                const disabled = !interactive
                return (
                  <button
                    key={j}
                    type="button"
                    disabled={disabled}
                    onClick={() => toggleOption(i, opt.label, q.multiple === true)}
                    className={`w-full flex items-start gap-2 rounded border px-2.5 py-2 text-left transition-colors ${
                      selected
                        ? 'bg-blue-500/15 border-blue-400/60'
                        : 'bg-rt-bg/50 border-rt-border/20 hover:border-blue-400/40 hover:bg-blue-500/5'
                    } ${disabled ? 'cursor-default opacity-70' : 'cursor-pointer'}`}
                  >
                    <div className={`w-4 h-4 flex-shrink-0 mt-0.5 flex items-center justify-center ${
                      q.multiple ? 'rounded-sm' : 'rounded-full'
                    } border ${selected ? 'border-blue-400 bg-blue-500/40' : 'border-blue-400/50'}`}>
                      {selected && <Circle className="w-2 h-2 text-blue-50 fill-blue-50" />}
                    </div>
                    <div className="min-w-0">
                      <div className="text-xs font-medium text-rt-text">{opt.label}</div>
                      {opt.description && (
                        <div className="text-[11px] text-rt-text-muted">{opt.description}</div>
                      )}
                    </div>
                  </button>
                )
              })}
            </div>
          )}
          {q.custom && interactive && (
            <input
              type="text"
              value={customText[i] ?? ''}
              onChange={e => setCustom(i, e.target.value)}
              placeholder="Or type your own answer…"
              className="w-full text-xs px-2.5 py-1.5 rounded border border-rt-border/30 bg-rt-bg/60 text-rt-text placeholder:text-rt-text-muted/50 focus:outline-none focus:border-blue-400/60"
            />
          )}
        </div>
      ))}
      {interactive && (
        <div className="flex items-center gap-2 pt-1">
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="px-3 py-1.5 rounded text-xs font-medium bg-blue-500/80 hover:bg-blue-500 text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {submitting ? 'Submitting…' : 'Submit answers'}
          </button>
          <button
            type="button"
            onClick={handleReject}
            disabled={submitting}
            className="px-3 py-1.5 rounded text-xs font-medium text-rt-text-muted hover:text-red-400 hover:bg-red-500/10 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center gap-1"
          >
            <X className="w-3 h-3" /> Dismiss
          </button>
        </div>
      )}
    </div>
  )
}

function ReadFileOutput({ input, output }: { input?: Record<string, unknown>; output: string }) {
  const filePath = (input?.file_path || input?.path || input?.filePath || '') as string
  const isCompleted = output && output.length > 0

  return (
    <div className="space-y-1.5">
      {filePath && (
        <div className="flex items-center gap-1.5 text-xs">
          <Eye className="w-3 h-3 text-rt-text-muted/60" />
          <span className="font-mono text-rt-text-muted">{filePath}</span>
        </div>
      )}
      {isCompleted ? (
        <pre className="text-xs text-rt-text-muted bg-rt-bg/50 rounded p-2 max-h-[250px] overflow-auto whitespace-pre-wrap break-all font-mono border border-rt-border/10">
          {output.length > 4000 ? output.slice(0, 4000) + '\n... (truncated)' : output}
        </pre>
      ) : (
        <div className="text-xs text-rt-text-muted/50 italic">Reading file...</div>
      )}
    </div>
  )
}

function ListDirOutput({ input, output }: { input?: Record<string, unknown>; output: string }) {
  const dirPath = (input?.path || input?.dir_path || '') as string
  const lines = output.split('\n').filter(l => l.trim())

  return (
    <div className="space-y-1.5">
      {dirPath && (
        <div className="flex items-center gap-1.5 text-xs">
          <FolderOpen className="w-3 h-3 text-yellow-400" />
          <span className="font-mono text-rt-text-muted">{dirPath}</span>
        </div>
      )}
      <div className="flex flex-wrap gap-x-3 gap-y-0.5">
        {lines.map((line, i) => {
          const isDir = line.endsWith('/') || line.startsWith('d')
          return (
            <div key={i} className="flex items-center gap-1 text-xs">
              {isDir ? <FolderOpen className="w-3 h-3 text-yellow-400/60" /> : <FileText className="w-3 h-3 text-rt-text-muted/40" />}
              <span className={isDir ? 'text-yellow-300/80' : 'text-rt-text-muted'}>{line.replace(/\/$/, '')}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function DefaultOutput({ output, isError }: { output: string; isError: boolean }) {
  return (
    <pre className={`text-xs rounded p-2 max-h-[300px] overflow-auto whitespace-pre-wrap break-all font-mono ${
      isError ? 'text-red-300 bg-red-500/5' : 'text-rt-text-muted bg-rt-bg/50'
    }`}>
      {output.length > 5000 ? output.slice(0, 5000) + '\n... (truncated)' : output}
    </pre>
  )
}


/**
 * OC's `websearch` tool result. Input has `{query}`; output is plain text with
 * one result per stanza (title / URL / snippet). We split by blank-line and
 * try to surface each as a card. Falls through to DefaultOutput on parse miss.
 */
function WebSearchOutput({ input, output }: { input?: Record<string, unknown>; output: string }) {
  const query = (input?.query || input?.q || '') as string
  // Split into stanzas; tolerate either `\n\n` or single-line title|url|snippet rows.
  const stanzas = output.split(/\n\n+/).filter(s => s.trim())
  const results = stanzas.map((s, i) => {
    const lines = s.split('\n').map(l => l.trim()).filter(Boolean)
    const urlIdx = lines.findIndex(l => /^https?:\/\//i.test(l))
    const title = urlIdx > 0 ? lines[urlIdx - 1] : (urlIdx === 0 ? '' : lines[0] ?? '')
    const url = urlIdx >= 0 ? lines[urlIdx] : ''
    const snippet = lines.slice(urlIdx + 1).join(' ').trim() || (urlIdx === -1 ? s.trim() : '')
    return { key: i, title, url, snippet }
  }).filter(r => r.title || r.url || r.snippet)

  if (results.length === 0) return <DefaultOutput output={output} isError={false} />

  return (
    <div className="space-y-1.5">
      {query && (
        <div className="flex items-center gap-1.5 text-xs">
          <Search className="w-3 h-3 text-rt-text-muted/70" />
          <span className="font-mono text-rt-text-muted">{query}</span>
          <span className="text-[10px] text-rt-text-muted/50">· {results.length} result{results.length === 1 ? '' : 's'}</span>
        </div>
      )}
      <div className="space-y-1.5">
        {results.slice(0, 12).map(r => (
          <div key={r.key} className="rounded border border-rt-border/20 bg-rt-bg/40 p-2">
            {r.title && <div className="text-xs font-medium text-rt-text truncate">{r.title}</div>}
            {r.url && (
              <a
                href={r.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[10px] text-blue-400/80 hover:underline truncate block"
              >
                {r.url}
              </a>
            )}
            {r.snippet && <div className="text-[11px] text-rt-text-muted mt-1 line-clamp-3">{r.snippet}</div>}
          </div>
        ))}
        {results.length > 12 && (
          <div className="text-[10px] text-rt-text-muted/50 text-center">+ {results.length - 12} more</div>
        )}
      </div>
    </div>
  )
}

/**
 * OC's `apply_patch` tool result. Input is `{patch}` (a unified-diff blob —
 * `*** Update File: …` / `@@ … @@` lines). We extract the touched filenames
 * + additions/deletions counts and render a compact summary; the full diff
 * stays available in the output pre below for click-to-expand.
 */
function ApplyPatchOutput({ input, output }: { input?: Record<string, unknown>; output: string }) {
  const patch = (input?.patch || input?.diff || '') as string
  // Heuristic file scan — handles both OpenAI-style `*** Update File: foo`
  // markers and traditional unified `--- a/foo` / `+++ b/foo` lines.
  const files: Array<{ filename: string; additions: number; deletions: number }> = []
  if (patch) {
    const lines = patch.split('\n')
    let current: { filename: string; additions: number; deletions: number } | null = null
    for (const line of lines) {
      const mUpd = line.match(/^\*\*\* (?:Update|Add|Delete) File:\s+(.+)$/)
      const mDiff = line.match(/^\+\+\+\s+(?:b\/)?(.+)$/)
      if (mUpd || mDiff) {
        if (current) files.push(current)
        current = { filename: (mUpd?.[1] || mDiff?.[1] || '').trim(), additions: 0, deletions: 0 }
        continue
      }
      if (!current) continue
      if (line.startsWith('+') && !line.startsWith('+++')) current.additions++
      else if (line.startsWith('-') && !line.startsWith('---')) current.deletions++
    }
    if (current) files.push(current)
  }

  if (files.length === 0) return <DefaultOutput output={output || patch} isError={false} />

  return (
    <div className="space-y-1.5">
      <div className="space-y-1">
        {files.map((f, i) => (
          <div key={i} className="flex items-center gap-2 text-xs rounded border border-rt-border/20 bg-rt-bg/40 px-2 py-1">
            <FileEdit className="w-3.5 h-3.5 text-rt-text-muted/70 flex-shrink-0" />
            <span className="font-mono text-rt-text truncate flex-1">{f.filename}</span>
            {f.additions > 0 && <span className="text-[10px] text-emerald-400">+{f.additions}</span>}
            {f.deletions > 0 && <span className="text-[10px] text-red-400">−{f.deletions}</span>}
          </div>
        ))}
      </div>
      {output && (
        <pre className="text-[11px] text-rt-text-muted/70 bg-rt-bg/30 rounded p-2 max-h-[200px] overflow-auto whitespace-pre-wrap break-all font-mono">
          {output.length > 2000 ? output.slice(0, 2000) + '\n... (truncated)' : output}
        </pre>
      )}
    </div>
  )
}

function RichOutput({ part, output }: { part: ToolPart; output: string }) {
  const tool = part.tool
  const input = part.state.input

  if (tool === 'retrace-browser_take_screenshot' || tool === 'retrace-screenops_screenshot') {
    if (_isBase64Image(output)) {
      const format = _detectImageFormat(output)
      return <ScreenshotOutput dataUrl={`data:${format};base64,${output}`} alt="Screenshot" />
    }
  }

  if (tool === 'bash') return <BashOutput output={output} />

  if (tool === 'edit' || tool === 'write' || tool === 'multiEdit') return <FileEditOutput input={input} output={output} />

  if (tool === 'retrace-kb_search') return <KBSearchOutput output={output} />
  if (tool === 'retrace-kb_browse' || tool === 'retrace-kb_list_entries') return <KBBrowseOutput output={output} />
  if (tool === 'retrace-kb_read_file') return <DefaultOutput output={output} isError={false} />
  if (tool === 'retrace-kb_learn') {
    const success = output.toLowerCase().includes('learned') || output.toLowerCase().includes('stored')
    return (
      <div className={`rounded border p-2.5 ${success ? 'bg-green-500/5 border-green-500/20' : 'bg-red-500/5 border-red-500/20'}`}>
        <div className="flex items-center gap-1.5">
          <GraduationCap className={`w-3.5 h-3.5 ${success ? 'text-green-400' : 'text-red-400'}`} />
          <span className={`text-xs font-medium ${success ? 'text-green-300' : 'text-red-300'}`}>
            {output}
          </span>
        </div>
      </div>
    )
  }

  if (tool === 'retrace-browser_navigate') return <BrowserNavigateOutput input={input} output={output} />
  if (tool === 'retrace-browser_read_page') return <DefaultOutput output={output} isError={false} />

  if (tool === 'question' || tool === 'ask_user') return <AskUserOutput input={input} isRunning={false} callID={part.callID} />
  if (tool === 'read') return <ReadFileOutput input={input} output={output} />
  if (tool === 'list_dir') return <ListDirOutput input={input} output={output} />
  if (tool === 'glob' || tool === 'grep') return <DefaultOutput output={output} isError={false} />
  if (tool === 'websearch') return <WebSearchOutput input={input} output={output} />
  if (tool === 'apply_patch') return <ApplyPatchOutput input={input} output={output} />

  return <DefaultOutput output={output} isError={false} />
}

/**
 * Returns a short, human-readable summary of a tool's input for the collapsed
 * pill. Falls back to the first non-empty string value in the input. The goal
 * is one glanceable line like `bash · ls -la` or `Search KB · "auth"`.
 */
function getToolInputSummary(part: ToolPart): string {
  const input = (part.state.input || {}) as Record<string, unknown>
  // Prefer the canonical "primary arg" for each common tool.
  const preferKeys = ['command', 'query', 'pattern', 'url', 'file_path', 'filePath', 'path', 'title']
  for (const k of preferKeys) {
    const v = input[k]
    if (typeof v === 'string' && v.trim()) return v
  }
  // Otherwise the first non-empty string value.
  for (const v of Object.values(input)) {
    if (typeof v === 'string' && v.trim()) return v
  }
  return ''
}

/**
 * Compact tool-call pill. Single line by default — click to expand input/output.
 * Matches OpenCode's visual treatment: neutral muted background, no color-coded
 * borders for normal completion. Errors and the question/ask-user "waiting"
 * state still get color treatment because they're actionable.
 */
export default function ToolCallBlock({ part, defaultExpanded = false }: { part: ToolPart; defaultExpanded?: boolean }) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const Icon = getToolIcon(part.tool)
  const label = part.title || getToolLabel(part.tool)
  const isRunning = part.state.status === 'running' || part.state.status === 'pending'
  const isError = part.state.status === 'error'
  const isAskUser = part.tool === 'question' || part.tool === 'ask_user'
  const isWaitingForUser = isAskUser && isRunning

  const inputStr = part.state.input ? JSON.stringify(part.state.input, null, 2) : ''
  const outputStr = typeof part.state.output === 'string' ? part.state.output : JSON.stringify(part.state.output || '', null, 2)
  const isScreenshot = (part.tool === 'retrace-browser_take_screenshot' || part.tool === 'retrace-screenops_screenshot') && _isBase64Image(outputStr)
  const inputSummary = getToolInputSummary(part)

  // Auto-expand only the things the user genuinely needs to see immediately:
  // a question tool waiting for input. Everything else stays collapsed until
  // clicked — matches OC's pill-by-default UX.
  const forceExpanded = isWaitingForUser
  const effectiveExpanded = forceExpanded || expanded

  return (
    <div className={`my-1.5 rounded-md border overflow-hidden ${
      isError ? 'border-red-500/30 bg-red-500/5' :
      isWaitingForUser ? 'border-blue-500/30 bg-blue-500/5' :
      'border-rt-border/40 bg-rt-surface/30'
    }`}>
      <button
        type="button"
        onClick={() => { if (!forceExpanded) setExpanded(!effectiveExpanded) }}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-rt-surface/50 transition-colors"
      >
        {effectiveExpanded ? (
          <ChevronDown className="w-3 h-3 text-rt-text-muted/60 flex-shrink-0" />
        ) : (
          <ChevronRight className="w-3 h-3 text-rt-text-muted/60 flex-shrink-0" />
        )}
        <Icon className={`w-3.5 h-3.5 flex-shrink-0 ${
          isError ? 'text-red-400' :
          isWaitingForUser ? 'text-blue-400 animate-pulse' :
          isRunning ? 'text-rt-text-muted/70 animate-pulse' :
          'text-rt-text-muted/70'
        }`} />
        <span className={`text-xs font-medium ${
          isError ? 'text-red-300' :
          isWaitingForUser ? 'text-blue-300' :
          'text-rt-text'
        }`}>{label}</span>
        {inputSummary && !effectiveExpanded && (
          <span className="text-[11px] text-rt-text-muted/60 font-mono truncate flex-1 min-w-0">
            · {inputSummary}
          </span>
        )}
        <div className="ml-auto flex items-center gap-1.5 flex-shrink-0">
          {isWaitingForUser ? (
            <span className="text-[10px] text-blue-400 animate-pulse">waiting…</span>
          ) : isRunning ? (
            <span className="text-[10px] text-rt-text-muted/70 animate-pulse">running…</span>
          ) : isError ? (
            <span className="text-[10px] text-red-400">error</span>
          ) : (
            <span className="text-[10px] text-rt-success/70">✓</span>
          )}
        </div>
      </button>
      {effectiveExpanded && (
        <div className="border-t border-rt-border/20 px-3 py-2 space-y-2">
          {inputStr && !isScreenshot && !isAskUser && (
            <div>
              <div className="text-[10px] font-semibold text-rt-text-muted/60 uppercase tracking-wide mb-1">Input</div>
              <pre className="text-xs text-rt-text-muted bg-rt-bg/50 rounded p-2 max-h-[200px] overflow-auto whitespace-pre-wrap break-all font-mono">
                {inputStr.length > 2000 ? inputStr.slice(0, 2000) + '\n... (truncated)' : inputStr}
              </pre>
            </div>
          )}
          {isAskUser && part.state.input && (
            <AskUserOutput input={part.state.input as Record<string, unknown>} isRunning={isRunning} callID={part.callID} />
          )}
          {outputStr && !isAskUser && (
            <div>
              <div className="text-[10px] font-semibold text-rt-text-muted/60 uppercase tracking-wide mb-1">
                {isScreenshot ? 'Screenshot' : 'Output'}
              </div>
              {isError ? (
                <DefaultOutput output={outputStr} isError />
              ) : (
                <RichOutput part={part} output={outputStr} />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
