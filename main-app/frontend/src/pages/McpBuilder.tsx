import { useState, useEffect, useCallback } from 'react'
import { FolderTree, Blocks, AlertCircle, Copy, CheckCircle2, ChevronDown, Wand2, Search, Terminal, Globe, Upload, Trash2, Circle } from 'lucide-react'

interface Product {
  product_id: string
  product_name: string
}

interface EndpointParam {
  name: string
  type: string
  required: boolean
  location: string // body, query, path, header
}

interface EndpointInfo {
  id: string // Add a unique id for state management
  method: string
  path: string
  description: string
  suggested_tool_name: string
  read_only: boolean
  parameters: EndpointParam[]
  checked: boolean // UI state
}

interface McpServerEntry {
  server_id: string
  name: string
  product_name: string
  destination_folder: string
  module_name: string
  mcp_config_json: any
  quick_start_commands: string
  source_type: string
  api_docs_url?: string
  api_base_url?: string
  auth_type?: string
  kb_product_id?: string
  selected_endpoints_json?: any[]
  status: string // running | stopped | error
  created_at: string
}

interface BuildResult {
  status: 'success' | 'error'
  message?: string
  mcpServers?: any
  quickStartCommands?: string
}

export default function McpBuilder() {
  const [products, setProducts] = useState<Product[]>([])
  const [selectedProductId, setSelectedProductId] = useState<string>('')
  
  const [endpoints, setEndpoints] = useState<EndpointInfo[] | null>(null)
  const [isAnalyzing, setIsAnalyzing] = useState(false)
  const [analyzeError, setAnalyzeError] = useState('')

  const [destinationFolder, setDestinationFolder] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [result, setResult] = useState<BuildResult | null>(null)
  const [copied, setCopied] = useState(false)
  const [copiedCmd, setCopiedCmd] = useState(false)

  // SSE streaming state
  const [generationPhase, setGenerationPhase] = useState('')
  const [currentIteration, setCurrentIteration] = useState(0)
  const [generationLogs, setGenerationLogs] = useState<string[]>([])

  // External API mode
  const [sourceMode, setSourceMode] = useState<'internal' | 'external'>('internal')
  const [externalApiName, setExternalApiName] = useState('')
  const [externalSourceType, setExternalSourceType] = useState<'url' | 'text'>('url')
  const [apiDocsUrl, setApiDocsUrl] = useState('')
  const [apiDocsText, setApiDocsText] = useState('')
  
  // AI-Detected Configuration (read-only)
  const [detectedBaseUrl, setDetectedBaseUrl] = useState<string | null>(null)
  const [detectedAuthType, setDetectedAuthType] = useState<string | null>(null)
  const [detectedAuthDetails, setDetectedAuthDetails] = useState<string | null>(null)
  const [kbProductId, setKbProductId] = useState<string | null>(null)
  const [pagesCrawled, setPagesCrawled] = useState<number>(0)
  const [analyzePhase, setAnalyzePhase] = useState<string>('')
  // MCP Server registry
  const [mcpServers, setMcpServers] = useState<McpServerEntry[]>([])
  const [copiedServerId, setCopiedServerId] = useState<string | null>(null)

  const fetchServers = useCallback(async () => {
    try {
      const token = localStorage.getItem('token')
      const res = await fetch('/api/v1/mcp-builder/servers', {
        headers: { 'Authorization': `Bearer ${token}` }
      })
      if (res.ok) {
        setMcpServers(await res.json())
      }
    } catch (e) {
      console.error("Failed to load MCP servers", e)
    }
  }, [])

  // Fetch servers on mount + poll every 30s
  useEffect(() => {
    fetchServers()
    const interval = setInterval(fetchServers, 3000)
    return () => clearInterval(interval)
  }, [fetchServers])

  const [isUploadingFile, setIsUploadingFile] = useState(false)
  const [uploadedFile, setUploadedFile] = useState<{
    name: string
    pages: number
    method: string
    text: string
  } | null>(null)

  // Fetch products on mount
  useEffect(() => {
    async function fetchProducts() {
      try {
        const token = localStorage.getItem('token')
        const res = await fetch('/api/v1/products', {
          headers: { 'Authorization': `Bearer ${token}` }
        })
        if (res.ok) {
          const data = await res.json()
          setProducts(data)
          if (data.length > 0) setSelectedProductId(data[0].product_id)
        }
      } catch (e) {
        console.error("Failed to load products", e)
      }
    }
    fetchProducts()
  }, [])

  const handleCopy = async () => {
    if (!result?.mcpServers) return
    await navigator.clipboard.writeText(JSON.stringify({ mcpServers: result.mcpServers }, null, 2))
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleCopyCmd = async () => {
    if (!result?.quickStartCommands) return
    await navigator.clipboard.writeText(result.quickStartCommands)
    setCopiedCmd(true)
    setTimeout(() => setCopiedCmd(false), 2000)
  }

  const handleAnalyze = async () => {
    if (sourceMode === 'internal' && !selectedProductId) return
    if (sourceMode === 'external') {
      if (!externalApiName) return
      if (externalSourceType === 'url' && !apiDocsUrl) return
      if (externalSourceType === 'text' && !apiDocsText && !uploadedFile) return
    }

    setIsAnalyzing(true)
    setAnalyzeError('')
    setEndpoints(null)
    setDetectedBaseUrl(null)
    setDetectedAuthType(null)
    setPagesCrawled(0)
    setAnalyzePhase(sourceMode === 'internal' ? 'Scanning Knowledge Base...' : 'Fetching documentation...')
    setResult(null)

    // Show crawling phase after a delay for external URL mode
    let phaseTimer: ReturnType<typeof setTimeout> | null = null
    if (sourceMode === 'external' && externalSourceType === 'url') {
      phaseTimer = setTimeout(() => setAnalyzePhase('Crawling linked documentation pages...'), 3000)
    }

    try {
      const token = localStorage.getItem('token')

      // Use uploaded file text if available, otherwise use typed text
      const effectiveDocsText = uploadedFile ? uploadedFile.text : apiDocsText

      if (sourceMode === 'internal') {
        // Internal product — simple JSON request
        const response = await fetch('/api/v1/mcp-builder/discover', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
          body: JSON.stringify({ product_id: selectedProductId })
        })
        if (!response.ok) {
          const err = await response.json()
          throw new Error(err.detail || 'Failed to analyze API')
        }
        const data = await response.json()
        if (phaseTimer) clearTimeout(phaseTimer)
        const mapped = (data.endpoints || []).map((ep: any, i: number) => ({
          ...ep, id: `${ep.method}-${ep.path}-${i}`, checked: true
        }))
        setEndpoints(mapped)
        if (mapped.length === 0) {
          setAnalyzeError('No API endpoints were discovered.')
        }
      } else {
        // External API — Two-step flow:
        // Step 1: If KB not populated yet, agent crawls and stores docs in KB
        // Step 2: Analyze KB for endpoints (same as internal products)

        let effectiveKbId = kbProductId
        if (!effectiveKbId) {
          // STEP 1: Fast crawl + store in KB
          setAnalyzePhase('Fetching documentation...')
          const body = {
            api_name: externalApiName,
            ...(externalSourceType === 'url' && !uploadedFile
              ? { api_docs_url: apiDocsUrl }
              : { api_docs_text: effectiveDocsText }
            )
          }
          const response = await fetch('/api/v1/mcp-builder/discover-agent', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(body)
          })

          if (!response.ok) {
            const err = await response.json()
            throw new Error(err.detail || 'Failed to fetch documentation')
          }

          const crawlData = await response.json()
          effectiveKbId = crawlData.kb_product_id
          setKbProductId(effectiveKbId)
          setAnalyzePhase(`Fetched ${crawlData.pages_stored} pages. Analyzing endpoints...`)
        } else {
          setAnalyzePhase('Analyzing KB for endpoints...')
        }

        // STEP 2: Analyze KB for endpoints (same flow as internal products)
        const kbId = effectiveKbId!
        const analyzeResponse = await fetch('/api/v1/mcp-builder/analyze-kb', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
          body: JSON.stringify({ api_name: externalApiName, kb_product_id: kbId })
        })

        if (!analyzeResponse.ok) {
          const err = await analyzeResponse.json()
          throw new Error(err.detail || 'Failed to analyze KB')
        }

        const data = await analyzeResponse.json()
        if (phaseTimer) clearTimeout(phaseTimer)
        setDetectedBaseUrl(data.base_url || null)
        setDetectedAuthType(data.auth_type || 'none')
        setDetectedAuthDetails(data.auth_details || null)
        setKbProductId(data.kb_product_id || kbId)
        setPagesCrawled(data.endpoints?.length || 0)

        const mapped = (data.endpoints || []).map((ep: any, i: number) => ({
          ...ep, id: `${ep.method}-${ep.path}-${i}`, checked: true
        }))
        setEndpoints(mapped)
        if (mapped.length === 0) {
          setAnalyzeError('No API endpoints were discovered. Try a different documentation URL.')
        }
      }
    } catch (err: any) {
      if (phaseTimer) clearTimeout(phaseTimer)
      setAnalyzeError(err.message)
    } finally {
      setIsAnalyzing(false)
      setAnalyzePhase('')
    }
  }

  const toggleEndpoint = (id: string) => {
    if (!endpoints) return
    setEndpoints(endpoints.map(ep => ep.id === id ? { ...ep, checked: !ep.checked } : ep))
  }

  const handleGenerate = async () => {
    // Get checked endpoints
    const selectedEndpoints = (endpoints || []).filter(ep => ep.checked)
    if (selectedEndpoints.length === 0) {
      alert("Please select at least one capability to generate a server.")
      return
    }

    setIsGenerating(true)
    setResult(null)
    setGenerationPhase('')
    setCurrentIteration(0)
    setGenerationLogs([])

    const isExternal = sourceMode === 'external'
    const prod = products.find(p => p.product_id === selectedProductId)
    const prodName = isExternal ? externalApiName : (prod ? prod.product_name : 'Unknown Product')
    const safeName = prodName.toLowerCase().replace(/[^a-z0-9]/g, '_')

    try {
      const token = localStorage.getItem('token')

      const payload: any = {
        product_name: prodName,
        selected_endpoints: selectedEndpoints,
        destination_folder: destinationFolder || `~/mcp-servers/${safeName}`
      }

      if (sourceMode === 'external') {
        payload.api_name = externalApiName
        if (uploadedFile) {
          payload.api_docs_text = uploadedFile.text
        } else if (externalSourceType === 'url') {
          payload.api_docs_url = apiDocsUrl
        } else {
          payload.api_docs_text = apiDocsText
        }
        payload.api_base_url = detectedBaseUrl
        payload.auth_type = detectedAuthType
        payload.auth_details = detectedAuthDetails
        payload.kb_product_id = kbProductId
      } else {
        payload.product_id = selectedProductId
      }

      const response = await fetch('/api/v1/mcp-builder/generate', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify(payload)
      })

      if (!response.ok) {
        throw new Error('Failed to generate MCP server')
      }

      // Parse SSE stream for real-time progress
      const reader = response.body?.getReader()
      if (!reader) throw new Error('No response stream')

      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n\n')
        buffer = lines.pop() || ''

        for (const block of lines) {
          if (!block.trim()) continue
          const eventMatch = block.match(/^event: (.+)$/m)
          const dataMatch = block.match(/^data: (.+)$/m)
          if (!eventMatch || !dataMatch) continue

          const eventType = eventMatch[1]
          let data: any
          try { data = JSON.parse(dataMatch[1]) } catch { continue }

          switch (eventType) {
            case 'agent_status':
              setGenerationPhase(data.message || data.step || '')
              break
            case 'agent_code':
              setCurrentIteration(data.iteration || 0)
              if (data.code) {
                // Extract file write operations from code for the log
                const writeMatches = data.code.match(/write_file\s*\(\s*["']([^"']+)["']/g)
                if (writeMatches) {
                  const files = writeMatches.map((m: string) => m.match(/["']([^"']+)["']/)?.[1]).filter(Boolean)
                  setGenerationLogs(prev => [...prev, ...files.map((f: string) => `Writing ${f}`)])
                }
              }
              break
            case 'tool_output':
              if (data.output) {
                const short = data.output.slice(0, 120).replace(/\n/g, ' ')
                setGenerationLogs(prev => [...prev.slice(-19), short])
              }
              break
            case 'mcp_result':
              setResult({
                status: data.status || 'success',
                message: data.message,
                mcpServers: data.mcpServers,
                quickStartCommands: data.quickStartCommands
              })
              fetchServers() // Refresh server list
              break
            case 'agent_error':
              setResult({
                status: 'error',
                message: data.detail || 'Generation failed'
              })
              break
          }
        }
      }

    } catch (err: any) {
      setResult({
        status: 'error',
        message: err.message || 'An error occurred during generation.'
      })
    } finally {
      setIsGenerating(false)
    }
  }

  // Helper colors for HTTP methods
  const getMethodColor = (method: string) => {
    const m = method.toUpperCase()
    if (m === 'GET') return 'text-blue-500 bg-blue-500/10 border-blue-500/20'
    if (m === 'POST') return 'text-green-500 bg-green-500/10 border-green-500/20'
    if (m === 'PUT' || m === 'PATCH') return 'text-orange-500 bg-orange-500/10 border-orange-500/20'
    if (m === 'DELETE') return 'text-red-500 bg-red-500/10 border-red-500/20'
    return 'text-gray-400 bg-gray-500/10 border-gray-500/20'
  }

  return (
    <div className="flex-1 p-8 overflow-y-auto">
      <div className="max-w-5xl mx-auto space-y-8">
        <div>
          <h1 className="text-3xl font-display font-semibold text-rt-text-strong bg-clip-text text-transparent bg-gradient-to-r from-rt-text-strong to-rt-text flex items-center gap-3">
            <Wand2 className="w-8 h-8 text-rt-primary" />
            Auto-Discovery MCP Builder
          </h1>
          <p className="mt-2 text-rt-text-muted text-lg max-w-3xl">
            Retrace will analyze the Knowledge Base of your product to automatically identify API capabilities, allowing you to instantly generate an MCP Server with zero configuration.
          </p>
        </div>

        {/* Built MCP Servers */}
        {mcpServers.length > 0 && (
          <div className="p-4 bg-rt-surface border border-rt-border/50 shadow-sm rounded-2xl">
            <h2 className="text-sm font-medium text-rt-text-strong mb-3 flex items-center gap-2">
              <Blocks className="w-4 h-4 text-rt-primary" />
              Built MCP Servers
              <span className="text-xs text-rt-text-muted font-normal">({mcpServers.length})</span>
            </h2>
            <div className="space-y-1.5">
              {mcpServers.map(srv => (
                <div key={srv.server_id} className="flex items-center justify-between px-3 py-2 bg-rt-bg rounded-lg border border-rt-border/30 hover:border-rt-border/60 transition-all group">
                  <div
                    className="flex items-center gap-3 min-w-0 cursor-pointer flex-1"
                    onClick={() => {
                      // Pre-fill the builder form with this server's settings
                      setSourceMode(srv.source_type === 'internal' ? 'internal' : 'external')
                      if (srv.source_type !== 'internal') {
                        setExternalApiName(srv.product_name)
                        if (srv.api_docs_url) {
                          setExternalSourceType('url')
                          setApiDocsUrl(srv.api_docs_url)
                        }
                        setDetectedBaseUrl(srv.api_base_url || null)
                        setDetectedAuthType(srv.auth_type || null)
                        setKbProductId(srv.kb_product_id || null)
                      }
                      setDestinationFolder(srv.destination_folder)
                      // Load previously selected endpoints
                      if (srv.selected_endpoints_json) {
                        setEndpoints(srv.selected_endpoints_json.map((ep: any, i: number) => ({
                          ...ep,
                          id: `${ep.method}-${ep.path}-${i}`,
                          checked: true
                        })))
                      }
                    }}
                    title="Click to load settings"
                  >
                    <Circle className={`w-2.5 h-2.5 flex-shrink-0 ${
                      srv.status === 'running' ? 'fill-emerald-400 text-emerald-400' :
                      srv.status === 'error' ? 'fill-red-400 text-red-400' :
                      'fill-gray-400 text-gray-400'
                    }`} />
                    <div className="min-w-0">
                      <span className="text-sm font-medium text-rt-text-strong">{srv.name}</span>
                      <span className="text-xs text-rt-text-muted ml-2">{srv.product_name}</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                      srv.status === 'running' ? 'bg-emerald-500/10 text-emerald-400' :
                      srv.status === 'error' ? 'bg-red-500/10 text-red-400' :
                      'bg-gray-500/10 text-gray-400'
                    }`}>
                      {srv.status}
                    </span>
                    <button
                      onClick={async () => {
                        await navigator.clipboard.writeText(JSON.stringify({ mcpServers: srv.mcp_config_json }, null, 2))
                        setCopiedServerId(srv.server_id)
                        setTimeout(() => setCopiedServerId(null), 2000)
                      }}
                      className="p-1 rounded hover:bg-rt-primary/10 text-rt-text-muted hover:text-rt-primary transition-colors"
                      title="Copy MCP config"
                    >
                      {copiedServerId === srv.server_id ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
                    </button>
                    <button
                      onClick={async () => {
                        const token = localStorage.getItem('token')
                        await fetch(`/api/v1/mcp-builder/servers/${srv.server_id}`, {
                          method: 'DELETE',
                          headers: { 'Authorization': `Bearer ${token}` }
                        })
                        fetchServers()
                      }}
                      className="p-1 rounded hover:bg-red-500/10 text-rt-text-muted hover:text-red-400 transition-colors"
                      title="Remove from list"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
          {/* Left Column: Configuration & Endpoints */}
          <div className="lg:col-span-7 space-y-6">
            
            {/* Step 1: Select & Discovery */}
            <div className="p-6 bg-rt-surface border border-rt-border/50 shadow-sm rounded-2xl">
              <h2 className="text-xl font-medium mb-4 flex items-center justify-between">
                <span className="flex items-center gap-2 text-rt-text-strong">
                  <div className="w-6 h-6 rounded bg-rt-primary/10 text-rt-primary flex items-center justify-center text-sm font-bold">1</div>
                  Select API Source
                </span>
              </h2>

              {/* Source Mode Toggle */}
              <div className="flex gap-1.5 p-1 bg-rt-bg rounded-lg border border-rt-border mb-5">
                <button
                  onClick={() => { setSourceMode('internal'); setEndpoints(null); setResult(null); setAnalyzeError('') }}
                  className={`flex-1 px-3 py-2 rounded-md text-xs font-medium transition-all flex items-center justify-center gap-1.5 ${
                    sourceMode === 'internal'
                      ? 'bg-rt-primary text-white shadow-sm'
                      : 'text-rt-text-muted hover:text-rt-text'
                  }`}
                >
                  <Blocks className="w-3.5 h-3.5" />
                  Internal Product
                </button>
                <button
                  onClick={() => { setSourceMode('external'); setEndpoints(null); setResult(null); setAnalyzeError('') }}
                  className={`flex-1 px-3 py-2 rounded-md text-xs font-medium transition-all flex items-center justify-center gap-1.5 ${
                    sourceMode === 'external'
                      ? 'bg-rt-primary text-white shadow-sm'
                      : 'text-rt-text-muted hover:text-rt-text'
                  }`}
                >
                  <Globe className="w-3.5 h-3.5" />
                  External API
                </button>
              </div>
              
              <div className="space-y-4">
                {sourceMode === 'internal' ? (
                  /* Internal Product Form */
                  <div className="space-y-1.5">
                    <label className="text-sm font-medium text-rt-text-muted">Target Retrace Product</label>
                    <div className="relative">
                      <select
                        value={selectedProductId}
                        onChange={(e) => setSelectedProductId(e.target.value)}
                        className="w-full bg-rt-bg border border-rt-border rounded-lg px-4 py-3 text-sm focus:ring-2 focus:ring-rt-primary/20 focus:border-rt-primary transition-all text-rt-text appearance-none cursor-pointer"
                      >
                        {products.length === 0 && <option value="">Loading products...</option>}
                        {products.map(p => (
                          <option key={p.product_id} value={p.product_id}>{p.product_name}</option>
                        ))}
                      </select>
                      <ChevronDown className="w-4 h-4 text-rt-text-muted absolute right-4 top-1/2 -translate-y-1/2 pointer-events-none" />
                    </div>
                  </div>
                ) : (
                  /* External API Form */
                  <>
                    <div className="space-y-1.5">
                      <label className="text-sm font-medium text-rt-text-muted">API Name</label>
                      <input
                        type="text"
                        placeholder="e.g. Slack, Todoist, Spotify"
                        value={externalApiName}
                        onChange={(e) => setExternalApiName(e.target.value)}
                        className="w-full bg-rt-bg border border-rt-border rounded-lg px-4 py-2.5 text-sm focus:ring-2 focus:ring-rt-primary/20 focus:border-rt-primary transition-all text-rt-text"
                      />
                    </div>
                    <div className="space-y-1.5 mt-4">
                      <label className="text-sm font-medium text-rt-text-muted">Documentation Source</label>
                      
                      <div className="flex gap-1.5 p-1 bg-rt-bg/50 rounded-lg border border-rt-border/50 mb-3">
                        <button
                          onClick={() => setExternalSourceType('url')}
                          className={`flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
                            externalSourceType === 'url' ? 'bg-rt-surface shadow text-rt-text-strong' : 'text-rt-text-muted hover:text-rt-text-strong'
                          }`}
                        >
                          Documentation URL
                        </button>
                        <button
                          onClick={() => setExternalSourceType('text')}
                          className={`flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
                            externalSourceType === 'text' ? 'bg-rt-surface shadow text-rt-text-strong' : 'text-rt-text-muted hover:text-rt-text-strong'
                          }`}
                        >
                          Paste Text / Upload
                        </button>
                      </div>

                      {externalSourceType === 'url' ? (
                        <div className="space-y-1 mt-2">
                          <input
                            type="url"
                            placeholder="https://api.example.com/docs or OpenAPI JSON URL"
                            value={apiDocsUrl}
                            onChange={(e) => setApiDocsUrl(e.target.value)}
                            className="w-full bg-rt-bg border border-rt-border rounded-lg px-4 py-2.5 text-sm focus:ring-2 focus:ring-rt-primary/20 focus:border-rt-primary transition-all text-rt-text font-mono text-xs"
                          />
                          <p className="text-xs text-rt-text-muted leading-relaxed">
                            The AI will download this URL to discover endpoints, detect authentication methods, and find the correct base URL.
                          </p>
                        </div>
                      ) : (
                        <div className="space-y-2 mt-2 animate-fade-in">
                          {uploadedFile ? (
                            /* File uploaded — show clean badge */
                            <div className="bg-rt-bg border border-rt-primary/30 rounded-lg px-4 py-3 flex items-center justify-between">
                              <div className="flex items-center gap-3">
                                <div className="bg-rt-primary/10 p-2 rounded-lg">
                                  <Blocks className="w-4 h-4 text-rt-primary" />
                                </div>
                                <div>
                                  <p className="text-sm font-medium text-rt-text-strong">{uploadedFile.name}</p>
                                  <p className="text-xs text-rt-text-muted">
                                    {uploadedFile.pages} pages • {uploadedFile.text.length.toLocaleString()} chars extracted • {uploadedFile.method}
                                  </p>
                                </div>
                              </div>
                              <button
                                onClick={() => { setUploadedFile(null); setApiDocsText(''); }}
                                className="text-rt-text-muted hover:text-red-400 transition-colors p-1"
                                title="Remove file"
                              >
                                <AlertCircle className="w-4 h-4" />
                              </button>
                            </div>
                          ) : (
                            /* No file — show textarea + upload area */
                            <>
                              <textarea
                                className="w-full bg-rt-bg border border-rt-border rounded-lg px-3 py-2 text-xs focus:ring-2 focus:ring-rt-primary/20 focus:border-rt-primary transition-all text-rt-text font-mono"
                                rows={5}
                                placeholder="Paste raw OpenAPI JSON, YAML, or just raw text copied from a documentation website..."
                                value={apiDocsText}
                                onChange={(e) => setApiDocsText(e.target.value)}
                              />
                              <div className="relative group">
                                <input
                                  type="file"
                                  className="absolute inset-0 w-full h-full opacity-0 cursor-pointer z-10"
                                  accept=".json,.yaml,.yml,.txt,.md,.pdf,.docx,.doc"
                                  onChange={async (e) => {
                                    const file = e.target.files?.[0];
                                    if (!file) return;
                                    e.target.value = '';

                                    const ext = file.name.split('.').pop()?.toLowerCase() || '';
                                    const binaryTypes = ['pdf', 'docx', 'doc'];

                                    if (binaryTypes.includes(ext)) {
                                      // Binary files → upload to backend for extraction
                                      setIsUploadingFile(true);
                                      try {
                                        const token = localStorage.getItem('token');
                                        const formData = new FormData();
                                        formData.append('file', file);
                                        const resp = await fetch('/api/v1/mcp-builder/upload-docs', {
                                          method: 'POST',
                                          headers: { 'Authorization': `Bearer ${token}` },
                                          body: formData
                                        });
                                        if (!resp.ok) {
                                          const err = await resp.json();
                                          throw new Error(err.detail || 'Upload failed');
                                        }
                                        const data = await resp.json();
                                        setUploadedFile({
                                          name: file.name,
                                          pages: data.pages,
                                          method: data.method,
                                          text: data.text,
                                        });
                                        setApiDocsText(''); // Clear textarea
                                      } catch (err: any) {
                                        setAnalyzeError(`File upload failed: ${err.message}`);
                                      } finally {
                                        setIsUploadingFile(false);
                                      }
                                    } else {
                                      // Text files → read directly and show as uploaded file
                                      const reader = new FileReader();
                                      reader.onload = (ev) => {
                                        const text = ev.target?.result?.toString() || '';
                                        setUploadedFile({
                                          name: file.name,
                                          pages: 1,
                                          method: 'text',
                                          text,
                                        });
                                        setApiDocsText(''); // Clear textarea
                                      };
                                      reader.readAsText(file);
                                    }
                                  }}
                                />
                                <div className="w-full bg-rt-bg/30 border border-rt-border/50 border-dashed rounded-lg px-3 py-3 text-xs text-center text-rt-text-muted group-hover:bg-rt-primary/5 group-hover:border-rt-primary/30 group-hover:text-rt-primary transition-all">
                                  {isUploadingFile ? (
                                    <div className="flex items-center justify-center gap-2">
                                      <div className="w-3 h-3 border-2 border-rt-primary/30 border-t-rt-primary rounded-full animate-spin" />
                                      <span>Extracting text from document...</span>
                                    </div>
                                  ) : (
                                    <>
                                      <Upload className="w-4 h-4 mx-auto mb-1 opacity-70 group-hover:scale-110 transition-transform" />
                                      Drop a file here (PDF, DOCX, JSON, YAML, TXT) or click to upload
                                    </>
                                  )}
                                </div>
                              </div>
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  </>
                )}

                <div className="pt-2">
                  <button
                    type="button"
                    onClick={handleAnalyze}
                    disabled={isAnalyzing || (sourceMode === 'internal' ? !selectedProductId : !externalApiName || (externalSourceType === 'url' ? !apiDocsUrl : !apiDocsText))}
                    className="w-full h-11 bg-rt-bg border border-rt-primary/30 hover:bg-rt-primary/5 text-rt-primary rounded-lg font-medium shadow-sm disabled:opacity-50 transition-all flex items-center justify-center gap-2 group"
                  >
                    {isAnalyzing ? (
                      <>
                        <div className="w-4 h-4 border-2 border-rt-primary/30 border-t-rt-primary rounded-full animate-spin" />
                        {analyzePhase || 'Analyzing...'}
                      </>
                    ) : (
                      <>
                        <Search className="w-4 h-4 group-hover:scale-110 transition-transform" />
                        {sourceMode === 'external' && !kbProductId ? 'Fetch & Analyze API' : 'Analyze API Capabilities'}
                      </>
                    )}
                  </button>
                </div>
              </div>

              {/* Discovery Error State */}
              {analyzeError && (
                <div className="mt-4 p-4 bg-red-500/5 border border-red-500/20 rounded-xl flex items-start gap-3 text-red-400">
                  <AlertCircle className="w-5 h-5 flex-shrink-0 mt-0.5" />
                  <p className="text-sm leading-relaxed">{analyzeError}</p>
                </div>
              )}
            </div>

            {/* Step 2: Capability Selection */}
            {endpoints && (
              <div className="p-6 bg-rt-surface border border-rt-border/50 shadow-sm rounded-2xl animate-fade-in flex flex-col h-full">
                <h2 className="text-xl font-medium mb-2 flex items-center justify-between">
                  <span className="flex items-center gap-2 text-rt-text-strong">
                    <div className="w-6 h-6 rounded bg-rt-primary/10 text-rt-primary flex items-center justify-center text-sm font-bold">2</div>
                    Select Server Capabilities
                  </span>
                  <span className="text-sm text-rt-text-muted font-normal bg-rt-bg px-3 py-1 rounded-full border border-rt-border">
                    {endpoints.filter(e => e.checked).length} of {endpoints.length} tools selected
                  </span>
                </h2>

                {/* AI Detection Badges for External APIs */}
                {sourceMode === 'external' && (
                  <div className="mb-4 flex flex-wrap gap-2 items-center text-xs text-rt-text-muted">
                    <span className="flex items-center gap-1.5 bg-rt-primary/10 text-rt-primary px-2 py-1 rounded-md border border-rt-primary/20 border-xs">
                      <Wand2 className="w-3 h-3" /> AI Detected Config
                    </span>
                    {detectedBaseUrl && (
                      <span className="bg-rt-surface px-2 py-1 rounded-md border border-rt-border/50 font-mono truncate max-w-[200px]">
                        URL: {detectedBaseUrl}
                      </span>
                    )}
                    {detectedAuthType && (
                      <span className="bg-rt-surface px-2 py-1 rounded-md border border-rt-border/50 uppercase">
                        Auth: {detectedAuthType}
                      </span>
                    )}
                    {pagesCrawled > 1 && (
                      <span className="bg-emerald-500/10 text-emerald-400 px-2 py-1 rounded-md border border-emerald-500/20">
                        Crawled {pagesCrawled} pages
                      </span>
                    )}
                  </div>
                )}
                
                <p className="text-xs text-rt-text-muted mb-4 flex-shrink-0">
                    Uncheck the tools you do not wish to expose to the LLM.
                  </p>
                  
                  <div className="flex-1 overflow-y-auto pr-2 space-y-2.5 min-h-0 custom-scrollbar pb-4">
                    {endpoints.map(ep => (
                      <div 
                        key={ep.id} 
                        onClick={() => toggleEndpoint(ep.id)}
                        className={`p-3.5 rounded-xl border cursor-pointer transition-all flex items-start gap-3 hover:border-rt-primary/50 ${ep.checked ? 'bg-rt-primary/5 border-rt-primary/30' : 'bg-rt-bg border-rt-border opacity-60 hover:opacity-100'}`}
                      >
                        <div className="pt-0.5 flex-shrink-0">
                          <input 
                            type="checkbox" 
                            checked={ep.checked} 
                            readOnly
                            className="w-4 h-4 rounded text-rt-primary border-rt-border focus:ring-rt-primary/20 accent-rt-primary"
                          />
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                            <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded border ${getMethodColor(ep.method)}`}>
                              {ep.method.toUpperCase()}
                            </span>
                            <span className="font-mono text-xs text-rt-text-strong truncate">{ep.path}</span>
                          </div>
                          <p className="text-[13px] text-rt-text-muted leading-snug line-clamp-2">
                            {ep.description}
                          </p>
                          <div className="mt-2 text-[10px] text-rt-text-muted/70 font-mono">
                            Tool Name: <span className="text-rt-primary/80">{ep.suggested_tool_name}</span>
                          </div>
                          {ep.parameters && ep.parameters.length > 0 && (
                            <div className="mt-1.5 flex flex-wrap gap-1">
                              {ep.parameters.slice(0, 6).map((p, i) => (
                                <span key={i} className={`text-[9px] px-1.5 py-0.5 rounded border font-mono ${p.required ? 'text-orange-400 bg-orange-500/5 border-orange-500/20' : 'text-rt-text-muted/60 bg-rt-bg border-rt-border/50'}`}>
                                  {p.name}{p.required ? '*' : ''}
                                </span>
                              ))}
                              {ep.parameters.length > 6 && (
                                <span className="text-[9px] text-rt-text-muted/50">+{ep.parameters.length - 6} more</span>
                              )}
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
              </div>
            )}
          </div>

          {/* Right Column: Generation Output */}
          <div className="lg:col-span-5 space-y-6">
            
            {/* Step 3: Destination & Build Button (Only show if endpoints are loaded) */}
            <div className={`p-6 bg-rt-surface border border-rt-border/50 shadow-sm rounded-2xl transition-all ${endpoints ? 'opacity-100' : 'opacity-40 pointer-events-none'}`}>
              <h2 className="text-xl font-medium mb-6 flex items-center justify-between">
                <span className="flex items-center gap-2 text-rt-text-strong">
                  <div className="w-6 h-6 rounded bg-rt-primary/10 text-rt-primary flex items-center justify-center text-sm font-bold">3</div>
                  Deploy Server
                </span>
              </h2>

              <div className="space-y-5">
                <div className="space-y-1.5">
                  <label className="text-sm font-medium text-rt-text-muted flex justify-between">
                    <span>File Destination</span>
                  </label>
                  <div className="relative">
                    <FolderTree className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted/50" />
                    <input
                      type="text"
                      placeholder="e.g. ~/mcp-servers/project"
                      value={destinationFolder}
                      onChange={(e) => setDestinationFolder(e.target.value)}
                      className="w-full bg-rt-bg border border-rt-border rounded-lg pl-9 pr-4 py-2.5 text-sm focus:ring-2 focus:ring-rt-primary/20 focus:border-rt-primary transition-all text-rt-text font-mono text-xs"
                    />
                  </div>
                  <p className="text-[11px] text-rt-text-muted/60 mt-1">If blank, creates a default folder.</p>
                </div>

                <div className="pt-2">
                  <button
                    type="button"
                    onClick={handleGenerate}
                    disabled={isGenerating || !endpoints || endpoints.filter(e => e.checked).length === 0}
                    className="w-full h-12 bg-rt-primary hover:bg-rt-primary-dark text-white rounded-lg font-medium shadow-md shadow-rt-primary/20 disabled:opacity-50 transition-all flex items-center justify-center gap-2"
                  >
                    {isGenerating ? (
                      <>
                        <div className="w-5 h-5 border-2 border-white/30 border-t-white/80 rounded-full animate-spin" />
                        Writing Code...
                      </>
                    ) : (
                      <>
                        <Blocks className="w-5 h-5" />
                        Generate MCP Codebase
                      </>
                    )}
                  </button>
                </div>
              </div>
            </div>

            {/* Results Display */}
            <div className="space-y-6">
              {isGenerating && (
                <div className="p-6 bg-rt-primary/10 border border-rt-primary/20 border-dashed rounded-2xl space-y-4">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 bg-rt-primary/20 rounded-full flex items-center justify-center flex-shrink-0">
                      <Blocks className="w-5 h-5 text-rt-primary animate-bounce" />
                    </div>
                    <div>
                      <h3 className="text-sm font-medium text-rt-text-strong">AI agent is writing the codebase...</h3>
                      <p className="text-xs text-rt-text-muted mt-0.5">
                        {generationPhase || 'Initializing...'}
                        {currentIteration > 0 && <span className="ml-2 text-rt-primary/70">Step {currentIteration}</span>}
                      </p>
                    </div>
                  </div>
                  {generationLogs.length > 0 && (
                    <div className="bg-[#111] rounded-xl border border-rt-border p-3 max-h-[200px] overflow-y-auto custom-scrollbar">
                      {generationLogs.slice(-10).map((log, i) => (
                        <div key={i} className="text-[11px] font-mono text-rt-text-muted/70 py-0.5 flex items-start gap-2">
                          <span className="text-rt-primary/50 flex-shrink-0">{'>'}</span>
                          <span className="truncate">{log}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {(result?.status === 'success' || result?.status === 'warning') && (
                <div className="p-6 bg-rt-success/5 border border-rt-success/20 rounded-2xl space-y-4 shadow-sm animate-in fade-in slide-in-from-top-4">
                  <div className="flex items-start gap-4 text-rt-success">
                    <div className="bg-rt-success/10 p-2 rounded-full mt-0.5">
                      <CheckCircle2 className="w-6 h-6" />
                    </div>
                    <div>
                      <h3 className="text-lg font-medium text-rt-success">Server Successfully Built</h3>
                      <p className="text-sm text-rt-text-muted mt-1">
                        {result?.status === 'warning'
                          ? result.message || 'Generation completed with warnings. The server may need manual fixes.'
                          : 'We wrote your new MCP codebase into the destination folder. Copy the configuration block below to install it into Claude Desktop or Cursor.'
                        }
                      </p>
                    </div>
                  </div>
                  
                  <div className="relative group mt-4">
                    <pre className="bg-[#111] p-4 rounded-xl text-xs font-mono text-[#43ff64d9] overflow-x-auto border border-rt-border shadow-inner">
                      {JSON.stringify({ mcpServers: result.mcpServers }, null, 2)}
                    </pre>
                    <button
                      onClick={handleCopy}
                      className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity bg-rt-surface border border-rt-border rounded-md p-2 shadow-lg hover:bg-rt-bg text-rt-text"
                    >
                      {copied ? <CheckCircle2 className="w-4 h-4 text-rt-success" /> : <Copy className="w-4 h-4" />}
                    </button>
                  </div>

                  {result.quickStartCommands && (
                    <div className="mt-4">
                      <h4 className="text-sm font-medium text-rt-text-strong mb-2 flex items-center gap-2">
                        <Terminal className="w-4 h-4 text-rt-primary" />
                        Quick Start — Paste in your terminal
                      </h4>
                      <div className="relative group">
                        <pre className="bg-[#111] p-4 rounded-xl text-xs font-mono text-[#60a5fa] overflow-x-auto border border-rt-border shadow-inner whitespace-pre-wrap">
                          {result.quickStartCommands}
                        </pre>
                        <button
                          onClick={handleCopyCmd}
                          className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity bg-rt-surface border border-rt-border rounded-md p-2 shadow-lg hover:bg-rt-bg text-rt-text"
                        >
                          {copiedCmd ? <CheckCircle2 className="w-4 h-4 text-rt-success" /> : <Copy className="w-4 h-4" />}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {result?.status === 'error' && (
                <div className="p-6 bg-red-500/5 border border-red-500/20 rounded-2xl space-y-3 shadow-sm animate-in fade-in">
                  <div className="flex items-center gap-3 text-red-500">
                    <AlertCircle className="w-6 h-6" />
                    <h3 className="text-lg font-medium">Generation Failed</h3>
                  </div>
                  <p className="text-sm text-rt-text-muted">{result.message}</p>
                </div>
              )}

              {!isGenerating && !result && !endpoints && (
                <div className="p-8 border border-rt-border/50 border-dashed rounded-2xl flex flex-col items-center justify-center text-center space-y-4 h-[300px] opacity-40">
                  <Wand2 className="w-8 h-8 text-rt-text-muted" />
                  <div>
                    <h3 className="text-[15px] font-medium text-rt-text-strong">Waiting for analysis</h3>
                    <p className="text-sm text-rt-text-muted mt-1 max-w-[200px] mx-auto">
                      Select a product and analyze it to begin.
                    </p>
                  </div>
                </div>
              )}
            </div>
            
          </div>
        </div>
      </div>
    </div>
  )
}
