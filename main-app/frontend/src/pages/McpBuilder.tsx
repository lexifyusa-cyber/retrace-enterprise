import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { FolderTree, Blocks, AlertCircle, Copy, CheckCircle2, ChevronDown, ChevronUp, Wand2, Search, Terminal, Globe, Upload, Circle, Loader2, ChevronLeft, ChevronRight, Zap, Server, Clock, Check, FileCode, Folder, File, FolderOpen, RotateCcw } from 'lucide-react'
import { localFilesApi } from '../utils/api'
import type { FileEntry } from '../types'

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

// Helper to load persisted MCP Builder state from sessionStorage
const MCP_SESSION_KEY = 'mcp_builder_state'
function loadSessionState(): Record<string, any> {
  try {
    const raw = sessionStorage.getItem(MCP_SESSION_KEY)
    return raw ? JSON.parse(raw) : {}
  } catch { return {} }
}
function saveSessionState(state: Record<string, any>) {
  try { sessionStorage.setItem(MCP_SESSION_KEY, JSON.stringify(state)) } catch {}
}

// Module-level in-flight analysis tracker — survives component unmount/remount
let _inflightAnalysis: {
  promise: Promise<any>
  phase: string
} | null = null

const GEN_SESSION_KEY = 'mcp_builder_generating'

const STEPS = [
  { num: 1, label: 'Source', icon: Globe },
  { num: 2, label: 'Capabilities', icon: Blocks },
  { num: 3, label: 'Deploy', icon: Zap },
] as const

export default function McpBuilder() {
  const _saved = loadSessionState()

  const [products, setProducts] = useState<Product[]>([])
  const [selectedProductId, setSelectedProductId] = useState<string>(_saved.selectedProductId ?? '')

  const [endpoints, setEndpoints] = useState<EndpointInfo[] | null>(_saved.endpoints ?? null)
  const [isAnalyzing, setIsAnalyzing] = useState(false)
  const [analyzeError, setAnalyzeError] = useState(_saved.analyzeError ?? '')

  const [destinationFolder, setDestinationFolder] = useState('')
  const [showFolderPicker, setShowFolderPicker] = useState(false)
  const [pickerPath, setPickerPath] = useState('~')
  const [pickerFiles, setPickerFiles] = useState<FileEntry[]>([])
  const [pickerLoading, setPickerLoading] = useState(false)
  const [isGenerating, setIsGenerating] = useState(false)
  const [result, setResult] = useState<BuildResult | null>(_saved.result ?? null)
  const [copied, setCopied] = useState(false)
  const [copiedCmd, setCopiedCmd] = useState(false)

  // SSE streaming state
  const [generationPhase, setGenerationPhase] = useState(_saved.generationPhase ?? '')
  const [currentIteration, setCurrentIteration] = useState(0)
  const [generationLogs, setGenerationLogs] = useState<string[]>(_saved.generationLogs ?? [])

  // External API mode
  const [sourceMode, setSourceMode] = useState<'internal' | 'external'>(_saved.sourceMode ?? 'internal')
  const [externalApiName, setExternalApiName] = useState(_saved.externalApiName ?? '')
  const [externalSourceType, setExternalSourceType] = useState<'url' | 'text'>(_saved.externalSourceType ?? 'url')
  const [apiDocsUrl, setApiDocsUrl] = useState(_saved.apiDocsUrl ?? '')
  const [apiDocsText, setApiDocsText] = useState(_saved.apiDocsText ?? '')

  // AI-Detected Configuration (read-only)
  const [detectedBaseUrl, setDetectedBaseUrl] = useState<string | null>(_saved.detectedBaseUrl ?? null)
  const [detectedAuthType, setDetectedAuthType] = useState<string | null>(_saved.detectedAuthType ?? null)
  const [detectedAuthDetails, setDetectedAuthDetails] = useState<string | null>(_saved.detectedAuthDetails ?? null)
  const [kbProductId, setKbProductId] = useState<string | null>(null)
  const [pagesCrawled, setPagesCrawled] = useState<number>(_saved.pagesCrawled ?? 0)
  const [analyzePhase, setAnalyzePhase] = useState<string>('')
  // MCP Server registry
  const [mcpServers, setMcpServers] = useState<McpServerEntry[]>([])
  const [copiedServerId, setCopiedServerId] = useState<string | null>(null)

  // Generation progress tracking (survives navigation)
  const [progressExpanded, setProgressExpanded] = useState(true)
  const progressPollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // === NEW UI STATE ===
  const [currentStep, setCurrentStep] = useState<1 | 2 | 3>(_saved.currentStep ?? 1)
  const [endpointSearch, setEndpointSearch] = useState('')
  const [methodFilter, setMethodFilter] = useState<string[]>([])
  const [resultTab, setResultTab] = useState<'config' | 'quickstart' | 'files'>('config')
  const [registryCollapsed, setRegistryCollapsed] = useState(false)
  const [wizardStarted, setWizardStarted] = useState(false)
  const terminalEndRef = useRef<HTMLDivElement>(null)
  const [stepDirection, setStepDirection] = useState(1) // 1 = forward, -1 = back

  // File browser state
  const [browsePath, setBrowsePath] = useState<string | null>(null)
  const [browseFiles, setBrowseFiles] = useState<FileEntry[]>([])
  const [browseLoading, setBrowseLoading] = useState(false)
  const [browseRootPath, setBrowseRootPath] = useState<string | null>(null)

  // Delete confirmation

  // Server start/stop toggling

  // Full reset – used by refresh icon and Build Another
  const resetAll = useCallback(() => {
    setCurrentStep(1)
    setStepDirection(-1)
    setEndpoints(null)
    setResult(null)
    setAnalyzeError('')
    setIsAnalyzing(false)
    setIsGenerating(false)
    setGenerationPhase('')
    setCurrentIteration(0)
    setGenerationLogs([])
    setSourceMode('internal')
    setExternalApiName('')
    setExternalSourceType('url')
    setApiDocsUrl('')
    setApiDocsText('')
    setUploadedFile(null)
    setDetectedBaseUrl('')
    setDetectedAuthType('')
    setDetectedAuthDetails('')
    setPagesCrawled(0)
    setKbProductId(null)
    setDestinationFolder('')
    setSelectedProductId(null)
    setCopiedCmd(false)
    sessionStorage.removeItem('mcp_builder_state')
  }, [])

  // On mount: check if a generation was in progress when we left
  useEffect(() => {
    const genState = sessionStorage.getItem(GEN_SESSION_KEY)
    if (genState && !isGenerating) {
      const { serverName } = JSON.parse(genState)
      // Start polling the backend for progress
      setIsGenerating(true)
      setGenerationPhase('Resuming generation...')
      setCurrentStep(3)
      const token = localStorage.getItem('rt_access_token')

      const poll = setInterval(async () => {
        try {
          const res = await fetch(`/api/v1/mcp-builder/progress/${serverName}`, {
            headers: { 'Authorization': `Bearer ${token}` }
          })
          if (!res.ok) return
          const data = await res.json()

          if (data.status === 'not_found') {
            clearInterval(poll)
            progressPollRef.current = null
            setIsGenerating(false)
            sessionStorage.removeItem(GEN_SESSION_KEY)
            fetchServers()
            return
          }

          setGenerationPhase(data.phase || '')
          setCurrentIteration(data.iteration || 0)
          if (data.logs?.length) {
            setGenerationLogs(data.logs.slice(-10))
          }

          if (data.status === 'completed' || data.status === 'failed') {
            clearInterval(poll)
            progressPollRef.current = null
            setIsGenerating(false)
            sessionStorage.removeItem(GEN_SESSION_KEY)
            if (data.result) {
              setResult({
                status: data.result.status || 'success',
                message: data.result.message,
                mcpServers: data.result.mcpServers,
                quickStartCommands: data.result.quickStartCommands,
              })
            }
            fetchServers()
          }
        } catch {
          // Network error — keep polling
        }
      }, 2000)

      progressPollRef.current = poll
      return () => { clearInterval(poll); progressPollRef.current = null }
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Persist key state to sessionStorage on every change
  useEffect(() => {
    saveSessionState({
      selectedProductId, endpoints, result,
      generationPhase, generationLogs, sourceMode, externalApiName,
      externalSourceType, apiDocsUrl, apiDocsText, detectedBaseUrl,
      detectedAuthType, detectedAuthDetails, pagesCrawled,
      currentStep,
    })
  }, [
    selectedProductId, endpoints, result,
    generationPhase, generationLogs, sourceMode, externalApiName,
    externalSourceType, apiDocsUrl, apiDocsText, detectedBaseUrl,
    detectedAuthType, detectedAuthDetails, pagesCrawled,
    currentStep,
  ])

  // Resume in-flight analysis if user navigated away and came back
  useEffect(() => {
    if (_inflightAnalysis && !endpoints && !isAnalyzing) {
      setIsAnalyzing(true)
      setAnalyzePhase(_inflightAnalysis.phase)
      _inflightAnalysis.promise.then((analysisResult) => {
        _inflightAnalysis = null
        if (analysisResult.endpoints) {
          setEndpoints(analysisResult.endpoints)
          saveSessionState({ ...loadSessionState(), endpoints: analysisResult.endpoints })
          if (analysisResult.endpoints.length > 0) {
            setCurrentStep(2)
            setStepDirection(1)
          }
        }
        if (analysisResult.detectedBaseUrl !== undefined) {
          setDetectedBaseUrl(analysisResult.detectedBaseUrl)
          setDetectedAuthType(analysisResult.detectedAuthType)
          setDetectedAuthDetails(analysisResult.detectedAuthDetails)
          setKbProductId(analysisResult.kbProductId)
          setPagesCrawled(analysisResult.pagesCrawled)
          saveSessionState({
            ...loadSessionState(),
            endpoints: analysisResult.endpoints,
            detectedBaseUrl: analysisResult.detectedBaseUrl,
            detectedAuthType: analysisResult.detectedAuthType,
            detectedAuthDetails: analysisResult.detectedAuthDetails,
            kbProductId: analysisResult.kbProductId,
            pagesCrawled: analysisResult.pagesCrawled,
          })
        }
        if (analysisResult.error) {
          setAnalyzeError(analysisResult.error)
        }
        setIsAnalyzing(false)
        setAnalyzePhase('')
      }).catch((err) => {
        _inflightAnalysis = null
        setAnalyzeError(err.message || 'Analysis failed')
        setIsAnalyzing(false)
        setAnalyzePhase('')
      })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const fetchServers = useCallback(async () => {
    try {
      const token = localStorage.getItem('rt_access_token')
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
        const token = localStorage.getItem('rt_access_token')
        const res = await fetch('/api/v1/products', {
          headers: { 'Authorization': `Bearer ${token}` }
        })
        if (res.ok) {
          const data = await res.json()
          const trained = data.filter((p: any) =>
            p.folder_groups?.some((g: any) => g.training_status === 'completed')
          )
          setProducts(trained)
          if (trained.length > 0) setSelectedProductId(trained[0].product_id)
        }
      } catch (e) {
        console.error("Failed to load products", e)
      }
    }
    fetchProducts()
  }, [])

  // Auto-scroll terminal logs
  useEffect(() => {
    terminalEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [generationLogs])

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
    const initialPhase = sourceMode === 'internal' ? 'Scanning Knowledge Base...' : 'Fetching documentation...'
    setAnalyzePhase(initialPhase)
    setResult(null)

    // Show crawling phase after a delay for external URL mode
    let phaseTimer: ReturnType<typeof setTimeout> | null = null
    if (sourceMode === 'external' && externalSourceType === 'url') {
      phaseTimer = setTimeout(() => setAnalyzePhase('Crawling linked documentation pages...'), 3000)
    }

    const token = localStorage.getItem('rt_access_token')
    const effectiveDocsText = uploadedFile ? uploadedFile.text : apiDocsText

    // Build the analysis promise and store it module-level so it survives unmount
    const analysisPromise = (async (): Promise<any> => {
      if (sourceMode === 'internal') {
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
        const mapped = (data.endpoints || []).map((ep: any, i: number) => ({
          ...ep, id: `${ep.method}-${ep.path}-${i}`, checked: true
        }))
        const result = { endpoints: mapped, error: mapped.length === 0 ? 'No API endpoints were discovered.' : null }
        saveSessionState({ ...loadSessionState(), endpoints: mapped })
        return result
      } else {
        let effectiveKbId = kbProductId
        if (!effectiveKbId) {
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
        }

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
        const mapped = (data.endpoints || []).map((ep: any, i: number) => ({
          ...ep, id: `${ep.method}-${ep.path}-${i}`, checked: true
        }))
        const result = {
          endpoints: mapped,
          detectedBaseUrl: data.base_url || null,
          detectedAuthType: data.auth_type || 'none',
          detectedAuthDetails: data.auth_details || null,
          kbProductId: data.kb_product_id || kbId,
          pagesCrawled: data.endpoints?.length || 0,
          error: mapped.length === 0 ? 'No API endpoints were discovered. Try a different documentation URL.' : null,
        }
        saveSessionState({ ...loadSessionState(), ...result })
        return result
      }
    })()

    // Store in module-level variable so remount can pick it up
    _inflightAnalysis = { promise: analysisPromise, phase: initialPhase }

    try {
      const analysisResult = await analysisPromise
      _inflightAnalysis = null
      if (phaseTimer) clearTimeout(phaseTimer)

      setEndpoints(analysisResult.endpoints)
      if (analysisResult.detectedBaseUrl !== undefined) {
        setDetectedBaseUrl(analysisResult.detectedBaseUrl)
        setDetectedAuthType(analysisResult.detectedAuthType)
        setDetectedAuthDetails(analysisResult.detectedAuthDetails)
        setKbProductId(analysisResult.kbProductId)
        setPagesCrawled(analysisResult.pagesCrawled)
      }
      if (analysisResult.error) {
        setAnalyzeError(analysisResult.error)
      } else {
        // Auto-advance to step 2
        setCurrentStep(2)
        setStepDirection(1)
      }
    } catch (err: any) {
      _inflightAnalysis = null
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
    const safeName = prodName.toLowerCase().replace(/[^a-z0-9]/g, '_').replace(/_+/g, '_')

    // Save generation flag to sessionStorage so we can resume on remount
    sessionStorage.setItem(GEN_SESSION_KEY, JSON.stringify({ serverName: safeName, startedAt: Date.now() }))

    try {
      const token = localStorage.getItem('rt_access_token')

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
      sessionStorage.removeItem(GEN_SESSION_KEY)
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

  // === COMPUTED VALUES ===

  const filteredEndpoints = useMemo(() => {
    if (!endpoints) return []
    return endpoints.filter(ep => {
      const matchesSearch = !endpointSearch ||
        ep.path.toLowerCase().includes(endpointSearch.toLowerCase()) ||
        ep.description.toLowerCase().includes(endpointSearch.toLowerCase()) ||
        ep.suggested_tool_name.toLowerCase().includes(endpointSearch.toLowerCase())
      const matchesMethod = methodFilter.length === 0 ||
        methodFilter.includes(ep.method.toUpperCase())
      return matchesSearch && matchesMethod
    })
  }, [endpoints, endpointSearch, methodFilter])

  // File browser
  const browseDirectory = useCallback(async (path: string) => {
    setBrowseLoading(true)
    setBrowsePath(path)
    try {
      const data = await localFilesApi.browse(path)
      // Sort: directories first, then files, alphabetical
      const sorted = [...data.files].sort((a, b) => {
        if (a.type !== b.type) return a.type === 'directory' ? -1 : 1
        return a.name.localeCompare(b.name)
      })
      setBrowseFiles(sorted)
    } catch {
      setBrowseFiles([])
    } finally {
      setBrowseLoading(false)
    }
  }, [])

  // Folder picker for destination
  const pickerBrowse = useCallback(async (path: string) => {
    setPickerLoading(true)
    try {
      const data = await localFilesApi.browse(path)
      const dirs = data.files
        .filter((f: FileEntry) => f.type === 'directory')
        .sort((a: FileEntry, b: FileEntry) => a.name.localeCompare(b.name))
      // Resolve ~ to actual path using the first file's parent
      let resolvedPath = path
      if ((path === '~' || path.startsWith('~/')) && dirs.length > 0) {
        const firstChild = dirs[0].path
        resolvedPath = firstChild.substring(0, firstChild.lastIndexOf('/')) || '/'
      }
      setPickerPath(resolvedPath)
      setPickerFiles(dirs)
    } catch {
      setPickerPath(path)
      setPickerFiles([])
    } finally {
      setPickerLoading(false)
    }
  }, [])

  const openFolderPicker = useCallback(() => {
    setShowFolderPicker(true)
    pickerBrowse('~')
  }, [pickerBrowse])

  const getEffectiveDestFolder = useCallback(() => {
    // Find the destination folder from the most recent built server or from form input
    const safeName = (sourceMode === 'external' ? externalApiName : products.find(p => p.product_id === selectedProductId)?.product_name || '')
      .toLowerCase().replace(/[^a-z0-9]/g, '_').replace(/_+/g, '_')
    return destinationFolder || `~/mcp-servers/${safeName}`
  }, [destinationFolder, sourceMode, externalApiName, products, selectedProductId])

  // Auto-browse when switching to files tab
  useEffect(() => {
    if (resultTab === 'files' && !browsePath && result) {
      const folder = getEffectiveDestFolder()
      if (folder) {
        setBrowseRootPath(folder)
        browseDirectory(folder)
      }
    }
  }, [resultTab, browsePath, result, getEffectiveDestFolder, browseDirectory])

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  const browseBreadcrumbs = useMemo(() => {
    if (!browsePath || !browseRootPath) return []
    const rootParts = browseRootPath.replace(/^~/, '').split('/').filter(Boolean)
    const currentParts = browsePath.replace(/^~/, '').split('/').filter(Boolean)
    // Show only parts after the root
    const relativeParts = currentParts.slice(rootParts.length)
    return relativeParts
  }, [browsePath, browseRootPath])

  // Step summary text for completed steps
  const getStepSummary = (step: number) => {
    if (step === 1 && endpoints) {
      const name = sourceMode === 'external' ? externalApiName : products.find(p => p.product_id === selectedProductId)?.product_name
      return `${name || 'API'} — ${endpoints.length} endpoints`
    }
    if (step === 2 && endpoints) {
      const selected = endpoints.filter(e => e.checked).length
      return `${selected} of ${endpoints.length} tools selected`
    }
    return ''
  }

  const canAdvanceTo = (step: number) => {
    if (step === 2) return endpoints !== null && endpoints.length > 0
    if (step === 3) return endpoints !== null && endpoints.filter(e => e.checked).length > 0
    return true
  }

  const goToStep = (step: 1 | 2 | 3) => {
    if (step > currentStep && !canAdvanceTo(step)) return
    setStepDirection(step > currentStep ? 1 : -1)
    setCurrentStep(step)
  }

  // Progress percentage for generation terminal
  const getProgressPct = () => {
    const phase = generationPhase.toLowerCase()
    if (phase.includes('scaffold') || phase.includes('setting up') || phase.includes('planning')) return 20
    if (phase.includes('writing') || phase.includes('handler') || phase.includes('implement') || phase.includes('code')) return 55
    if (phase.includes('test') || phase.includes('validat') || phase.includes('fix')) return 80
    if (phase.includes('complete') || phase.includes('done') || phase.includes('success')) return 100
    if (currentIteration > 0) return Math.min(20 + currentIteration * 15, 90)
    return 10
  }

  // ===================== RENDER =====================

  const showHero = mcpServers.length === 0 && !endpoints && !isAnalyzing && !isGenerating && !result && !wizardStarted

  return (
    <div className="flex-1 p-8 overflow-y-auto">
      <div className="max-w-4xl mx-auto space-y-8">

        {/* Page Header */}
        <div>
          <h1 className="text-3xl font-display font-semibold text-rt-text-strong bg-clip-text text-transparent bg-gradient-to-r from-rt-text-strong to-rt-text flex items-center gap-3">
            <Wand2 className="w-8 h-8 text-rt-primary" />
            MCP Builder
          </h1>
          <p className="mt-2 text-rt-text-muted text-lg max-w-3xl">
            Auto-discover API capabilities and generate production-ready MCP servers with zero configuration.
          </p>
        </div>

        {/* ===== SERVER REGISTRY CARDS ===== */}
        {mcpServers.length > 0 && (
          <div className="space-y-3">
            <button
              onClick={() => setRegistryCollapsed(!registryCollapsed)}
              className="flex items-center gap-2 text-sm font-medium text-rt-text-strong hover:text-rt-primary transition-colors"
            >
              <Server className="w-4 h-4 text-rt-primary" />
              Your MCP Servers
              <span className="text-xs text-rt-text-muted font-normal bg-rt-bg px-2 py-0.5 rounded-full border border-rt-border">{mcpServers.length}</span>
              {registryCollapsed ? <ChevronRight className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
            </button>

            <AnimatePresence>
              {!registryCollapsed && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.2 }}
                  className="overflow-hidden"
                >
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                    {mcpServers.map((srv, index) => (
                      <motion.div
                        key={srv.server_id}
                        initial={{ opacity: 0, y: 20 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ delay: index * 0.05 }}
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
                          if (srv.selected_endpoints_json) {
                            setEndpoints(srv.selected_endpoints_json.map((ep: any, i: number) => ({
                              ...ep,
                              id: `${ep.method}-${ep.path}-${i}`,
                              checked: true
                            })))
                          }
                          setCurrentStep(1)
                        }}
                        className="p-4 bg-rt-surface border border-rt-border/50 rounded-xl hover:border-rt-primary/40 transition-all cursor-pointer group shadow-sm"
                      >
                        <div className="flex items-start justify-between mb-3">
                          <div className="flex items-center gap-2 min-w-0">
                            <Circle className="w-2.5 h-2.5 flex-shrink-0 fill-emerald-400 text-emerald-400" />
                            <span className="text-sm font-medium text-rt-text-strong truncate">{srv.name}</span>
                          </div>
                        </div>
                        <div className="space-y-1.5 mb-3">
                          <p className="text-xs text-rt-text-muted truncate">{srv.product_name}</p>
                          <div className="flex items-center gap-2">
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-rt-primary/10 text-rt-primary border border-rt-primary/20">
                              {srv.source_type === 'internal' ? 'Internal' : 'External'}
                            </span>
                            {srv.selected_endpoints_json && (
                              <span className="text-[10px] text-rt-text-muted">
                                {srv.selected_endpoints_json.length} tools
                              </span>
                            )}
                          </div>
                        </div>
                        <div className="flex items-center justify-between pt-2 border-t border-rt-border/30">
                          <span className="text-[10px] text-rt-text-muted flex items-center gap-1">
                            <Clock className="w-3 h-3" />
                            {new Date(srv.created_at).toLocaleDateString()}
                          </span>
                          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                            <button
                              onClick={async (e) => {
                                e.stopPropagation()
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
                              onClick={(e) => {
                                e.stopPropagation()
                                setBrowseRootPath(srv.destination_folder)
                                setBrowsePath(null)
                                setResultTab('files')
                                setResult({ status: 'success', mcpServers: srv.mcp_config_json, quickStartCommands: srv.quick_start_commands })
                                setStepDirection(1)
                                setCurrentStep(3)
                                // Trigger browse after state update
                                setTimeout(() => browseDirectory(srv.destination_folder), 100)
                              }}
                              className="p-1 rounded hover:bg-rt-primary/10 text-rt-text-muted hover:text-rt-primary transition-colors"
                              title="Browse files"
                            >
                              <FolderOpen className="w-3.5 h-3.5" />
                            </button>
                          </div>
                        </div>
                      </motion.div>
                    ))}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}

        {/* ===== HERO EMPTY STATE ===== */}
        {showHero && (
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5 }}
            className="flex flex-col items-center justify-center text-center py-16 space-y-6"
          >
            <div className="icon-orb flex items-center justify-center">
              <Wand2 className="w-7 h-7 text-rt-primary" />
            </div>
            <div className="space-y-2">
              <h2 className="text-2xl font-display font-semibold text-rt-text-strong">Build MCP Servers in Seconds</h2>
              <p className="text-rt-text-muted max-w-md mx-auto">
                Point at any API — internal or external — and Retrace will auto-discover endpoints, generate production-ready code, and give you a ready-to-use MCP server.
              </p>
            </div>
            <div className="flex items-center gap-6 text-sm text-rt-text-muted">
              <span className="flex items-center gap-1.5"><Search className="w-4 h-4 text-rt-primary" /> Auto-discovers endpoints</span>
              <span className="flex items-center gap-1.5"><FileCode className="w-4 h-4 text-rt-primary" /> AI-generated code</span>
              <span className="flex items-center gap-1.5"><Zap className="w-4 h-4 text-rt-primary" /> One-click deploy</span>
            </div>
            <button
              onClick={() => { setWizardStarted(true); setCurrentStep(1) }}
              className="btn-primary mt-4"
            >
              Get Started
            </button>
          </motion.div>
        )}

        {/* ===== STEPPER BAR ===== */}
        {!showHero && (
          <div className="flex items-center justify-center gap-0">
            <button
              onClick={resetAll}
              title="Reset & start over"
              className="mr-3 p-2 rounded-lg text-rt-text-muted hover:text-rt-primary hover:bg-rt-primary/10 transition-all"
            >
              <RotateCcw className="w-4 h-4" />
            </button>
            {STEPS.map((step, i) => {
              const isActive = currentStep === step.num
              const isCompleted = currentStep > step.num
              const isAccessible = canAdvanceTo(step.num) || step.num <= currentStep
              const StepIcon = step.icon
              return (
                <div key={step.num} className="flex items-center">
                  {i > 0 && (
                    <div className={`w-16 sm:w-24 h-px mx-1 ${isCompleted || isActive ? 'bg-rt-primary' : 'bg-rt-border border-dashed'}`} />
                  )}
                  <button
                    onClick={() => isAccessible && goToStep(step.num as 1 | 2 | 3)}
                    className={`flex flex-col items-center gap-1.5 px-3 py-2 rounded-xl transition-all ${
                      isAccessible ? 'cursor-pointer' : 'cursor-default opacity-40'
                    } ${isActive ? 'bg-rt-primary/10' : 'hover:bg-rt-surface'}`}
                  >
                    <div className={`w-9 h-9 rounded-full flex items-center justify-center text-sm font-bold transition-all ${
                      isActive
                        ? 'bg-rt-primary text-white shadow-md shadow-rt-primary/30'
                        : isCompleted
                          ? 'bg-rt-primary/20 text-rt-primary'
                          : 'bg-rt-bg border border-rt-border text-rt-text-muted'
                    }`}>
                      {isCompleted ? <Check className="w-4 h-4" /> : <StepIcon className="w-4 h-4" />}
                    </div>
                    <span className={`text-xs font-medium ${isActive ? 'text-rt-primary' : 'text-rt-text-muted'}`}>
                      {step.label}
                    </span>
                    {isCompleted && (
                      <motion.span
                        initial={{ opacity: 0, scale: 0.9 }}
                        animate={{ opacity: 1, scale: 1 }}
                        className="text-[10px] text-rt-text-muted bg-rt-bg px-2 py-0.5 rounded-full border border-rt-border max-w-[120px] truncate"
                      >
                        {getStepSummary(step.num)}
                      </motion.span>
                    )}
                  </button>
                </div>
              )
            })}
          </div>
        )}

        {/* ===== WIZARD STEP CONTENT ===== */}
        {!showHero && (
          <AnimatePresence mode="wait" initial={false}>
            {/* ===== STEP 1: SOURCE ===== */}
            {currentStep === 1 && (
              <motion.div
                key="step-1"
                initial={{ opacity: 0, x: stepDirection * 40 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: stepDirection * -40 }}
                transition={{ duration: 0.25, ease: 'easeInOut' }}
                className="space-y-6"
              >
                <div className="p-6 bg-rt-surface border border-rt-border/50 shadow-sm rounded-2xl">
                  <h2 className="text-xl font-medium mb-4 text-rt-text-strong">Select API Source</h2>

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
                                          setIsUploadingFile(true);
                                          try {
                                            const token = localStorage.getItem('rt_access_token');
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
                                            setApiDocsText('');
                                          } catch (err: any) {
                                            setAnalyzeError(`File upload failed: ${err.message}`);
                                          } finally {
                                            setIsUploadingFile(false);
                                          }
                                        } else {
                                          const reader = new FileReader();
                                          reader.onload = (ev) => {
                                            const text = ev.target?.result?.toString() || '';
                                            setUploadedFile({
                                              name: file.name,
                                              pages: 1,
                                              method: 'text',
                                              text,
                                            });
                                            setApiDocsText('');
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
                        disabled={isAnalyzing || (sourceMode === 'internal' ? !selectedProductId : !externalApiName || (externalSourceType === 'url' ? !apiDocsUrl : !apiDocsText && !uploadedFile))}
                        className="w-full h-11 bg-rt-primary hover:bg-rt-primary-dark text-white rounded-lg font-medium shadow-sm disabled:opacity-50 transition-all flex items-center justify-center gap-2 group"
                      >
                        {isAnalyzing ? (
                          <>
                            <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
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

                {/* Navigation */}
                {endpoints && (
                  <div className="flex justify-end">
                    <button
                      onClick={() => goToStep(2)}
                      className="flex items-center gap-2 px-5 py-2.5 bg-rt-primary text-white rounded-lg font-medium hover:bg-rt-primary-dark transition-all shadow-sm"
                    >
                      Next: Select Capabilities
                      <ChevronRight className="w-4 h-4" />
                    </button>
                  </div>
                )}
              </motion.div>
            )}

            {/* ===== STEP 2: CAPABILITIES ===== */}
            {currentStep === 2 && endpoints && (
              <motion.div
                key="step-2"
                initial={{ opacity: 0, x: stepDirection * 40 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: stepDirection * -40 }}
                transition={{ duration: 0.25, ease: 'easeInOut' }}
                className="space-y-6"
              >
                <div className="p-6 bg-rt-surface border border-rt-border/50 shadow-sm rounded-2xl">
                  <div className="flex items-center justify-between mb-4">
                    <h2 className="text-xl font-medium text-rt-text-strong">Select Server Capabilities</h2>
                    <span className="text-sm text-rt-text-muted font-normal bg-rt-bg px-3 py-1 rounded-full border border-rt-border">
                      {endpoints.filter(e => e.checked).length} of {endpoints.length} selected
                    </span>
                  </div>

                  {/* AI Detection Badges for External APIs */}
                  {sourceMode === 'external' && (detectedBaseUrl || detectedAuthType) && (
                    <div className="mb-4 p-3 bg-rt-primary/5 border border-rt-primary/20 rounded-xl">
                      <div className="flex items-center gap-2 mb-2">
                        <Wand2 className="w-3.5 h-3.5 text-rt-primary" />
                        <span className="text-xs font-medium text-rt-primary">AI Detected Configuration</span>
                      </div>
                      <div className="flex flex-wrap gap-2 text-xs">
                        {detectedBaseUrl && (
                          <span className="bg-rt-surface px-2 py-1 rounded-md border border-rt-border/50 font-mono truncate max-w-[250px] text-rt-text-muted">
                            {detectedBaseUrl}
                          </span>
                        )}
                        {detectedAuthType && (
                          <span className="bg-rt-surface px-2 py-1 rounded-md border border-rt-border/50 uppercase text-rt-text-muted">
                            Auth: {detectedAuthType}
                          </span>
                        )}
                        {pagesCrawled > 1 && (
                          <span className="bg-emerald-500/10 text-emerald-400 px-2 py-1 rounded-md border border-emerald-500/20">
                            Crawled {pagesCrawled} pages
                          </span>
                        )}
                      </div>
                    </div>
                  )}

                  {/* Search + Filters */}
                  <div className="space-y-3 mb-4">
                    <div className="relative">
                      <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted/50" />
                      <input
                        type="text"
                        placeholder="Search endpoints..."
                        value={endpointSearch}
                        onChange={(e) => setEndpointSearch(e.target.value)}
                        className="w-full bg-rt-bg border border-rt-border rounded-lg pl-9 pr-4 py-2 text-sm focus:ring-2 focus:ring-rt-primary/20 focus:border-rt-primary transition-all text-rt-text"
                      />
                    </div>
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-1.5">
                        {['GET', 'POST', 'PUT', 'DELETE'].map(method => (
                          <button
                            key={method}
                            onClick={() => setMethodFilter(prev =>
                              prev.includes(method) ? prev.filter(m => m !== method) : [...prev, method]
                            )}
                            className={`text-[10px] font-bold px-2 py-1 rounded border transition-all ${
                              methodFilter.includes(method)
                                ? getMethodColor(method)
                                : 'text-rt-text-muted/50 bg-rt-bg border-rt-border/50 hover:border-rt-border'
                            }`}
                          >
                            {method}
                          </button>
                        ))}
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => {
                            const filteredIds = new Set(filteredEndpoints.map(e => e.id))
                            setEndpoints(endpoints.map(ep => filteredIds.has(ep.id) ? { ...ep, checked: true } : ep))
                          }}
                          className="text-[11px] text-rt-primary hover:underline"
                        >
                          Select All
                        </button>
                        <span className="text-rt-border">|</span>
                        <button
                          onClick={() => {
                            const filteredIds = new Set(filteredEndpoints.map(e => e.id))
                            setEndpoints(endpoints.map(ep => filteredIds.has(ep.id) ? { ...ep, checked: false } : ep))
                          }}
                          className="text-[11px] text-rt-text-muted hover:text-rt-text hover:underline"
                        >
                          None
                        </button>
                      </div>
                    </div>
                  </div>

                  {/* Endpoint List */}
                  <div className="overflow-y-auto pr-1 space-y-2 max-h-[450px] custom-scrollbar pb-2">
                    {filteredEndpoints.map(ep => (
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
                    {filteredEndpoints.length === 0 && endpoints.length > 0 && (
                      <div className="text-center py-8 text-sm text-rt-text-muted">
                        No endpoints match your filters.
                      </div>
                    )}
                  </div>
                </div>

                {/* Navigation */}
                <div className="flex items-center justify-between">
                  <button
                    onClick={() => goToStep(1)}
                    className="flex items-center gap-2 px-4 py-2.5 text-rt-text-muted hover:text-rt-text-strong rounded-lg hover:bg-rt-surface transition-all"
                  >
                    <ChevronLeft className="w-4 h-4" />
                    Back
                  </button>
                  <button
                    onClick={() => goToStep(3)}
                    disabled={endpoints.filter(e => e.checked).length === 0}
                    className="flex items-center gap-2 px-5 py-2.5 bg-rt-primary text-white rounded-lg font-medium hover:bg-rt-primary-dark transition-all shadow-sm disabled:opacity-50"
                  >
                    Next: Deploy
                    <ChevronRight className="w-4 h-4" />
                  </button>
                </div>
              </motion.div>
            )}

            {/* ===== STEP 3: DEPLOY ===== */}
            {currentStep === 3 && (
              <motion.div
                key="step-3"
                initial={{ opacity: 0, x: stepDirection * 40 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: stepDirection * -40 }}
                transition={{ duration: 0.25, ease: 'easeInOut' }}
                className="space-y-6"
              >
                {/* Deploy Config */}
                {!result && (
                  <div className="p-6 bg-rt-surface border border-rt-border/50 shadow-sm rounded-2xl">
                    <h2 className="text-xl font-medium mb-6 text-rt-text-strong">Deploy Server</h2>

                    <div className="space-y-5">
                      <div className="space-y-1.5">
                        <label className="text-sm font-medium text-rt-text-muted">File Destination</label>
                        <button
                          type="button"
                          onClick={openFolderPicker}
                          className="w-full flex items-center gap-3 bg-rt-bg border border-rt-border rounded-lg px-4 py-2.5 text-sm hover:bg-rt-surface transition-colors text-left"
                        >
                          <FolderOpen className="w-4 h-4 text-rt-text-muted/50 shrink-0" />
                          {destinationFolder ? (
                            <span className="text-rt-text font-mono text-xs truncate">{destinationFolder}</span>
                          ) : (
                            <span className="text-rt-text-muted/50 text-xs">Click to select folder...</span>
                          )}
                        </button>
                        <p className="text-[11px] text-rt-text-muted/60 mt-1">If blank, creates a default folder.</p>
                      </div>

                      {/* Folder Picker Modal */}
                      {showFolderPicker && (
                        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm" onClick={() => setShowFolderPicker(false)}>
                          <div className="bg-rt-surface border border-rt-border rounded-2xl shadow-2xl w-[520px] max-h-[500px] flex flex-col" onClick={e => e.stopPropagation()}>
                            {/* Header */}
                            <div className="px-5 py-4 border-b border-rt-border/50">
                              <h3 className="text-sm font-semibold text-rt-text-strong mb-2">Select Destination Folder</h3>
                              {/* Breadcrumb */}
                              <div className="flex items-center gap-1 text-xs text-rt-text-muted overflow-x-auto">
                                <button
                                  onClick={() => pickerBrowse('/')}
                                  className="hover:text-rt-primary transition-colors shrink-0"
                                >/</button>
                                {pickerPath.replace(/^~/, '').split('/').filter(Boolean).map((part, i, arr) => {
                                  const fullPath = '/' + arr.slice(0, i + 1).join('/')
                                  return (
                                    <span key={fullPath} className="flex items-center gap-1 shrink-0">
                                      <ChevronRight className="w-3 h-3 text-rt-text-muted/40" />
                                      <button
                                        onClick={() => pickerBrowse(fullPath)}
                                        className={`hover:text-rt-primary transition-colors ${i === arr.length - 1 ? 'text-rt-text-strong font-medium' : ''}`}
                                      >{part}</button>
                                    </span>
                                  )
                                })}
                              </div>
                            </div>

                            {/* Directory list */}
                            <div className="flex-1 overflow-y-auto min-h-0 max-h-[320px]">
                              {pickerLoading ? (
                                <div className="flex items-center justify-center py-12 text-rt-text-muted text-sm">Loading...</div>
                              ) : pickerFiles.length === 0 ? (
                                <div className="flex items-center justify-center py-12 text-rt-text-muted text-sm">No subdirectories</div>
                              ) : (
                                <div className="py-1">
                                  {pickerFiles.map(file => (
                                    <button
                                      key={file.path}
                                      onClick={() => pickerBrowse(file.path)}
                                      className="w-full flex items-center gap-3 px-5 py-2.5 hover:bg-rt-primary/5 transition-colors text-left"
                                    >
                                      <Folder className="w-4 h-4 text-rt-primary/70 shrink-0" />
                                      <span className="text-sm text-rt-text truncate">{file.name}</span>
                                    </button>
                                  ))}
                                </div>
                              )}
                            </div>

                            {/* Footer */}
                            <div className="px-5 py-3 border-t border-rt-border/50 flex items-center justify-between">
                              <p className="text-xs text-rt-text-muted font-mono truncate max-w-[280px]">{pickerPath}</p>
                              <div className="flex gap-2">
                                <button
                                  onClick={() => setShowFolderPicker(false)}
                                  className="px-3 py-1.5 text-xs text-rt-text-muted hover:text-rt-text transition-colors"
                                >Cancel</button>
                                <button
                                  onClick={() => {
                                    setDestinationFolder(pickerPath)
                                    setShowFolderPicker(false)
                                  }}
                                  className="px-4 py-1.5 text-xs bg-rt-primary text-white rounded-lg hover:bg-rt-primary-dark transition-colors font-medium"
                                >Select This Folder</button>
                              </div>
                            </div>
                          </div>
                        </div>
                      )}

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
                              <Zap className="w-5 h-5" />
                              Generate MCP Codebase
                            </>
                          )}
                        </button>
                      </div>
                    </div>
                  </div>
                )}

                {/* ===== GENERATION TERMINAL ===== */}
                {isGenerating && (
                  <div className="bg-[#0d1117] rounded-xl border border-rt-border overflow-hidden shadow-lg">
                    {/* Progress bar */}
                    <div className="h-1 bg-gray-800">
                      <motion.div
                        className="h-full bg-rt-primary"
                        animate={{ width: `${getProgressPct()}%` }}
                        transition={{ duration: 0.5 }}
                      />
                    </div>
                    {/* Header */}
                    <div
                      className="flex items-center justify-between px-4 py-3 border-b border-gray-800 cursor-pointer"
                      onClick={() => setProgressExpanded(prev => !prev)}
                    >
                      <div className="flex items-center gap-3">
                        <Loader2 className="w-4 h-4 text-rt-primary animate-spin" />
                        <div>
                          <span className="text-sm font-medium text-gray-200">Generation in progress</span>
                          <span className="text-xs text-gray-500 ml-3">
                            {generationPhase || 'Agent is working...'}
                            {currentIteration > 0 && <span className="ml-2 text-rt-primary/70">Step {currentIteration}</span>}
                          </span>
                        </div>
                      </div>
                      {progressExpanded
                        ? <ChevronUp className="w-4 h-4 text-gray-500" />
                        : <ChevronDown className="w-4 h-4 text-gray-500" />
                      }
                    </div>
                    {/* Terminal body */}
                    {progressExpanded && (
                      <div className="p-4 max-h-[280px] overflow-y-auto custom-scrollbar font-mono text-xs">
                        {generationLogs.length === 0 && (
                          <div className="text-gray-600 py-4 text-center">Waiting for output...</div>
                        )}
                        {generationLogs.slice(-15).map((log, i) => (
                          <div key={i} className="text-gray-300 py-0.5 flex items-start gap-2">
                            <span className="text-rt-primary flex-shrink-0">{'>'}</span>
                            <span className="break-all">{log}</span>
                          </div>
                        ))}
                        <div ref={terminalEndRef} />
                      </div>
                    )}
                  </div>
                )}

                {/* ===== SUCCESS RESULT WITH TABS ===== */}
                {(result?.status === 'success' || result?.status === 'warning') && (
                  <motion.div
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    className="bg-rt-surface border border-rt-border/50 rounded-2xl shadow-sm overflow-hidden"
                  >
                    {/* Success header */}
                    <div className="p-6 bg-rt-success/5 border-b border-rt-success/20">
                      <div className="flex items-start gap-4 text-rt-success">
                        <div className="bg-rt-success/10 p-2 rounded-full mt-0.5">
                          <CheckCircle2 className="w-6 h-6" />
                        </div>
                        <div>
                          <h3 className="text-lg font-medium text-rt-success">Server Successfully Built</h3>
                          <p className="text-sm text-rt-text-muted mt-1">
                            {result?.status === 'warning'
                              ? result.message || 'Generation completed with warnings.'
                              : 'Copy the config below to install into Claude Desktop or Cursor.'
                            }
                          </p>
                        </div>
                      </div>
                    </div>

                    {/* Tabs */}
                    <div className="border-b border-rt-border/50">
                      <div className="flex px-6">
                        {[
                          { key: 'config', label: 'Config', icon: Copy },
                          { key: 'quickstart', label: 'Quick Start', icon: Terminal },
                          { key: 'files', label: 'Files Generated', icon: FileCode },
                        ].map(tab => (
                          <button
                            key={tab.key}
                            onClick={() => setResultTab(tab.key as any)}
                            className={`flex items-center gap-1.5 px-4 py-3 text-xs font-medium border-b-2 transition-all ${
                              resultTab === tab.key
                                ? 'border-rt-primary text-rt-primary'
                                : 'border-transparent text-rt-text-muted hover:text-rt-text'
                            }`}
                          >
                            <tab.icon className="w-3.5 h-3.5" />
                            {tab.label}
                          </button>
                        ))}
                      </div>
                    </div>

                    {/* Tab content */}
                    <div className="p-6">
                      <AnimatePresence mode="wait">
                        {resultTab === 'config' && (
                          <motion.div key="config" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}>
                            <div className="relative group">
                              <pre className="bg-[#111] p-4 rounded-xl text-xs font-mono text-[#43ff64d9] overflow-x-auto border border-rt-border shadow-inner max-h-[300px] overflow-y-auto custom-scrollbar">
                                {JSON.stringify({ mcpServers: result.mcpServers }, null, 2)}
                              </pre>
                              <button
                                onClick={handleCopy}
                                className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity bg-rt-surface border border-rt-border rounded-md p-2 shadow-lg hover:bg-rt-bg text-rt-text"
                              >
                                {copied ? <CheckCircle2 className="w-4 h-4 text-rt-success" /> : <Copy className="w-4 h-4" />}
                              </button>
                            </div>
                          </motion.div>
                        )}
                        {resultTab === 'quickstart' && (
                          <motion.div key="quickstart" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}>
                            {result.quickStartCommands ? (
                              <div className="relative group">
                                <pre className="bg-[#111] p-4 rounded-xl text-xs font-mono text-[#60a5fa] overflow-x-auto border border-rt-border shadow-inner whitespace-pre-wrap max-h-[300px] overflow-y-auto custom-scrollbar">
                                  {result.quickStartCommands}
                                </pre>
                                <button
                                  onClick={handleCopyCmd}
                                  className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity bg-rt-surface border border-rt-border rounded-md p-2 shadow-lg hover:bg-rt-bg text-rt-text"
                                >
                                  {copiedCmd ? <CheckCircle2 className="w-4 h-4 text-rt-success" /> : <Copy className="w-4 h-4" />}
                                </button>
                              </div>
                            ) : (
                              <p className="text-sm text-rt-text-muted text-center py-6">No quick start commands available.</p>
                            )}
                          </motion.div>
                        )}
                        {resultTab === 'files' && (
                          <motion.div key="files" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}>
                            {/* Breadcrumb */}
                            <div className="flex items-center gap-1 text-xs mb-3 overflow-x-auto bg-rt-bg rounded-lg px-3 py-2 border border-rt-border/50">
                              <button
                                onClick={() => browseRootPath && browseDirectory(browseRootPath)}
                                className={`px-1.5 py-0.5 rounded hover:bg-rt-border/50 transition-colors flex items-center gap-1 flex-shrink-0 ${
                                  browseBreadcrumbs.length === 0 ? 'text-rt-primary font-medium' : 'text-rt-text-muted'
                                }`}
                              >
                                <FolderOpen className="w-3.5 h-3.5" />
                                root
                              </button>
                              {browseBreadcrumbs.map((part, i) => (
                                <div key={i} className="flex items-center flex-shrink-0">
                                  <ChevronRight className="w-3 h-3 text-rt-text-muted/50" />
                                  <button
                                    onClick={() => {
                                      if (!browseRootPath) return
                                      const rootParts = browseRootPath.replace(/^~/, '').split('/').filter(Boolean)
                                      const targetPath = '/' + [...rootParts, ...browseBreadcrumbs.slice(0, i + 1)].join('/')
                                      browseDirectory(targetPath)
                                    }}
                                    className={`px-1.5 py-0.5 rounded hover:bg-rt-border/50 transition-colors ${
                                      i === browseBreadcrumbs.length - 1 ? 'text-rt-primary font-medium' : 'text-rt-text-muted'
                                    }`}
                                  >
                                    {part}
                                  </button>
                                </div>
                              ))}
                            </div>

                            {/* File list */}
                            <div className="border border-rt-border/50 rounded-lg overflow-hidden">
                              {browseLoading ? (
                                <div className="flex items-center justify-center py-8 text-rt-text-muted text-sm gap-2">
                                  <Loader2 className="w-4 h-4 animate-spin" />
                                  Loading files...
                                </div>
                              ) : browseFiles.length === 0 && browsePath ? (
                                <div className="flex flex-col items-center justify-center py-8 text-rt-text-muted text-sm gap-2">
                                  <Folder className="w-6 h-6 opacity-40" />
                                  <p>Directory is empty or not found.</p>
                                  <p className="text-[11px] font-mono opacity-60">{browsePath}</p>
                                </div>
                              ) : !browsePath ? (
                                <div className="flex flex-col items-center justify-center py-8 text-rt-text-muted text-sm gap-2">
                                  <Folder className="w-6 h-6 opacity-40" />
                                  <p>No destination folder available.</p>
                                </div>
                              ) : (
                                <div className="max-h-[320px] overflow-y-auto custom-scrollbar divide-y divide-rt-border/30">
                                  {/* Parent directory */}
                                  {browsePath !== browseRootPath && (
                                    <button
                                      onClick={() => {
                                        const parts = browsePath.split('/').filter(Boolean)
                                        const parent = '/' + parts.slice(0, -1).join('/')
                                        browseDirectory(parent || '/')
                                      }}
                                      className="w-full flex items-center gap-3 px-3 py-2 hover:bg-rt-surface transition-colors text-left text-sm"
                                    >
                                      <FolderOpen className="w-4 h-4 text-rt-primary" />
                                      <span className="text-rt-text-muted">..</span>
                                    </button>
                                  )}
                                  {browseFiles.map((file) => (
                                    <div
                                      key={file.path}
                                      className={`flex items-center gap-3 px-3 py-2 hover:bg-rt-surface transition-colors ${
                                        file.type === 'directory' ? 'cursor-pointer' : ''
                                      }`}
                                      onClick={() => {
                                        if (file.type === 'directory') browseDirectory(file.path)
                                      }}
                                    >
                                      {file.type === 'directory' ? (
                                        <Folder className="w-4 h-4 text-rt-primary flex-shrink-0" />
                                      ) : (
                                        <File className="w-4 h-4 text-rt-text-muted/60 flex-shrink-0" />
                                      )}
                                      <span className={`text-sm truncate flex-1 ${
                                        file.type === 'directory' ? 'text-rt-text-strong font-medium' : 'text-rt-text font-mono text-xs'
                                      }`}>
                                        {file.name}
                                      </span>
                                      {file.type === 'file' && (
                                        <span className="text-[10px] text-rt-text-muted/50 flex-shrink-0">
                                          {formatFileSize(file.size)}
                                        </span>
                                      )}
                                    </div>
                                  ))}
                                </div>
                              )}
                            </div>
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </div>
                  </motion.div>
                )}

                {/* Error result */}
                {result?.status === 'error' && (
                  <div className="p-6 bg-red-500/5 border border-red-500/20 rounded-2xl space-y-3 shadow-sm animate-fade-in">
                    <div className="flex items-center gap-3 text-red-500">
                      <AlertCircle className="w-6 h-6" />
                      <h3 className="text-lg font-medium">Generation Failed</h3>
                    </div>
                    <p className="text-sm text-rt-text-muted">{result.message}</p>
                  </div>
                )}

                {/* Navigation */}
                <div className="flex items-center justify-between">
                  <button
                    onClick={() => goToStep(2)}
                    className="flex items-center gap-2 px-4 py-2.5 text-rt-text-muted hover:text-rt-text-strong rounded-lg hover:bg-rt-surface transition-all"
                  >
                    <ChevronLeft className="w-4 h-4" />
                    Back
                  </button>
                  {result && (
                    <button
                      onClick={resetAll}
                      className="flex items-center gap-2 px-5 py-2.5 bg-rt-primary text-white rounded-lg font-medium hover:bg-rt-primary-dark transition-all shadow-sm"
                    >
                      <Zap className="w-4 h-4" />
                      Build Another
                    </button>
                  )}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        )}

      </div>

      {/* Delete Confirmation Modal */}
    </div>
  )
}
