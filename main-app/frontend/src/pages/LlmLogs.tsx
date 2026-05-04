import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Clock, Code2, Zap, AlertTriangle } from 'lucide-react'
import { agentApi, LlmLog } from '../utils/api'

export default function LlmLogs() {
  const [selectedConversationId, setSelectedConversationId] = useState<string>('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  // Fetch list of conversations with logs
  const { data: conversations = [], isLoading: loadingConvs } = useQuery({
    queryKey: ['llm-log-conversations'],
    queryFn: agentApi.listLlmLogConversations,
    refetchInterval: 5000,
  })

  // Auto-select the first (most recent) conversation
  useMemo(() => {
    if (!selectedConversationId && conversations.length > 0) {
      setSelectedConversationId(conversations[0].conversation_id)
    }
  }, [conversations, selectedConversationId])

  // Fetch logs for the selected conversation
  const { data: logs = [], isLoading: loadingLogs } = useQuery({
    queryKey: ['llm-logs', selectedConversationId],
    queryFn: () => agentApi.getLlmLogs(selectedConversationId),
    enabled: !!selectedConversationId,
    refetchInterval: 3000,
  })

  const toggleExpand = (logId: string) => {
    const next = new Set(expanded)
    if (next.has(logId)) next.delete(logId)
    else next.add(logId)
    setExpanded(next)
  }

  // Group logs by iteration for timeline display
  const totalTokens = useMemo(
    () => logs.reduce((acc, l) => acc + (l.tokens?.total || 0), 0),
    [logs]
  )
  const totalLatency = useMemo(
    () => logs.reduce((acc, l) => acc + (l.latency_ms || 0), 0),
    [logs]
  )

  return (
    <div className="p-8 max-w-7xl mx-auto">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center gap-3 mb-2">
          <Zap className="w-6 h-6 text-rt-primary" />
          <h1 className="text-3xl font-serif font-bold text-rt-text">LLM Logs</h1>
          <span className="px-2 py-0.5 text-xs font-bold bg-rt-primary/20 text-rt-primary rounded">
            DEV
          </span>
        </div>
        <p className="text-rt-text-muted text-sm">
          Inspect every LLM request/response with token usage for each conversation iteration.
        </p>
      </div>

      {/* Conversation picker */}
      <div className="mb-6 flex items-center gap-4">
        <label className="text-sm font-medium text-rt-text">Conversation:</label>
        <select
          value={selectedConversationId}
          onChange={(e) => {
            setSelectedConversationId(e.target.value)
            setExpanded(new Set())
          }}
          disabled={loadingConvs || conversations.length === 0}
          className="flex-1 max-w-2xl px-3 py-2 bg-rt-bg-light border border-rt-border rounded-md text-sm text-rt-text focus:outline-none focus:ring-2 focus:ring-rt-primary"
        >
          {loadingConvs && <option>Loading...</option>}
          {!loadingConvs && conversations.length === 0 && (
            <option>No LLM logs yet — send a message in Agent chat first</option>
          )}
          {conversations.map((c) => (
            <option key={c.conversation_id} value={c.conversation_id}>
              {c.title} ({c.log_count} calls) — {c.updated_at?.slice(0, 19).replace('T', ' ')}
            </option>
          ))}
        </select>
      </div>

      {/* Summary stats */}
      {logs.length > 0 && (
        <div className="mb-6 grid grid-cols-4 gap-4">
          <StatCard label="Iterations" value={logs.length.toString()} />
          <StatCard label="Total tokens" value={totalTokens.toLocaleString()} />
          <StatCard label="Total latency" value={`${(totalLatency / 1000).toFixed(2)}s`} />
          <StatCard label="Avg tokens / call" value={Math.round(totalTokens / logs.length).toString()} />
        </div>
      )}

      {/* Timeline */}
      {loadingLogs && <div className="text-rt-text-muted text-sm">Loading logs...</div>}
      {!loadingLogs && selectedConversationId && logs.length === 0 && (
        <div className="text-rt-text-muted text-sm">No logs found for this conversation.</div>
      )}

      {logs.length > 0 && (
        <div className="relative">
          {/* Vertical timeline line */}
          <div className="absolute left-4 top-0 bottom-0 w-px bg-rt-border" />

          <div className="space-y-3">
            {logs.map((log, idx) => (
              <LogEntry
                key={log.log_id}
                log={log}
                index={idx}
                expanded={expanded.has(log.log_id)}
                onToggle={() => toggleExpand(log.log_id)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="p-4 bg-rt-bg-light border border-rt-border rounded-lg">
      <div className="text-xs text-rt-text-muted uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-bold text-rt-text mt-1">{value}</div>
    </div>
  )
}

function LogEntry({
  log,
  index,
  expanded,
  onToggle,
}: {
  log: LlmLog
  index: number
  expanded: boolean
  onToggle: () => void
}) {
  // Parse request messages JSON
  const parsedMessages = useMemo(() => {
    try {
      return log.request_messages ? JSON.parse(log.request_messages) : []
    } catch {
      return []
    }
  }, [log.request_messages])

  const timestamp = log.created_at
    ? new Date(log.created_at).toLocaleString()
    : ''

  return (
    <div className="relative pl-12">
      {/* Timeline dot */}
      <div className="absolute left-[10px] top-4 w-3 h-3 rounded-full bg-rt-primary border-2 border-rt-bg" />

      <div className="bg-rt-bg-light border border-rt-border rounded-lg overflow-hidden">
        {/* Header - always visible */}
        <button
          onClick={onToggle}
          className="w-full px-4 py-3 flex items-center gap-3 hover:bg-rt-primary-fixed/10 transition-colors text-left"
        >
          {expanded ? (
            <ChevronDown className="w-4 h-4 text-rt-text-muted flex-shrink-0" />
          ) : (
            <ChevronRight className="w-4 h-4 text-rt-text-muted flex-shrink-0" />
          )}

          <span className="px-2 py-0.5 text-xs font-mono bg-rt-primary/20 text-rt-primary rounded flex-shrink-0">
            #{log.iteration || index + 1}
          </span>

          <span className="text-sm font-medium text-rt-text truncate flex-1">
            {log.model_name || 'unknown model'}
            {log.model_provider && (
              <span className="text-rt-text-muted ml-2">({log.model_provider})</span>
            )}
          </span>

          {log.error && (
            <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
          )}

          <div className="flex items-center gap-3 text-xs text-rt-text-muted flex-shrink-0">
            <div className="flex items-center gap-1">
              <Code2 className="w-3 h-3" />
              <span>{log.tokens.input.toLocaleString()} in</span>
            </div>
            <div className="flex items-center gap-1">
              <span>{log.tokens.output.toLocaleString()} out</span>
            </div>
            {log.tokens.cache_read > 0 && (
              <div className="flex items-center gap-1 text-green-500">
                <span>{log.tokens.cache_read.toLocaleString()} cache</span>
              </div>
            )}
            {log.tokens.reasoning > 0 && (
              <div className="flex items-center gap-1 text-purple-500">
                <span>{log.tokens.reasoning.toLocaleString()} reasoning</span>
              </div>
            )}
            <div className="flex items-center gap-1">
              <Clock className="w-3 h-3" />
              <span>{(log.latency_ms / 1000).toFixed(2)}s</span>
            </div>
            <div className="text-rt-text-muted/60">{timestamp}</div>
          </div>
        </button>

        {/* Expanded details */}
        {expanded && (
          <div className="border-t border-rt-border bg-rt-bg p-4 space-y-4">
            {/* Token breakdown */}
            <div>
              <div className="text-xs font-semibold text-rt-text-muted uppercase tracking-wide mb-2">
                Token Breakdown
              </div>
              <div className="grid grid-cols-6 gap-2 text-sm">
                <TokenStat label="Input" value={log.tokens.input} />
                <TokenStat label="Output" value={log.tokens.output} />
                <TokenStat label="Total" value={log.tokens.total} />
                <TokenStat label="Cache read" value={log.tokens.cache_read} color="green" />
                <TokenStat label="Cache create" value={log.tokens.cache_creation} color="green" />
                <TokenStat label="Reasoning" value={log.tokens.reasoning} color="purple" />
              </div>
            </div>

            {/* Error */}
            {log.error && (
              <div>
                <div className="text-xs font-semibold text-red-500 uppercase tracking-wide mb-2">
                  Error
                </div>
                <pre className="text-xs bg-red-900/20 border border-red-900/40 rounded p-3 text-red-300 overflow-x-auto whitespace-pre-wrap">
                  {log.error}
                </pre>
              </div>
            )}

            {/* Request messages */}
            <div>
              <div className="text-xs font-semibold text-rt-text-muted uppercase tracking-wide mb-2">
                Request Messages ({parsedMessages.length})
              </div>
              <div className="space-y-2 max-h-96 overflow-y-auto">
                {parsedMessages.map((m: any, i: number) => (
                  <MessageBlock key={i} message={m} />
                ))}
              </div>
            </div>

            {/* Response */}
            <div>
              <div className="text-xs font-semibold text-rt-text-muted uppercase tracking-wide mb-2">
                Response
              </div>
              <pre className="text-xs bg-rt-bg-light border border-rt-border rounded p-3 text-rt-text overflow-x-auto whitespace-pre-wrap max-h-96 overflow-y-auto">
                {log.response_content || '(empty)'}
              </pre>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function TokenStat({
  label,
  value,
  color = 'default',
}: {
  label: string
  value: number
  color?: 'default' | 'green' | 'purple'
}) {
  const colorClass = {
    default: 'text-rt-text',
    green: 'text-green-500',
    purple: 'text-purple-500',
  }[color]
  return (
    <div className="p-2 bg-rt-bg-light border border-rt-border rounded">
      <div className="text-[10px] text-rt-text-muted uppercase">{label}</div>
      <div className={`text-lg font-bold font-mono ${colorClass}`}>
        {value.toLocaleString()}
      </div>
    </div>
  )
}

function MessageBlock({ message }: { message: any }) {
  const role = message.role || 'unknown'
  const content = message.content

  const roleColors: Record<string, string> = {
    system: 'border-blue-500/40 bg-blue-900/10',
    user: 'border-green-500/40 bg-green-900/10',
    human: 'border-green-500/40 bg-green-900/10',
    assistant: 'border-purple-500/40 bg-purple-900/10',
    ai: 'border-purple-500/40 bg-purple-900/10',
    tool: 'border-orange-500/40 bg-orange-900/10',
  }
  const colorClass = roleColors[role.toLowerCase()] || 'border-rt-border bg-rt-bg-light'

  return (
    <div className={`border rounded p-3 ${colorClass}`}>
      <div className="text-[10px] font-bold uppercase tracking-wide text-rt-text-muted mb-1">
        {role}
      </div>
      {typeof content === 'string' ? (
        <pre className="text-xs text-rt-text whitespace-pre-wrap break-words font-mono">
          {content}
        </pre>
      ) : Array.isArray(content) ? (
        <div className="space-y-2">
          {content.map((part: any, i: number) => {
            if (typeof part === 'string') {
              return (
                <pre key={i} className="text-xs text-rt-text whitespace-pre-wrap break-words font-mono">
                  {part}
                </pre>
              )
            }
            if (part?.type === 'text') {
              return (
                <pre key={i} className="text-xs text-rt-text whitespace-pre-wrap break-words font-mono">
                  {part.text}
                </pre>
              )
            }
            if (part?.type === 'image_url') {
              const imgSrc = part.image_url?.url || part.image_url_preview || ''
              if (imgSrc.startsWith('data:image/')) {
                return (
                  <div key={i} className="my-2">
                    <img
                      src={imgSrc}
                      alt="Browser screenshot"
                      className="max-w-full rounded border border-rt-border"
                      style={{ maxHeight: '400px', objectFit: 'contain' }}
                    />
                  </div>
                )
              }
              return (
                <div key={i} className="text-xs text-rt-text-muted italic">
                  🖼️ [image: {part.image_url_preview || imgSrc.slice(0, 100) || 'base64 data'}]
                </div>
              )
            }
            return (
              <pre key={i} className="text-xs text-rt-text-muted whitespace-pre-wrap break-words font-mono">
                {JSON.stringify(part, null, 2)}
              </pre>
            )
          })}
        </div>
      ) : (
        <pre className="text-xs text-rt-text-muted whitespace-pre-wrap break-words font-mono">
          {JSON.stringify(content, null, 2)}
        </pre>
      )}
    </div>
  )
}
