import { useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Loader2, Check, Server, Code2, Wifi, HardDrive } from 'lucide-react'
import toast from 'react-hot-toast'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface EndpointInfo {
  method: string
  path: string
  description: string
  suggested_tool_name: string
  read_only: boolean
}

interface MCPBuilderConfig {
  language: 'typescript' | 'python'
  transport: 'stdio' | 'http'
  selected_endpoints: EndpointInfo[]
  output_dir?: string
}

interface MCPBuilderDialogProps {
  productId: string
  productName: string
  onClose: () => void
  onBuild: (config: MCPBuilderConfig, task: string) => void
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const METHOD_COLORS: Record<string, string> = {
  GET: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  POST: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  PUT: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  PATCH: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  DELETE: 'bg-red-500/20 text-red-400 border-red-500/30',
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function MCPBuilderDialog({
  productId,
  productName,
  onClose,
  onBuild,
}: MCPBuilderDialogProps) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [endpoints, setEndpoints] = useState<EndpointInfo[]>([])
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [language, setLanguage] = useState<'typescript' | 'python'>('typescript')
  const [transport, setTransport] = useState<'stdio' | 'http'>('stdio')

  // Fetch endpoints on mount
  useEffect(() => {
    let cancelled = false
    const discover = async () => {
      setLoading(true)
      setError(null)
      try {
        const response = await fetch('/api/v1/agent/mcp-builder/discover', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ product_id: productId }),
        })
        if (!response.ok) {
          const errData = await response.json().catch(() => ({ detail: 'Discovery failed' }))
          throw new Error(errData.detail || 'Failed to discover endpoints')
        }
        const data = await response.json()
        if (!cancelled) {
          setEndpoints(data.endpoints || [])
          // Select all by default
          setSelected(new Set((data.endpoints || []).map((_: EndpointInfo, i: number) => i)))
        }
      } catch (err: any) {
        if (!cancelled) setError(err.message || 'Failed to discover endpoints')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    discover()
    return () => { cancelled = true }
  }, [productId])

  const toggleEndpoint = useCallback((idx: number) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx)
      else next.add(idx)
      return next
    })
  }, [])

  const selectAll = useCallback(() => {
    setSelected(new Set(endpoints.map((_, i) => i)))
  }, [endpoints])

  const selectNone = useCallback(() => {
    setSelected(new Set())
  }, [])

  const handleBuild = useCallback(() => {
    if (selected.size === 0) {
      toast.error('Select at least one endpoint')
      return
    }
    const selectedEndpoints = endpoints.filter((_, i) => selected.has(i))
    const config: MCPBuilderConfig = {
      language,
      transport,
      selected_endpoints: selectedEndpoints,
    }
    onBuild(config, `Build the MCP server for ${productName} with the selected tools.`)
  }, [selected, endpoints, language, transport, productName, onBuild])

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
        onClick={onClose}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 10 }}
          transition={{ type: 'spring', damping: 25, stiffness: 300 }}
          className="w-full max-w-2xl max-h-[85vh] bg-rt-bg-light border border-rt-border rounded-2xl shadow-2xl flex flex-col overflow-hidden"
          onClick={e => e.stopPropagation()}
        >
          {/* Header */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-rt-border">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-violet-500/20 to-cyan-500/20 flex items-center justify-center">
                <Server className="w-4.5 h-4.5 text-violet-400" />
              </div>
              <div>
                <h2 className="text-lg font-semibold">Build MCP Server</h2>
                <p className="text-xs text-rt-text-muted">for {productName}</p>
              </div>
            </div>
            <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-rt-surface transition-colors text-rt-text-muted hover:text-rt-text">
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto px-6 py-4 space-y-5">
            {loading ? (
              <div className="flex flex-col items-center justify-center py-16 gap-3">
                <Loader2 className="w-8 h-8 text-violet-400 animate-spin" />
                <p className="text-sm text-rt-text-muted">Discovering API endpoints from knowledge base...</p>
              </div>
            ) : error ? (
              <div className="flex flex-col items-center justify-center py-16 gap-3">
                <p className="text-sm text-red-400">{error}</p>
                <button onClick={onClose} className="text-sm text-rt-primary hover:underline">Close</button>
              </div>
            ) : (
              <>
                {/* Endpoints */}
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <h3 className="text-sm font-medium text-rt-text">
                      API Endpoints
                      <span className="ml-2 text-xs text-rt-text-muted">({endpoints.length} found)</span>
                    </h3>
                    <div className="flex gap-2 text-xs">
                      <button onClick={selectAll} className="text-rt-primary hover:underline">Select all</button>
                      <span className="text-rt-text-muted">·</span>
                      <button onClick={selectNone} className="text-rt-text-muted hover:text-rt-text hover:underline">None</button>
                    </div>
                  </div>

                  {endpoints.length === 0 ? (
                    <div className="p-4 rounded-lg border border-rt-border bg-rt-surface text-center">
                      <p className="text-sm text-rt-text-muted">No API endpoints found in the knowledge base.</p>
                      <p className="text-xs text-rt-text-muted mt-1">
                        Try training the product with API docs, OpenAPI specs, or source code with route definitions.
                      </p>
                    </div>
                  ) : (
                    <div className="space-y-1 max-h-[280px] overflow-y-auto pr-1">
                      {endpoints.map((ep, idx) => (
                        <label
                          key={idx}
                          className={`flex items-start gap-3 px-3 py-2.5 rounded-lg border cursor-pointer transition-all ${
                            selected.has(idx)
                              ? 'border-violet-500/40 bg-violet-500/5'
                              : 'border-rt-border hover:border-rt-text-muted bg-rt-surface/50'
                          }`}
                        >
                          <input
                            type="checkbox"
                            checked={selected.has(idx)}
                            onChange={() => toggleEndpoint(idx)}
                            className="mt-0.5 accent-violet-500"
                          />
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2">
                              <span className={`text-[10px] font-mono font-bold px-1.5 py-0.5 rounded border ${METHOD_COLORS[ep.method] || 'bg-gray-500/20 text-gray-400 border-gray-500/30'}`}>
                                {ep.method}
                              </span>
                              <span className="text-sm font-mono text-rt-text truncate">{ep.path}</span>
                            </div>
                            <p className="text-xs text-rt-text-muted mt-0.5 line-clamp-1">{ep.description}</p>
                            <p className="text-[10px] text-rt-text-muted/60 font-mono mt-0.5">{ep.suggested_tool_name}</p>
                          </div>
                        </label>
                      ))}
                    </div>
                  )}
                </div>

                {/* Language */}
                <div>
                  <h3 className="text-sm font-medium text-rt-text mb-2">Language</h3>
                  <div className="grid grid-cols-2 gap-2">
                    <button
                      onClick={() => setLanguage('typescript')}
                      className={`flex items-center gap-2 px-4 py-3 rounded-lg border transition-all ${
                        language === 'typescript'
                          ? 'border-violet-500/40 bg-violet-500/10 text-violet-300'
                          : 'border-rt-border hover:border-rt-text-muted text-rt-text-muted'
                      }`}
                    >
                      <Code2 className="w-4 h-4" />
                      <div className="text-left">
                        <p className="text-sm font-medium">TypeScript</p>
                        <p className="text-[10px] opacity-60">Recommended</p>
                      </div>
                      {language === 'typescript' && <Check className="w-4 h-4 ml-auto" />}
                    </button>
                    <button
                      onClick={() => setLanguage('python')}
                      className={`flex items-center gap-2 px-4 py-3 rounded-lg border transition-all ${
                        language === 'python'
                          ? 'border-violet-500/40 bg-violet-500/10 text-violet-300'
                          : 'border-rt-border hover:border-rt-text-muted text-rt-text-muted'
                      }`}
                    >
                      <Code2 className="w-4 h-4" />
                      <div className="text-left">
                        <p className="text-sm font-medium">Python</p>
                        <p className="text-[10px] opacity-60">FastMCP</p>
                      </div>
                      {language === 'python' && <Check className="w-4 h-4 ml-auto" />}
                    </button>
                  </div>
                </div>

                {/* Transport */}
                <div>
                  <h3 className="text-sm font-medium text-rt-text mb-2">Transport</h3>
                  <div className="grid grid-cols-2 gap-2">
                    <button
                      onClick={() => setTransport('stdio')}
                      className={`flex items-center gap-2 px-4 py-3 rounded-lg border transition-all ${
                        transport === 'stdio'
                          ? 'border-violet-500/40 bg-violet-500/10 text-violet-300'
                          : 'border-rt-border hover:border-rt-text-muted text-rt-text-muted'
                      }`}
                    >
                      <HardDrive className="w-4 h-4" />
                      <div className="text-left">
                        <p className="text-sm font-medium">Local (stdio)</p>
                        <p className="text-[10px] opacity-60">Runs on your machine</p>
                      </div>
                      {transport === 'stdio' && <Check className="w-4 h-4 ml-auto" />}
                    </button>
                    <button
                      onClick={() => setTransport('http')}
                      className={`flex items-center gap-2 px-4 py-3 rounded-lg border transition-all ${
                        transport === 'http'
                          ? 'border-violet-500/40 bg-violet-500/10 text-violet-300'
                          : 'border-rt-border hover:border-rt-text-muted text-rt-text-muted'
                      }`}
                    >
                      <Wifi className="w-4 h-4" />
                      <div className="text-left">
                        <p className="text-sm font-medium">Remote (HTTP)</p>
                        <p className="text-[10px] opacity-60">Hosted server</p>
                      </div>
                      {transport === 'http' && <Check className="w-4 h-4 ml-auto" />}
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>

          {/* Footer */}
          {!loading && !error && (
            <div className="px-6 py-4 border-t border-rt-border flex items-center justify-between">
              <p className="text-xs text-rt-text-muted">
                {selected.size} of {endpoints.length} tools selected
              </p>
              <div className="flex gap-2">
                <button
                  onClick={onClose}
                  className="px-4 py-2 rounded-lg border border-rt-border text-sm text-rt-text-muted hover:text-rt-text hover:border-rt-text-muted transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleBuild}
                  disabled={selected.size === 0}
                  className="px-5 py-2 rounded-lg bg-gradient-to-r from-violet-600 to-cyan-600 text-white text-sm font-medium hover:from-violet-500 hover:to-cyan-500 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Build MCP Server ({selected.size} tools)
                </button>
              </div>
            </div>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}
