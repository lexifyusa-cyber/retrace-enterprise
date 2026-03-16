import { useState, useEffect, useRef, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Send, Loader2, FileCode, FileText, Image, AlertTriangle, ChevronDown, ChevronUp, Package, Check, X, Clock, Search, Database } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import { productApi, queryApi } from '@/utils/api'
import { SourceReference, Product, StepTimings } from '@/types'

// ---------------------------------------------------------------------------
// Types for the streaming pipeline
// ---------------------------------------------------------------------------

interface StreamState {
  status: 'idle' | 'streaming' | 'done' | 'error'
  step: string
  stepMessage: string
  answer: string
  sources: SourceReference[]
  relatedQueries: string[]
  confidenceScore: number
  timings: Partial<StepTimings>
  error: string | null
  question: string
}

const INITIAL_STREAM: StreamState = {
  status: 'idle',
  step: '',
  stepMessage: '',
  answer: '',
  sources: [],
  relatedQueries: [],
  confidenceScore: 0,
  timings: {},
  error: null,
  question: '',
}

export default function Query() {
  const [question, setQuestion] = useState('')
  const [selectedProductIds, setSelectedProductIds] = useState<string[]>([])
  const [showProductPicker, setShowProductPicker] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [stream, setStream] = useState<StreamState>(INITIAL_STREAM)
  const [autoSelected, setAutoSelected] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const queryClient = useQueryClient()

  const { data: products = [] } = useQuery({
    queryKey: ['products'],
    queryFn: () => productApi.list(),
  })

  const trainedProducts = products.filter(p =>
    p.folder_groups.some(g => g.training_status === 'completed')
  )

  // Auto-select first trained product on load
  useEffect(() => {
    if (!autoSelected && trainedProducts.length > 0 && selectedProductIds.length === 0) {
      setSelectedProductIds([trainedProducts[0].product_id])
      setAutoSelected(true)
    }
  }, [trainedProducts, autoSelected, selectedProductIds.length])

  const { data: history = [] } = useQuery({
    queryKey: ['query-history'],
    queryFn: () => queryApi.getHistory(10),
  })

  const toggleProduct = (productId: string) => {
    setSelectedProductIds(prev =>
      prev.includes(productId)
        ? prev.filter(id => id !== productId)
        : [...prev, productId]
    )
  }

  const selectAll = () => {
    setSelectedProductIds(trainedProducts.map(p => p.product_id))
  }

  const clearAll = () => {
    setSelectedProductIds([])
  }

  // ── Streaming query handler ────────────────────────────────────────
  const handleAsk = useCallback(async (q: string, pids: string[]) => {
    if (!q.trim() || pids.length === 0) return

    // Abort any in-progress stream
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    setStream({ ...INITIAL_STREAM, status: 'streaming', question: q })
    setQuestion('')

    try {
      const response = await fetch('/api/v1/query/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q, product_ids: pids }),
        signal: controller.signal,
      })

      if (!response.ok) {
        const err = await response.text()
        setStream(prev => ({ ...prev, status: 'error', error: err }))
        return
      }

      const reader = response.body?.getReader()
      if (!reader) {
        setStream(prev => ({ ...prev, status: 'error', error: 'No stream reader' }))
        return
      }

      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // Parse SSE events from buffer
        const lines = buffer.split('\n')
        buffer = lines.pop() || '' // keep incomplete line in buffer

        let currentEvent = ''
        let currentData = ''

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            currentData = line.slice(6)
            // Process the event
            if (currentEvent && currentData) {
              try {
                const parsed = JSON.parse(currentData)
                handleSSEEvent(currentEvent, parsed)
              } catch {
                // ignore malformed data
              }
            }
            currentEvent = ''
            currentData = ''
          }
        }
      }

      // Mark as done if not already
      setStream(prev =>
        prev.status === 'streaming' ? { ...prev, status: 'done' } : prev
      )

      // Refresh history
      queryClient.invalidateQueries({ queryKey: ['query-history'] })
    } catch (err: any) {
      if (err.name === 'AbortError') return
      setStream(prev => ({
        ...prev,
        status: 'error',
        error: err.message || 'Stream failed',
      }))
    }
  }, [queryClient])

  const handleSSEEvent = (event: string, data: any) => {
    switch (event) {
      case 'status':
        setStream(prev => ({
          ...prev,
          step: data.step,
          stepMessage: data.message,
        }))
        break

      case 'timings':
        setStream(prev => ({
          ...prev,
          timings: { ...prev.timings, ...data },
        }))
        break

      case 'classification':
        // Query routing info — no UI state change needed, but could display
        break

      case 'sources':
        setStream(prev => ({ ...prev, sources: data }))
        break

      case 'token':
        setStream(prev => ({
          ...prev,
          answer: prev.answer + data.content,
        }))
        break

      case 'related':
        setStream(prev => ({ ...prev, relatedQueries: data }))
        break

      case 'done':
        setStream(prev => ({
          ...prev,
          status: 'done',
          confidenceScore: data.confidence_score || 0,
          timings: data.timings || prev.timings,
        }))
        break

      case 'error':
        setStream(prev => ({
          ...prev,
          status: 'error',
          error: data.detail || 'Unknown error',
        }))
        break
    }
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!question.trim() || stream.status === 'streaming' || selectedProductIds.length === 0) return
    handleAsk(question, selectedProductIds)
  }

  const handleHistoryClick = (q: string) => {
    setQuestion(q)
    setShowHistory(false)
  }

  const selectedNames = trainedProducts
    .filter(p => selectedProductIds.includes(p.product_id))
    .map(p => p.product_name)

  const isStreaming = stream.status === 'streaming'
  const hasResult = stream.status === 'done' || (stream.status === 'streaming' && stream.answer.length > 0)

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="p-8 pb-4 border-b border-rt-border">
        <h1 className="text-3xl font-display font-bold mb-2">Ask a Question</h1>
        <p className="text-rt-text-muted">
          Query your knowledge base with natural language
        </p>
      </div>

      {/* Product selector bar */}
      <div className="px-8 py-3 border-b border-rt-border bg-rt-bg-light">
        <div className="flex items-center gap-3">
          <span className="text-sm font-medium text-rt-text-muted whitespace-nowrap">
            Search in:
          </span>

          {trainedProducts.length === 0 ? (
            <span className="text-sm text-rt-text-muted italic">
              No trained products yet. Train a product first.
            </span>
          ) : (
            <div className="relative flex-1">
              <button
                type="button"
                onClick={() => setShowProductPicker(!showProductPicker)}
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-rt-border hover:border-rt-text-muted transition-colors text-sm w-full max-w-lg"
              >
                <Package className="w-4 h-4 text-rt-primary flex-shrink-0" />
                <span className="flex-1 text-left truncate">
                  {selectedProductIds.length === 0
                    ? 'Select products…'
                    : selectedProductIds.length === trainedProducts.length
                    ? 'All products'
                    : selectedNames.join(', ')}
                </span>
                <span className="badge badge-info text-xs">{selectedProductIds.length}</span>
                <ChevronDown className={`w-4 h-4 text-rt-text-muted transition-transform ${showProductPicker ? 'rotate-180' : ''}`} />
              </button>

              <AnimatePresence>
                {showProductPicker && (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -4 }}
                    className="absolute top-full left-0 mt-1 w-full max-w-lg bg-rt-bg-light border border-rt-border rounded-lg shadow-xl z-50 overflow-hidden"
                  >
                    <div className="flex items-center justify-between px-3 py-2 border-b border-rt-border bg-rt-surface/50">
                      <span className="text-xs text-rt-text-muted">{trainedProducts.length} trained products</span>
                      <div className="flex gap-2">
                        <button type="button" onClick={selectAll} className="text-xs text-rt-primary hover:underline">Select all</button>
                        <button type="button" onClick={clearAll} className="text-xs text-rt-text-muted hover:underline">Clear</button>
                      </div>
                    </div>

                    <div className="max-h-60 overflow-y-auto divide-y divide-rt-border">
                      {trainedProducts.map((product) => {
                        const isSelected = selectedProductIds.includes(product.product_id)
                        const stats = product.folder_groups.find(g => g.metadata?.chunks_indexed)?.metadata as Record<string, any> | undefined
                        const chunksIndexed = stats?.chunks_indexed || 0

                        return (
                          <button
                            key={product.product_id}
                            type="button"
                            onClick={() => toggleProduct(product.product_id)}
                            className={`w-full flex items-center gap-3 px-3 py-2.5 text-left hover:bg-rt-surface transition-colors ${
                              isSelected ? 'bg-rt-primary/5' : ''
                            }`}
                          >
                            <div className={`w-5 h-5 rounded border flex items-center justify-center flex-shrink-0 transition-colors ${
                              isSelected ? 'bg-rt-primary border-rt-primary' : 'border-rt-border'
                            }`}>
                              {isSelected && <Check className="w-3.5 h-3.5 text-white" />}
                            </div>
                            <div className="flex-1 min-w-0">
                              <p className="text-sm font-medium truncate">{product.product_name}</p>
                              <p className="text-xs text-rt-text-muted">
                                {product.folder_groups.length} groups · {chunksIndexed.toLocaleString()} datasets
                              </p>
                            </div>
                          </button>
                        )
                      })}
                    </div>

                    <div className="px-3 py-2 border-t border-rt-border">
                      <button type="button" onClick={() => setShowProductPicker(false)} className="btn-primary text-xs w-full py-1.5">
                        Done
                      </button>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}

          {/* Selected product pills */}
          {selectedProductIds.length > 0 && selectedProductIds.length <= 3 && (
            <div className="hidden lg:flex items-center gap-1.5">
              {selectedNames.map((name, i) => (
                <span
                  key={i}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-rt-primary/10 text-rt-primary text-xs"
                >
                  {name}
                  <button type="button" onClick={() => toggleProduct(selectedProductIds[i])} className="hover:text-rt-accent">
                    <X className="w-3 h-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Main content */}
      <div className="flex-1 overflow-auto p-8">
        <AnimatePresence mode="wait">
          {/* Streaming / result view */}
          {(isStreaming || hasResult || stream.status === 'error') ? (
            <motion.div
              key="result"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -20 }}
            >
              <StreamingAnswer stream={stream} />
            </motion.div>
          ) : stream.status === 'done' && !stream.answer ? (
            <motion.div
              key="no-result"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="card text-center py-8 text-rt-text-muted"
            >
              No results found for your query.
            </motion.div>
          ) : (
            <motion.div
              key="empty"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="text-center py-16"
            >
              <div className="w-20 h-20 mx-auto mb-6 rounded-2xl bg-gradient-to-br from-rt-primary/20 to-rt-primary-dark/20 flex items-center justify-center">
                <Send className="w-10 h-10 text-rt-primary" />
              </div>
              <h3 className="text-xl font-display font-semibold mb-2">
                What would you like to know?
              </h3>
              <p className="text-rt-text-muted max-w-md mx-auto mb-8">
                Ask questions about your codebase, documentation, incidents, or architecture.
              </p>

              <div className="max-w-xl mx-auto">
                <p className="text-sm text-rt-text-muted mb-3">Try asking:</p>
                <div className="flex flex-wrap gap-2 justify-center">
                  {[
                    'How does the authentication system work?',
                    'What causes login 500 errors?',
                    'Show me the database schema',
                    'How to add a new API endpoint?',
                  ].map((q) => (
                    <button
                      key={q}
                      onClick={() => setQuestion(q)}
                      className="px-3 py-1.5 rounded-full bg-rt-surface text-sm hover:bg-rt-border transition-colors"
                    >
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Input area */}
      <div className="p-8 pt-4 border-t border-rt-border bg-rt-bg-light">
        <form onSubmit={handleSubmit} className="relative">
          {/* History dropdown */}
          {showHistory && history.length > 0 && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="absolute bottom-full left-0 right-0 mb-2 bg-rt-surface border border-rt-border rounded-lg shadow-xl max-h-60 overflow-y-auto"
            >
              {history.map((h: { query_id: string; question: string }) => (
                <button
                  key={h.query_id}
                  type="button"
                  onClick={() => handleHistoryClick(h.question)}
                  className="w-full p-3 text-left hover:bg-rt-border transition-colors text-sm truncate"
                >
                  {h.question}
                </button>
              ))}
            </motion.div>
          )}

          <div className="flex gap-4">
            <div className="relative flex-1">
              <input
                type="text"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onFocus={() => setShowHistory(true)}
                onBlur={() => setTimeout(() => setShowHistory(false), 200)}
                placeholder={
                  selectedProductIds.length === 0
                    ? 'Select products above first…'
                    : 'Ask anything about your systems...'
                }
                className="input pr-12"
                disabled={isStreaming || selectedProductIds.length === 0}
              />
              {history.length > 0 && (
                <button
                  type="button"
                  onClick={() => setShowHistory(!showHistory)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 p-1 text-rt-text-muted hover:text-rt-text"
                >
                  {showHistory ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                </button>
              )}
            </div>
            <button
              type="submit"
              disabled={!question.trim() || isStreaming || selectedProductIds.length === 0}
              className="btn-primary flex items-center gap-2"
            >
              {isStreaming ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Send className="w-4 h-4" />
              )}
              Ask
            </button>
          </div>

          {selectedProductIds.length === 0 && trainedProducts.length > 0 && (
            <p className="text-xs text-rt-warning mt-2">
              Select at least one product to search
            </p>
          )}
        </form>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Streaming answer display
// ---------------------------------------------------------------------------

function StreamingAnswer({ stream }: { stream: StreamState }) {
  const answerEndRef = useRef<HTMLDivElement>(null)
  const [showAllSources, setShowAllSources] = useState(false)

  const isStreaming = stream.status === 'streaming'
  const isDone = stream.status === 'done'

  // Auto-scroll as tokens arrive
  useEffect(() => {
    if (isStreaming && answerEndRef.current) {
      answerEndRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [stream.answer, isStreaming])

  const confidenceColor = stream.confidenceScore >= 0.8
    ? 'text-rt-success'
    : stream.confidenceScore >= 0.5
    ? 'text-rt-warning'
    : 'text-rt-accent'

  const timings = stream.timings as StepTimings

  return (
    <div className="space-y-6">
      {/* Question */}
      <div className="card bg-rt-primary/5 border-rt-primary/20">
        <p className="font-medium">{stream.question}</p>
      </div>

      {/* Pipeline status indicator */}
      {isStreaming && !stream.answer && (
        <div className="card">
          <PipelineStatus step={stream.step} message={stream.stepMessage} timings={stream.timings} />
        </div>
      )}

      {/* Error */}
      {stream.status === 'error' && (
        <div className="card bg-red-500/5 border-red-500/20">
          <div className="flex items-center gap-3">
            <AlertTriangle className="w-5 h-5 text-red-400" />
            <div>
              <p className="font-medium text-red-400">Query failed</p>
              <p className="text-sm text-rt-text-muted mt-1">
                {stream.error || 'An error occurred. Check Settings and try again.'}
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Answer (streams in real-time) */}
      {stream.answer && (
        <div className="card">
          {/* Streaming indicator */}
          {isStreaming && (
            <div className="flex items-center gap-2 mb-4 pb-3 border-b border-rt-border">
              <div className="flex gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-rt-primary animate-bounce" style={{ animationDelay: '0ms' }} />
                <span className="w-1.5 h-1.5 rounded-full bg-rt-primary animate-bounce" style={{ animationDelay: '150ms' }} />
                <span className="w-1.5 h-1.5 rounded-full bg-rt-primary animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
              <span className="text-xs text-rt-text-muted">Generating answer...</span>
            </div>
          )}

          <div className="prose">
            <ReactMarkdown>{stream.answer}</ReactMarkdown>
          </div>
          <div ref={answerEndRef} />

          {/* Confidence & timings (show when done) */}
          {isDone && (
            <>
              <div className="mt-6 pt-4 border-t border-rt-border flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-rt-text-muted">Confidence:</span>
                  <div className="flex items-center gap-1">
                    <div className="w-24 h-2 bg-rt-surface rounded-full overflow-hidden">
                      <div
                        className={`h-full ${
                          stream.confidenceScore >= 0.8 ? 'bg-rt-success' :
                          stream.confidenceScore >= 0.5 ? 'bg-rt-warning' :
                          'bg-rt-accent'
                        }`}
                        style={{ width: `${stream.confidenceScore * 100}%` }}
                      />
                    </div>
                    <span className={`text-sm font-medium ${confidenceColor}`}>
                      {Math.round(stream.confidenceScore * 100)}%
                    </span>
                  </div>
                </div>
                <span className="text-sm text-rt-text-muted">
                  {timings.total_ms || 0}ms
                </span>
              </div>

              {timings.total_ms && <TimingsBreakdown timings={timings} />}
            </>
          )}
        </div>
      )}

      {/* Sources */}
      {stream.sources.length > 0 && (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-display font-semibold">Sources</h3>
            <span className="text-sm text-rt-text-muted">
              {stream.sources.length} references
            </span>
          </div>

          <div className="space-y-3">
            {(showAllSources ? stream.sources : stream.sources.slice(0, 3)).map((source, i) => (
              <SourceCard key={i} source={source} />
            ))}
          </div>

          {stream.sources.length > 3 && (
            <button
              onClick={() => setShowAllSources(!showAllSources)}
              className="text-sm text-rt-primary hover:underline mt-4"
            >
              {showAllSources ? 'Show less' : `Show ${stream.sources.length - 3} more`}
            </button>
          )}
        </div>
      )}

      {/* Related Questions */}
      {stream.relatedQueries.length > 0 && isDone && (
        <div className="card">
          <h3 className="font-display font-semibold mb-4">Related Questions</h3>
          <div className="flex flex-wrap gap-2">
            {stream.relatedQueries.map((q, i) => (
              <button
                key={i}
                className="px-3 py-1.5 rounded-full bg-rt-surface text-sm hover:bg-rt-border transition-colors"
              >
                {q}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Pipeline status steps
// ---------------------------------------------------------------------------

function PipelineStatus({ step, message, timings }: { step: string; message: string; timings: Partial<StepTimings> }) {
  const steps = [
    { id: 'init', label: 'Initialize', icon: Database, timing: timings.init_clients_ms },
    { id: 'classify', label: 'Classify Question', icon: Search, timing: timings.classify_ms },
    { id: 'context', label: 'Context Assembly', icon: FileText, timing: timings.context_assembly_ms },
    { id: 'answer', label: 'LLM Answer', icon: Send, timing: timings.llm_answer_ms },
    { id: 'related', label: 'Related Queries', icon: Package, timing: timings.related_queries_ms },
  ]

  const currentIdx = steps.findIndex(s => s.id === step)

  return (
    <div className="space-y-3">
      {steps.map((s, idx) => {
        const isActive = s.id === step
        const isComplete = idx < currentIdx || (s.timing !== undefined && s.timing > 0)
        const Icon = s.icon

        return (
          <div key={s.id} className="flex items-center gap-3">
            <div className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 transition-all ${
              isActive
                ? 'bg-rt-primary text-white'
                : isComplete
                ? 'bg-rt-success/20 text-rt-success'
                : 'bg-rt-surface text-rt-text-muted'
            }`}>
              {isActive ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : isComplete ? (
                <Check className="w-4 h-4" />
              ) : (
                <Icon className="w-4 h-4" />
              )}
            </div>
            <div className="flex-1">
              <p className={`text-sm ${isActive ? 'font-medium' : isComplete ? 'text-rt-text-muted' : 'text-rt-text-muted/50'}`}>
                {isActive ? message : s.label}
              </p>
            </div>
            {s.timing !== undefined && s.timing > 0 && (
              <span className="text-xs text-rt-text-muted">{s.timing}ms</span>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Timings breakdown bar
// ---------------------------------------------------------------------------

function TimingsBreakdown({ timings }: { timings: StepTimings }) {
  const steps = [
    { label: 'Init Clients', ms: timings.init_clients_ms || 0, color: 'bg-gray-400' },
    { label: 'Classify', ms: timings.classify_ms || 0, color: 'bg-yellow-400' },
    { label: 'Context Assembly', ms: timings.context_assembly_ms || 0, color: 'bg-green-400' },
    { label: 'LLM Answer', ms: timings.llm_answer_ms || 0, color: 'bg-purple-400' },
    { label: 'Related Queries', ms: timings.related_queries_ms || 0, color: 'bg-orange-400' },
  ]

  const total = timings.total_ms || 1

  return (
    <div className="mt-4 pt-4 border-t border-rt-border">
      <div className="flex items-center gap-2 mb-3">
        <Clock className="w-4 h-4 text-rt-text-muted" />
        <span className="text-sm font-medium text-rt-text-muted">Pipeline Breakdown</span>
        <span className="text-xs text-rt-text-muted ml-auto">{total}ms total</span>
      </div>

      {/* Stacked bar */}
      <div className="flex h-3 rounded-full overflow-hidden mb-3">
        {steps.map((step) => (
          <div
            key={step.label}
            className={`${step.color} transition-all`}
            style={{ width: `${Math.max((step.ms / total) * 100, 0.5)}%` }}
            title={`${step.label}: ${step.ms}ms`}
          />
        ))}
      </div>

      {/* Legend */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        {steps.map((step) => (
          <div key={step.label} className="flex items-center gap-1.5">
            <div className={`w-2.5 h-2.5 rounded-full ${step.color} flex-shrink-0`} />
            <div className="min-w-0">
              <p className="text-xs text-rt-text-muted truncate">{step.label}</p>
              <p className="text-xs font-medium">
                {step.ms}ms
                <span className="text-rt-text-muted ml-1">
                  ({Math.round((step.ms / total) * 100)}%)
                </span>
              </p>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Source card
// ---------------------------------------------------------------------------

function SourceCard({ source }: { source: SourceReference }) {
  const typeIcons: Record<string, typeof FileCode> = {
    code: FileCode,
    documentation: FileText,
    diagram: Image,
    incident: AlertTriangle,
  }

  const Icon = typeIcons[source.type] || FileText

  const typeColors: Record<string, string> = {
    code: 'bg-blue-500/10 text-blue-400',
    documentation: 'bg-green-500/10 text-green-400',
    diagram: 'bg-purple-500/10 text-purple-400',
    incident: 'bg-orange-500/10 text-orange-400',
  }

  return (
    <div className="p-3 rounded-lg bg-rt-surface/50">
      <div className="flex items-start gap-3">
        <div className={`p-2 rounded-lg ${typeColors[source.type] || 'bg-rt-surface text-rt-text-muted'}`}>
          <Icon className="w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="badge badge-info">{source.type}</span>
          </div>
          {source.file_path && (
            <p className="text-xs font-mono text-rt-text-muted truncate">
              {source.file_path}
            </p>
          )}
          {source.snippet && (
            <p className="text-sm mt-2 line-clamp-2 text-rt-text-muted">
              {source.snippet}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
