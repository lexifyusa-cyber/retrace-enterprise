import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Bot, Send, Loader2, Terminal, FileText, Search, Globe, Monitor,
  Package, Check, ChevronDown, ChevronRight, AlertTriangle, Code, Play, X, Database,
  FileEdit, Plus, MessageSquare, Trash2, Edit2, GraduationCap, CheckCircle2,
  ClipboardList, CalendarClock, Repeat, Timer,
} from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import toast from 'react-hot-toast'
import { productApi, agentApi } from '@/utils/api'
import { Product, AgentTool, Conversation } from '@/types'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface AgentMessage {
  id: string
  type: 'user' | 'status' | 'tools' | 'code' | 'output' | 'answer' | 'error' | 'done' | 'sop' | 'doc'
  content: string
  iteration?: number
  meta?: Record<string, any>
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function Agent() {
  const queryClient = useQueryClient()
  const [task, setTask] = useState('')
  const [selectedProductId, setSelectedProductId] = useState<string>('')
  const [showProductPicker, setShowProductPicker] = useState(false)
  const [messages, setMessages] = useState<AgentMessage[]>([])
  const [isRunning, setIsRunning] = useState(false)
  const [streamingAnswer, setStreamingAnswer] = useState<string | null>(null)
  const [streamingSOP, setStreamingSOP] = useState<string | null>(null)
  const [streamingDoc, setStreamingDoc] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const idCounter = useRef(0)

  // Conversation state
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null)
  const [editingTitle, setEditingTitle] = useState<string | null>(null)
  const [editTitleValue, setEditTitleValue] = useState('')

  const { data: products = [] } = useQuery({
    queryKey: ['products'],
    queryFn: () => productApi.list(),
  })

  const { data: tools = [] } = useQuery<AgentTool[]>({
    queryKey: ['agent-tools'],
    queryFn: () => agentApi.listTools(),
  })

  // Tool toggle via existing backend endpoint
  const updateToolsMutation = useMutation({
    mutationFn: (disabledTools: string[]) => agentApi.updateAgentTools(disabledTools),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-tools'] })
      toast.success('Agent tools updated')
    },
    onError: (err: any) => {
      toast.error(err?.response?.data?.detail || 'Failed to update tools')
    },
  })

  const handleToolToggle = (toolName: string, currentlyEnabled: boolean) => {
    const currentDisabled = tools.filter(t => t.enabled === false).map(t => t.name)
    let newDisabled: string[]
    if (currentlyEnabled) {
      const wouldBeEnabled = tools.filter(t => t.name !== toolName && (t.enabled !== false)).length
      if (wouldBeEnabled === 0) {
        toast.error('At least one tool must be enabled')
        return
      }
      newDisabled = [...currentDisabled, toolName]
    } else {
      newDisabled = currentDisabled.filter(n => n !== toolName)
    }
    updateToolsMutation.mutate(newDisabled)
  }

  // Conversations for the selected product (or product-less)
  const { data: conversations = [], refetch: refetchConversations } = useQuery<Conversation[]>({
    queryKey: ['conversations', selectedProductId || '__none__'],
    queryFn: () => agentApi.listConversations(selectedProductId || '__none__'),
  })

  const trainedProducts = products.filter((p: Product) =>
    p.folder_groups?.some(g => g.training_status === 'completed') === true
  )

  // When product changes, clear active conversation
  useEffect(() => {
    setActiveConversationId(null)
    setMessages([])
  }, [selectedProductId])

  // Auto-scroll
  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [messages, streamingAnswer, streamingSOP, streamingDoc])

  // Load conversation messages
  const loadConversation = useCallback(async (conversationId: string) => {
    try {
      const convo = await agentApi.getConversation(conversationId)
      setActiveConversationId(conversationId)
      if (convo.messages && convo.messages.length > 0) {
        setMessages(convo.messages.map((m, i) => ({
          id: `loaded_${i}`,
          type: m.type,
          content: m.content,
          iteration: m.iteration || undefined,
          meta: m.meta || undefined,
        })))
        idCounter.current = convo.messages.length
      } else {
        setMessages([])
        idCounter.current = 0
      }
    } catch {
      toast.error('Failed to load conversation')
    }
  }, [])

  const addMessage = useCallback((msg: Omit<AgentMessage, 'id'>) => {
    const id = `msg_${idCounter.current++}`
    setMessages(prev => {
      if (msg.type === 'status') {
        const withoutOldStatus = prev.filter(m => m.type !== 'status')
        return [...withoutOldStatus, { ...msg, id }]
      }
      const cleaned = prev.filter(m => m.type !== 'status')
      return [...cleaned, { ...msg, id }]
    })
  }, [])

  // Save messages to backend
  const saveMessagesToConversation = useCallback(async (conversationId: string, msgs: AgentMessage[]) => {
    try {
      const saveable = msgs.filter(m => m.type !== 'status').map(m => ({
        type: m.type, content: m.content, iteration: m.iteration, meta: m.meta,
      }))
      if (saveable.length > 0) {
        await agentApi.saveMessages(conversationId, saveable)
        refetchConversations()
      }
    } catch (err) {
      console.error('Failed to save messages:', err)
    }
  }, [refetchConversations])

  // Create new conversation
  const handleNewConversation = useCallback(async () => {
    try {
      const convo = await agentApi.createConversation(selectedProductId || '', 'New conversation')
      setActiveConversationId(convo.conversation_id)
      setMessages([])
      idCounter.current = 0
      refetchConversations()
    } catch {
      toast.error('Failed to create conversation')
    }
  }, [selectedProductId, refetchConversations])

  // Delete conversation
  const handleDeleteConversation = useCallback(async (conversationId: string, e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await agentApi.deleteConversation(conversationId)
      if (activeConversationId === conversationId) {
        setActiveConversationId(null)
        setMessages([])
      }
      refetchConversations()
      toast.success('Conversation deleted')
    } catch {
      toast.error('Failed to delete conversation')
    }
  }, [activeConversationId, refetchConversations])

  // Rename conversation
  const handleRenameConversation = useCallback(async (conversationId: string) => {
    if (!editTitleValue.trim()) return
    try {
      await agentApi.renameConversation(conversationId, editTitleValue.trim())
      setEditingTitle(null)
      refetchConversations()
    } catch {
      toast.error('Failed to rename conversation')
    }
  }, [editTitleValue, refetchConversations])

  // Learn This
  const handleLearnThis = useCallback(async (question: string, answer: string) => {
    if (!selectedProductId) {
      toast.error('Select a product to save knowledge')
      return
    }
    try {
      await agentApi.learnThis(selectedProductId, question, answer)
      toast.success('Added to knowledge base!', { icon: '🎓' })
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to learn Q&A')
      throw err
    }
  }, [selectedProductId])

  // Execute task
  const handleExecute = useCallback(async () => {
    if (!task.trim() || isRunning) return

    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    // Auto-create conversation if none active
    let conversationId = activeConversationId
    if (!conversationId) {
      try {
        const convo = await agentApi.createConversation(selectedProductId || '', task.slice(0, 80))
        conversationId = convo.conversation_id
        setActiveConversationId(conversationId)
        refetchConversations()
      } catch {
        toast.error('Failed to create conversation')
        return
      }
    }

    setIsRunning(true)
    const userMsg: AgentMessage = { id: `msg_${idCounter.current++}`, type: 'user', content: task }
    setMessages(prev => [...prev, userMsg])
    const currentTask = task
    setTask('')
    const newMessages: AgentMessage[] = [userMsg]

    try {
      const response = await fetch('/api/v1/agent/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ product_id: selectedProductId || null, task: currentTask, max_iterations: 10, conversation_id: conversationId }),
        signal: controller.signal,
      })

      if (!response.ok) {
        const err = await response.text()
        const errMsg: Omit<AgentMessage, 'id'> = { type: 'error', content: err }
        addMessage(errMsg)
        newMessages.push({ ...errMsg, id: `msg_${idCounter.current}` })
        setIsRunning(false)
        if (conversationId) await saveMessagesToConversation(conversationId, newMessages)
        return
      }

      const reader = response.body?.getReader()
      if (!reader) { addMessage({ type: 'error', content: 'No stream reader' }); setIsRunning(false); return }

      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        let currentEvent = ''
        for (const line of lines) {
          if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim() }
          else if (line.startsWith('data: ') && currentEvent) {
            try {
              const data = JSON.parse(line.slice(6))
              const msg = handleSSEEvent(currentEvent, data)
              if (msg) newMessages.push({ ...msg, id: `msg_${idCounter.current}` })
            } catch { /* ignore */ }
            currentEvent = ''
          }
        }
      }
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        const errMsg: Omit<AgentMessage, 'id'> = { type: 'error', content: err.message || 'Connection failed' }
        addMessage(errMsg)
        newMessages.push({ ...errMsg, id: `msg_${idCounter.current}` })
      }
    } finally {
      setIsRunning(false)
      if (conversationId) await saveMessagesToConversation(conversationId, newMessages)
    }
  }, [task, selectedProductId, isRunning, activeConversationId, addMessage, refetchConversations, saveMessagesToConversation])

  const handleSSEEvent = useCallback((event: string, data: any): Omit<AgentMessage, 'id'> | null => {
    let msg: Omit<AgentMessage, 'id'> | null = null
    switch (event) {
      case 'agent_status': {
        const content = data.message || data.step
        if (content !== 'Agent is working...') msg = { type: 'status', content }
        break
      }
      case 'agent_tools': msg = { type: 'tools', content: `${data.count} tools loaded: ${(data.tools || []).join(', ')}` }; break
      case 'agent_code': msg = { type: 'code', content: data.code, iteration: data.iteration }; break
      case 'tool_output': msg = { type: 'output', content: data.output, iteration: data.iteration }; break
      case 'agent_answer': msg = { type: 'answer', content: data.content }; break
      case 'agent_answer_chunk': {
        setStreamingAnswer(data.chunk)
        if (data.done) {
          msg = { type: 'answer', content: data.chunk }
          setStreamingAnswer(null)
        }
        break
      }
      case 'agent_done': msg = { type: 'done', content: `Completed in ${data.iterations} iteration(s)`, meta: data.timings }; break
      case 'agent_error': msg = { type: 'error', content: data.detail || 'Agent error' }; break
    }
    if (msg) addMessage(msg)
    return msg
  }, [addMessage])

  const handleSubmit = (e: React.FormEvent) => { e.preventDefault(); handleExecute() }
  const handleStop = () => { abortRef.current?.abort(); setIsRunning(false); addMessage({ type: 'status', content: 'Stopped by user' }) }

  // SOP creation
  const [isCreatingSOP, setIsCreatingSOP] = useState(false)

  // Documentation creation
  const [isCreatingDoc, setIsCreatingDoc] = useState(false)

  const handleCreateSOP = useCallback(async (goalOverride?: string) => {
    if (isCreatingSOP) return

    const userMsgs = messages.filter(m => m.type === 'user')
    const goal = goalOverride ?? userMsgs[userMsgs.length - 1]?.content
    if (!goal) {
      toast.error('No conversation to create automation from')
      return
    }
    const saveable = messages.filter(m => ['user', 'code', 'output', 'answer'].includes(m.type)).map(m => ({
      type: m.type, content: m.content, iteration: m.iteration, meta: m.meta,
    }))

    setIsCreatingSOP(true)
    addMessage({ type: 'status', content: 'Generating Automation...' })
    try {
      const response = await fetch('/api/v1/agent/create-sop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ product_id: selectedProductId || null, goal, messages: saveable, conversation_id: activeConversationId || undefined }),
      })
      if (!response.ok) {
        const err = await response.text()
        addMessage({ type: 'error', content: err })
        return
      }
      const reader = response.body?.getReader()
      if (!reader) { addMessage({ type: 'error', content: 'No stream reader' }); return }
      const decoder = new TextDecoder()
      let buffer = ''
      let sopResult: { sop_id?: string; title?: string; sop_markdown?: string } = {}
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        let currentEvent = ''
        for (const line of lines) {
          if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim() }
          else if (line.startsWith('data: ') && currentEvent) {
            try {
              const eventData = JSON.parse(line.slice(6))
              if (currentEvent === 'sop_chunk') {
                setStreamingSOP(eventData.chunk)
              } else if (currentEvent === 'sop_done') {
                sopResult = eventData
                setStreamingSOP(null)
              } else if (currentEvent === 'sop_error') {
                addMessage({ type: 'error', content: eventData.detail || 'Automation generation failed' })
                setStreamingSOP(null)
              }
            } catch { /* ignore */ }
            currentEvent = ''
          }
        }
      }
      if (sopResult.sop_id) {
        const sopMessage = { type: 'sop' as const, content: sopResult.sop_markdown || '', meta: { sop_id: sopResult.sop_id, title: sopResult.title } }
        addMessage(sopMessage)
        if (activeConversationId) {
          await saveMessagesToConversation(activeConversationId, [{ ...sopMessage, id: '' }])
        }
        toast.success('Automation created!')
      }
    } catch (err: any) {
      addMessage({ type: 'error', content: err?.message || 'Failed to create automation' })
      setStreamingSOP(null)
    } finally {
      setIsCreatingSOP(false)
    }
  }, [selectedProductId, messages, isCreatingSOP, activeConversationId, addMessage, saveMessagesToConversation])

  const handleApproveSOP = useCallback(async (sopId: string, scheduleType?: string, scheduleConfig?: Record<string, any>): Promise<void> => {
    try {
      await agentApi.approveSOP(sopId, scheduleType || 'none', scheduleConfig)
      toast.success('Automation approved & scheduled!')
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to approve automation')
      throw err
    }
  }, [])

  const handleEditSOP = useCallback(async (sopId: string, editInstructions: string): Promise<{ sop_markdown: string; title: string }> => {
    const response = await fetch('/api/v1/agent/edit-sop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sop_id: sopId, edit_instructions: editInstructions }),
    })
    if (!response.ok) throw new Error(await response.text())
    const reader = response.body?.getReader()
    if (!reader) throw new Error('No stream reader')
    const decoder = new TextDecoder()
    let buffer = ''
    let editResult: { sop_markdown: string; title: string } = { sop_markdown: '', title: '' }
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      let currentEvent = ''
      for (const line of lines) {
        if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim() }
        else if (line.startsWith('data: ') && currentEvent) {
          try {
            const eventData = JSON.parse(line.slice(6))
            if (currentEvent === 'sop_chunk') {
              setStreamingSOP(eventData.chunk)
            } else if (currentEvent === 'sop_done') {
              editResult = { sop_markdown: eventData.sop_markdown, title: eventData.title }
              setStreamingSOP(null)
            } else if (currentEvent === 'sop_error') {
              setStreamingSOP(null)
              throw new Error(eventData.detail || 'SOP edit failed')
            }
          } catch (e) { if (e instanceof Error && e.message !== 'SOP edit failed') { /* ignore parse errors */ } else throw e }
          currentEvent = ''
        }
      }
    }
    setMessages(prev => prev.map(m =>
      m.meta?.sop_id === sopId
        ? { ...m, content: editResult.sop_markdown, meta: { ...m.meta, title: editResult.title, status: 'draft' } }
        : m
    ))
    toast.success('Automation updated!')
    return editResult
  }, [])

  // Documentation creation
  const handleCreateDoc = useCallback(async (goalOverride?: string) => {
    if (isCreatingDoc) return

    const userMsgs = messages.filter(m => m.type === 'user')
    const goal = goalOverride ?? userMsgs[userMsgs.length - 1]?.content
    if (!goal) {
      toast.error('No conversation to create documentation from')
      return
    }
    const saveable = messages.filter(m => ['user', 'code', 'output', 'answer'].includes(m.type)).map(m => ({
      type: m.type, content: m.content, iteration: m.iteration, meta: m.meta,
    }))

    setIsCreatingDoc(true)
    addMessage({ type: 'status', content: 'Generating Documentation...' })
    try {
      const response = await fetch('/api/v1/agent/create-doc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ product_id: selectedProductId || null, goal, messages: saveable, conversation_id: activeConversationId || undefined }),
      })
      if (!response.ok) {
        const err = await response.text()
        addMessage({ type: 'error', content: err })
        return
      }
      const reader = response.body?.getReader()
      if (!reader) { addMessage({ type: 'error', content: 'No stream reader' }); return }
      const decoder = new TextDecoder()
      let buffer = ''
      let docResult: { doc_id?: string; title?: string; doc_markdown?: string } = {}
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        let currentEvent = ''
        for (const line of lines) {
          if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim() }
          else if (line.startsWith('data: ') && currentEvent) {
            try {
              const eventData = JSON.parse(line.slice(6))
              if (currentEvent === 'doc_chunk') {
                setStreamingDoc(eventData.chunk)
              } else if (currentEvent === 'doc_done') {
                docResult = eventData
                setStreamingDoc(null)
              } else if (currentEvent === 'doc_error') {
                addMessage({ type: 'error', content: eventData.detail || 'Documentation generation failed' })
                setStreamingDoc(null)
              }
            } catch { /* ignore */ }
            currentEvent = ''
          }
        }
      }
      if (docResult.doc_id) {
        const docMessage = { type: 'doc' as const, content: docResult.doc_markdown || '', meta: { doc_id: docResult.doc_id, title: docResult.title } }
        addMessage(docMessage)
        if (activeConversationId) {
          await saveMessagesToConversation(activeConversationId, [{ ...docMessage, id: '' }])
        }
        toast.success('Documentation created!')
      }
    } catch (err: any) {
      addMessage({ type: 'error', content: err?.message || 'Failed to create documentation' })
      setStreamingDoc(null)
    } finally {
      setIsCreatingDoc(false)
    }
  }, [selectedProductId, messages, isCreatingDoc, activeConversationId, addMessage, saveMessagesToConversation])

  const handleApproveDoc = useCallback(async (docId: string): Promise<void> => {
    try {
      await agentApi.approveDoc(docId)
      toast.success('Documentation approved!')
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to approve documentation')
      throw err
    }
  }, [])

  const handleEditDoc = useCallback(async (docId: string, editInstructions: string): Promise<{ doc_markdown: string; title: string }> => {
    const response = await fetch('/api/v1/agent/edit-doc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc_id: docId, edit_instructions: editInstructions }),
    })
    if (!response.ok) throw new Error(await response.text())
    const reader = response.body?.getReader()
    if (!reader) throw new Error('No stream reader')
    const decoder = new TextDecoder()
    let buffer = ''
    let editResult: { doc_markdown: string; title: string } = { doc_markdown: '', title: '' }
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      let currentEvent = ''
      for (const line of lines) {
        if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim() }
        else if (line.startsWith('data: ') && currentEvent) {
          try {
            const eventData = JSON.parse(line.slice(6))
            if (currentEvent === 'doc_chunk') {
              setStreamingDoc(eventData.chunk)
            } else if (currentEvent === 'doc_done') {
              editResult = { doc_markdown: eventData.doc_markdown, title: eventData.title }
              setStreamingDoc(null)
            } else if (currentEvent === 'doc_error') {
              setStreamingDoc(null)
              throw new Error(eventData.detail || 'Documentation edit failed')
            }
          } catch (e) { if (e instanceof Error && e.message !== 'Documentation edit failed') { /* ignore parse errors */ } else throw e }
          currentEvent = ''
        }
      }
    }
    setMessages(prev => prev.map(m =>
      m.meta?.doc_id === docId
        ? { ...m, content: editResult.doc_markdown, meta: { ...m.meta, title: editResult.title } }
        : m
    ))
    toast.success('Documentation updated!')
    return editResult
  }, [])

  const selectedProduct = trainedProducts.find((p: Product) => p.product_id === selectedProductId)
  const enabledToolsCount = tools.filter(t => t.available && (t.enabled !== false)).length

  // Collapsible tool blocks
  const [expandedToolBlocks, setExpandedToolBlocks] = useState<Set<string>>(new Set())
  const toggleToolBlock = useCallback((id: string) => {
    setExpandedToolBlocks(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }, [])

  type MessageNode = { kind: 'single'; index: number } | { kind: 'toolBlock'; toolsIndex: number; childIndices: number[] }
  const messageNodes = useMemo((): MessageNode[] => {
    const nodes: MessageNode[] = []
    let i = 0
    while (i < messages.length) {
      if (messages[i].type === 'tools') {
        const childIndices: number[] = []
        const toolsIdx = i
        i++
        while (i < messages.length && (messages[i].type === 'code' || messages[i].type === 'output')) {
          childIndices.push(i)
          i++
        }
        nodes.push({ kind: 'toolBlock', toolsIndex: toolsIdx, childIndices })
      } else {
        nodes.push({ kind: 'single', index: i })
        i++
      }
    }
    return nodes
  }, [messages])

  // Tool icon/name maps
  const toolIcons: Record<string, typeof Terminal> = {
    terminal: Terminal, read_file: FileText, write_file: FileEdit,
    basic_search: Search, deep_websearch: Globe, screenops: Monitor, search_knowledge_base: Database,
  }
  const toolNames: Record<string, string> = {
    terminal: 'Terminal', read_file: 'Read File', write_file: 'Write File',
    basic_search: 'Basic Search', deep_websearch: 'Deep Web Search', screenops: 'ScreenOps', search_knowledge_base: 'Knowledge Base',
  }

  // ── Render ───────────────────────────────────────────────────────
  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="p-8 pb-4 border-b border-rt-border">
        <div className="flex items-center gap-3 mb-2">
          <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-purple-500/20 to-blue-500/20 flex items-center justify-center">
            <Bot className="w-5 h-5 text-purple-400" />
          </div>
          <div>
            <h1 className="text-3xl font-display font-bold">Agent</h1>
            <p className="text-rt-text-muted text-sm">Execute tasks on your products using AI-powered tools</p>
          </div>
        </div>
      </div>

      {/* Product selector + tool status */}
      <div className="px-8 py-3 border-b border-rt-border bg-rt-bg-light flex items-center gap-4">
        <div className="relative">
          <button type="button" onClick={() => setShowProductPicker(!showProductPicker)}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-rt-border hover:border-rt-text-muted transition-colors text-sm">
            <Package className="w-4 h-4 text-rt-primary" />
            <span>{selectedProduct?.product_name || 'No product'}</span>
            <ChevronDown className={`w-4 h-4 text-rt-text-muted transition-transform ${showProductPicker ? 'rotate-180' : ''}`} />
          </button>
          <AnimatePresence>
            {showProductPicker && (
              <motion.div initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }}
                className="absolute top-full left-0 mt-1 w-64 bg-rt-bg-light border border-rt-border rounded-lg shadow-xl z-50 overflow-hidden">
                <button type="button"
                  onClick={() => { setSelectedProductId(''); setShowProductPicker(false) }}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-rt-surface transition-colors ${!selectedProductId ? 'bg-rt-primary/5 text-rt-primary' : ''}`}>
                  {!selectedProductId && <Check className="w-3.5 h-3.5" />}
                  <span className="text-rt-text-muted italic">No product (general)</span>
                </button>
                {trainedProducts.map((p: Product) => (
                  <button key={p.product_id} type="button"
                    onClick={() => { setSelectedProductId(p.product_id); setShowProductPicker(false) }}
                    className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-rt-surface transition-colors ${p.product_id === selectedProductId ? 'bg-rt-primary/5 text-rt-primary' : ''}`}>
                    {p.product_id === selectedProductId && <Check className="w-3.5 h-3.5" />}
                    <span className="truncate">{p.product_name}</span>
                  </button>
                ))}
                {trainedProducts.length === 0 && <p className="px-3 py-2 text-sm text-rt-text-muted italic">No trained products</p>}
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        {/* Tool icons — click to enable/disable */}
        <div className="flex items-center gap-3 text-xs text-rt-text-muted">
          <span className="flex items-center gap-1.5">
            <div className={`w-2 h-2 rounded-full ${enabledToolsCount > 0 ? 'bg-rt-success' : 'bg-rt-warning'}`} />
            <span>{enabledToolsCount}/{tools.length} tools</span>
          </span>
          <div className="flex gap-1">
            {tools.map(t => {
              const Icon = toolIcons[t.name] || Bot
              const displayName = toolNames[t.name] || t.name
              const kbNoProduct = t.name === 'search_knowledge_base' && !selectedProductId
              const isEnabled = !kbNoProduct && t.available && (t.enabled !== false)
              const canToggle = t.available && !kbNoProduct
              return (
                <button key={t.name} type="button"
                  onClick={() => canToggle && handleToolToggle(t.name, isEnabled)}
                  disabled={!canToggle || updateToolsMutation.isPending}
                  title={`${displayName}: ${isEnabled ? 'enabled (click to disable)' : 'disabled (click to enable)'}`}
                  className={`relative group p-1.5 rounded-lg transition-all ${canToggle ? isEnabled ? 'text-rt-primary hover:bg-rt-primary/10' : 'text-rt-text-muted/30 hover:text-rt-text-muted/50 hover:bg-rt-surface' : 'text-rt-text-muted/20 cursor-not-allowed'}`}>
                  <Icon className="w-4 h-4" />
                  <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-3 py-2 bg-rt-bg-light border border-rt-border rounded-lg text-xs text-rt-text whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-50 shadow-xl">
                    <div className="font-semibold text-rt-text mb-0.5">{displayName}</div>
                    {t.description && <div className="text-rt-text-muted text-[11px] max-w-xs mt-0.5 whitespace-normal">{t.description.length > 60 ? `${t.description.substring(0, 60)}...` : t.description}</div>}
                    {canToggle && <div className="text-rt-text-muted text-[11px] mt-1">{isEnabled ? 'Click to disable' : 'Click to enable'}</div>}
                    <div className="absolute top-full left-1/2 -translate-x-1/2 -mt-px"><div className="w-2 h-2 bg-rt-bg-light border-r border-b border-rt-border rotate-45" /></div>
                  </div>
                </button>
              )
            })}
          </div>
        </div>
      </div>

      {/* Conversations row — horizontal, below the product bar */}
      <div className="px-8 py-2 border-b border-rt-border bg-rt-bg-light/50 flex items-center gap-2 overflow-x-auto">
          <button onClick={handleNewConversation}
            className="flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-rt-primary/10 hover:bg-rt-primary/20 text-rt-primary text-xs font-medium transition-colors">
            <Plus className="w-3.5 h-3.5" /> New
          </button>
          {conversations.map((convo) => (
            <div key={convo.conversation_id}
              className={`group flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg cursor-pointer transition-colors text-xs ${
                activeConversationId === convo.conversation_id
                  ? 'bg-rt-primary/15 text-rt-primary border border-rt-primary/30'
                  : 'bg-rt-surface/50 hover:bg-rt-surface text-rt-text-muted hover:text-rt-text border border-transparent'
              }`}>
              {editingTitle === convo.conversation_id ? (
                <form className="flex items-center gap-1" onSubmit={(e) => { e.preventDefault(); handleRenameConversation(convo.conversation_id) }}>
                  <input value={editTitleValue} onChange={(e) => setEditTitleValue(e.target.value)}
                    className="w-24 bg-rt-surface border border-rt-border rounded px-1.5 py-0.5 text-xs focus:outline-none focus:border-rt-primary"
                    autoFocus onBlur={() => setEditingTitle(null)} onClick={(e) => e.stopPropagation()} />
                </form>
              ) : (
                <>
                  <div className="flex items-center gap-1.5 min-w-0" onClick={() => loadConversation(convo.conversation_id)}>
                    <MessageSquare className="w-3 h-3 flex-shrink-0 opacity-50" />
                    <span className="truncate max-w-[120px]">{convo.title}</span>
                    <span className="text-[10px] opacity-50 flex-shrink-0">({convo.message_count})</span>
                  </div>
                  <div className="flex gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
                    <button onClick={(e) => { e.stopPropagation(); setEditingTitle(convo.conversation_id); setEditTitleValue(convo.title) }}
                      className="p-0.5 rounded hover:bg-rt-border transition-colors" title="Rename"><Edit2 className="w-2.5 h-2.5" /></button>
                    <button onClick={(e) => handleDeleteConversation(convo.conversation_id, e)}
                      className="p-0.5 rounded hover:bg-red-500/20 text-red-400 transition-colors" title="Delete"><Trash2 className="w-2.5 h-2.5" /></button>
                  </div>
                </>
              )}
            </div>
          ))}
          {conversations.length === 0 && <span className="text-[11px] text-rt-text-muted/50 italic">No conversations yet — start typing or click New</span>}
        </div>

      {/* Conversation area */}
      <div className="flex-1 overflow-auto p-8">
        {messages.length === 0 ? (
          <div className="text-center py-16">
            <div className="w-20 h-20 mx-auto mb-6 rounded-2xl bg-gradient-to-br from-purple-500/20 to-blue-500/20 flex items-center justify-center">
              <Bot className="w-10 h-10 text-purple-400" />
            </div>
            <h3 className="text-xl font-display font-semibold mb-2">What would you like the agent to do?</h3>
            <p className="text-rt-text-muted max-w-md mx-auto mb-8">
              The agent can execute tasks using terminal commands, file operations, web search, and screen automation{selectedProductId ? " — all informed by your product's knowledge base" : ''}.
            </p>
            <div className="max-w-xl mx-auto">
              <p className="text-sm text-rt-text-muted mb-3">Try these:</p>
              <div className="flex flex-wrap gap-2 justify-center">
                {['Read the main config file and explain it', 'Search for the latest security patches', 'List all Python files in the project', 'Fix the auth validation in login.py'].map(q => (
                  <button key={q} onClick={() => setTask(q)} className="px-3 py-1.5 rounded-full bg-rt-surface text-sm hover:bg-rt-border transition-colors">{q}</button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <div className="space-y-4 max-w-4xl mx-auto">
            {messageNodes.map((node, nodeIndex) => {
              if (node.kind === 'single') {
                const msg = messages[node.index]
                return (
                  <MessageBubble
                    key={msg.id}
                    message={msg}
                    messages={messages}
                    messageIndex={node.index}
                    showProductActions={!!selectedProductId}
                    onLearnThis={handleLearnThis}
                    onCreateSOP={handleCreateSOP}
                    isCreatingSOP={isCreatingSOP}
                    onApproveSOP={handleApproveSOP}
                    onEditSOP={handleEditSOP}
                    onCreateDoc={handleCreateDoc}
                    isCreatingDoc={isCreatingDoc}
                    onEditDoc={handleEditDoc}
                    onApproveDoc={handleApproveDoc}
                    hasAnswer={messages.some(m => m.type === 'answer') || streamingAnswer !== null}
                  />
                )
              }
              const toolsMsg = messages[node.toolsIndex]
              const lastIndexInBlock = node.childIndices.length > 0 ? node.childIndices[node.childIndices.length - 1] : node.toolsIndex
              const lastToolBlockNodeIndex = messageNodes.reduce((acc, n, i) => (n.kind === 'toolBlock' ? i : acc), -1)
              const answerAfterThisBlock = messages.some((m, idx) => m.type === 'answer' && idx > lastIndexInBlock)
              const streamingForThisBlock = streamingAnswer !== null && nodeIndex === lastToolBlockNodeIndex
              const answerStarted = answerAfterThisBlock || streamingForThisBlock
              const isExpanded = answerStarted ? expandedToolBlocks.has(toolsMsg.id) : true
              return (
                <div key={toolsMsg.id}>
                  <button
                    type="button"
                    onClick={() => toggleToolBlock(toolsMsg.id)}
                    className="flex items-center gap-2 text-xs text-rt-text-muted pl-2 w-full text-left rounded-lg hover:bg-rt-surface/50 transition-colors py-1.5"
                  >
                    <Check className="w-3 h-3 text-rt-success flex-shrink-0" />
                    <span className="flex-1">{toolsMsg.content}</span>
                    <ChevronDown className={`w-3.5 h-3.5 flex-shrink-0 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
                  </button>
                  {isExpanded && node.childIndices.length > 0 && (
                    <div className="space-y-4 mt-4">
                      {node.childIndices.map((idx) => {
                        const m = messages[idx]
                        return (
                          <MessageBubble
                            key={m.id}
                            message={m}
                            messages={messages}
                            messageIndex={idx}
                            showProductActions={!!selectedProductId}
                            onLearnThis={handleLearnThis}
                            onCreateSOP={handleCreateSOP}
                            hasAnswer={messages.some(mm => mm.type === 'answer') || streamingAnswer !== null}
                            isCreatingSOP={isCreatingSOP}
                            onApproveSOP={handleApproveSOP}
                            onEditSOP={handleEditSOP}
                            onCreateDoc={handleCreateDoc}
                            isCreatingDoc={isCreatingDoc}
                            onEditDoc={handleEditDoc}
                            onApproveDoc={handleApproveDoc}
                          />
                        )
                      })}
                    </div>
                  )}
                </div>
              )
            })}
            {streamingAnswer !== null && (
              <div className="card">
                <div className="flex items-center gap-2 mb-3 text-xs text-rt-text-muted"><Bot className="w-3.5 h-3.5 text-purple-400" /><span>Agent Answer</span></div>
                <div className="prose"><ReactMarkdown>{streamingAnswer}</ReactMarkdown></div>
              </div>
            )}
            {streamingSOP !== null && (
              <div className="card border-emerald-500/30 bg-emerald-500/5">
                <div className="flex items-center gap-2 mb-3 text-xs text-emerald-400">
                  <ClipboardList className="w-3.5 h-3.5" />
                  <span className="font-medium">Automation</span>
                  <Loader2 className="w-3 h-3 animate-spin ml-auto" />
                </div>
                <div className="prose prose-sm max-w-none"><ReactMarkdown>{streamingSOP}</ReactMarkdown></div>
              </div>
            )}
            {streamingDoc !== null && (
              <div className="card border-blue-500/30 bg-blue-500/5">
                <div className="flex items-center gap-2 mb-3 text-xs text-blue-400">
                  <FileText className="w-3.5 h-3.5" />
                  <span className="font-medium">Documentation</span>
                  <Loader2 className="w-3 h-3 animate-spin ml-auto" />
                </div>
                <div className="prose prose-sm max-w-none"><ReactMarkdown>{streamingDoc}</ReactMarkdown></div>
              </div>
            )}
            <div ref={scrollRef} />
          </div>
        )}
      </div>

      {/* Input area */}
      <div className="p-8 pt-4 border-t border-rt-border bg-rt-bg-light">
        <form onSubmit={handleSubmit} className="flex gap-4">
          <input type="text" value={task} onChange={e => setTask(e.target.value)}
            placeholder={isRunning ? 'Agent is working...' : 'Describe a task to perform...'}
            className="input flex-1" disabled={isRunning} />
          {isRunning ? (
            <button type="button" onClick={handleStop} className="btn-secondary flex items-center gap-2 text-red-400 border-red-400/30 hover:bg-red-400/10">
              <X className="w-4 h-4" /> Stop
            </button>
          ) : (
            <button type="submit" disabled={!task.trim()} className="btn-primary flex items-center gap-2">
              <Play className="w-4 h-4" /> Go
            </button>
          )}
        </form>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Message bubble with "Learn This" button on answers
// ---------------------------------------------------------------------------

function MessageBubble({ message: msg, messages, messageIndex, showProductActions, onLearnThis, onCreateSOP, isCreatingSOP, onApproveSOP, onEditSOP, onCreateDoc, isCreatingDoc, onEditDoc, onApproveDoc, hasAnswer }: {
  message: AgentMessage; messages: AgentMessage[]; messageIndex: number
  showProductActions?: boolean
  onLearnThis: (question: string, answer: string) => void
  onCreateSOP?: (goal: string) => void
  isCreatingSOP?: boolean
  onApproveSOP?: (sopId: string, scheduleType?: string, scheduleConfig?: Record<string, any>) => Promise<void>
  onEditSOP?: (sopId: string, editInstructions: string) => Promise<{ sop_markdown: string; title: string }>
  onCreateDoc?: (goal: string) => void
  isCreatingDoc?: boolean
  onEditDoc?: (docId: string, editInstructions: string) => Promise<{ doc_markdown: string; title: string }>
  onApproveDoc?: (docId: string) => Promise<void>
  hasAnswer?: boolean
}) {
  const [isLearning, setIsLearning] = useState(false)
  const [learned, setLearned] = useState(false)
  const [sopApproved, setSopApproved] = useState(false)
  const [isApproving, setIsApproving] = useState(false)
  const [showEditInput, setShowEditInput] = useState(false)
  const [editInstructions, setEditInstructions] = useState('')
  const [isEditing, setIsEditing] = useState(false)
  const [showSchedulePicker, setShowSchedulePicker] = useState(false)
  const [docApproved, setDocApproved] = useState(false)
  const [isApprovingDoc, setIsApprovingDoc] = useState(false)
  const [showDocEditInput, setShowDocEditInput] = useState(false)
  const [docEditInstructions, setDocEditInstructions] = useState('')
  const [isEditingDoc, setIsEditingDoc] = useState(false)
  const [activePrompt, setActivePrompt] = useState<'learn' | 'automation' | 'doc' | null>(null)
  const [promptInput, setPromptInput] = useState('')
  const [isExpanded, setIsExpanded] = useState(true)
  const [displayedContent, setDisplayedContent] = useState(msg.content)
  const streamedRef = useRef(false)


  // Streaming effect for answer
  useEffect(() => {
    if (msg.type !== 'answer') {
      setDisplayedContent(msg.content)
      return
    }
    // Skip streaming for loaded (historical) messages
    if (msg.id.startsWith('loaded_')) {
      setDisplayedContent(msg.content)
      return
    }
    if (streamedRef.current) {
      setDisplayedContent(msg.content)
      return
    }
    streamedRef.current = true
    const words = msg.content.split(/(\s+)/)
    let wordIdx = 0
    const interval = setInterval(() => {
      wordIdx += 3
      if (wordIdx >= words.length) {
        setDisplayedContent(msg.content)
        clearInterval(interval)
      } else {
        setDisplayedContent(words.slice(0, wordIdx).join(''))
      }
    }, 25)
    return () => clearInterval(interval)
  }, [msg.content, msg.type, msg.id])

  const getQuestionForAnswer = (): string | null => {
    if (msg.type !== 'answer') return null
    for (let i = messageIndex - 1; i >= 0; i--) {
      if (messages[i].type === 'user') return messages[i].content
    }
    return null
  }

  const handlePromptSubmit = async (e?: React.FormEvent) => {
    if (e) e.preventDefault()
    const originalGoal = getQuestionForAnswer()
    if (!originalGoal) return
    const userInput = promptInput.trim()

    if (activePrompt === 'learn') {
      setIsLearning(true)
      try {
        await onLearnThis(userInput || originalGoal, msg.content)
        setLearned(true)
      } finally {
        setIsLearning(false)
      }
    } else if (activePrompt === 'automation') {
      onCreateSOP?.(userInput || originalGoal)
    } else if (activePrompt === 'doc') {
      onCreateDoc?.(userInput || originalGoal)
    }

    setActivePrompt(null)
    setPromptInput('')
  }

  switch (msg.type) {
    case 'user':
      return (<div className="card bg-rt-primary/5 border-rt-primary/20"><div className="flex items-start gap-2"><span className="text-lg">👤</span><p className="font-medium">{msg.content}</p></div></div>)
    case 'status':
      return (<div className="flex items-center gap-2 text-sm text-rt-text-muted pl-2"><Loader2 className="w-3 h-3 animate-spin" />{msg.content}</div>)
    case 'tools':
      return (<div className="flex items-center gap-2 text-xs text-rt-text-muted pl-2"><Check className="w-3 h-3 text-rt-success" />{msg.content}</div>)
    case 'code':
      return (
        <div className="card bg-rt-surface/50 border-rt-border">
          <div
            className="flex items-center gap-2 text-xs text-rt-text-muted cursor-pointer select-none"
            onClick={() => setIsExpanded(prev => !prev)}
          >
            <ChevronRight className={`w-3.5 h-3.5 transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
            <Code className="w-3.5 h-3.5 text-blue-400" />
            <span>Code Execution {msg.iteration ? `(iteration ${msg.iteration})` : ''}</span>
          </div>
          {isExpanded && (
            <pre className="bg-rt-surface border border-rt-border text-rt-text text-sm p-3 rounded-lg overflow-x-auto font-mono whitespace-pre-wrap mt-2">{msg.content}</pre>
          )}
        </div>)
    case 'output':
      return (
        <div className="card bg-rt-surface/30 border-rt-border">
          <div
            className="flex items-center gap-2 text-xs text-rt-text-muted cursor-pointer select-none"
            onClick={() => setIsExpanded(prev => !prev)}
          >
            <ChevronRight className={`w-3.5 h-3.5 transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
            <Terminal className="w-3.5 h-3.5 text-yellow-400" />
            <span>Output</span>
          </div>
          {isExpanded && (
            <pre className="bg-rt-surface border border-rt-border text-rt-text text-xs p-3 rounded-lg overflow-x-auto font-mono whitespace-pre-wrap max-h-60 overflow-y-auto mt-2">{msg.content}</pre>
          )}
        </div>)
    case 'answer':
      return (
        <div className="card">
          <div className="flex items-center gap-2 mb-3 text-xs text-rt-text-muted"><Bot className="w-3.5 h-3.5 text-purple-400" /><span>Agent Answer</span></div>
          <div className="prose"><ReactMarkdown>{msg.content}</ReactMarkdown></div>
          {showProductActions !== false && (
            <div className="mt-3 pt-2 border-t border-rt-border/50 flex flex-wrap items-center gap-2">
              {learned ? (
                <span className="inline-flex items-center gap-1.5 text-xs text-rt-success"><CheckCircle2 className="w-3.5 h-3.5" />Added to knowledge base</span>
              ) : (
                <button
                  onClick={() => { setActivePrompt(activePrompt === 'learn' ? null : 'learn'); setPromptInput('') }}
                  disabled={isLearning || !getQuestionForAnswer()}
                  className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                    activePrompt === 'learn'
                      ? 'bg-amber-500/25 text-amber-300 ring-1 ring-amber-500/40'
                      : 'bg-amber-500/10 text-amber-400 hover:bg-amber-500/20'
                  }`}
                  title="Train this Q&A as expert knowledge — it will be prioritized in future searches"
                >
                  {isLearning ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <GraduationCap className="w-3.5 h-3.5" />}
                  Learn This
                </button>
              )}
              <button
                onClick={() => { setActivePrompt(activePrompt === 'automation' ? null : 'automation'); setPromptInput('') }}
                disabled={isCreatingSOP || !getQuestionForAnswer() || !onCreateSOP}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                  activePrompt === 'automation'
                    ? 'bg-emerald-500/25 text-emerald-300 ring-1 ring-emerald-500/40'
                    : 'bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20'
                }`}
                title="Build an automation workflow from this Q&A"
              >
                {isCreatingSOP ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ClipboardList className="w-3.5 h-3.5" />}
                Build Automation
              </button>
              <button
                onClick={() => { setActivePrompt(activePrompt === 'doc' ? null : 'doc'); setPromptInput('') }}
                disabled={isCreatingDoc || !getQuestionForAnswer() || !onCreateDoc}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                  activePrompt === 'doc'
                    ? 'bg-blue-500/25 text-blue-300 ring-1 ring-blue-500/40'
                    : 'bg-blue-500/10 text-blue-400 hover:bg-blue-500/20'
                }`}
                title="Build a knowledge article from this Q&A"
              >
                {isCreatingDoc ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FileText className="w-3.5 h-3.5" />}
                Build Documentation
              </button>
            </div>
          )}
          {showProductActions !== false && activePrompt && (
            <div className="mt-2 pt-2 border-t border-rt-border/30">
              <p className="text-xs text-rt-text-muted mb-1.5">
                {activePrompt === 'learn' && 'Optionally refine the question before saving to knowledge base:'}
                {activePrompt === 'automation' && 'Optionally describe what the automation should focus on:'}
                {activePrompt === 'doc' && 'Optionally describe what the documentation should cover:'}
              </p>
              <form onSubmit={(e) => e.preventDefault()} className="flex gap-2 items-end">
                <textarea
                  value={promptInput}
                  onChange={(e) => setPromptInput(e.target.value)}
                  placeholder="Leave empty to use defaults..."
                  rows={2}
                  className="input flex-1 text-sm min-h-[2.5rem] resize-y"
                  disabled={isLearning || isCreatingSOP || isCreatingDoc}
                  autoFocus
                />
                <button
                  type="button"
                  onClick={() => handlePromptSubmit()}
                  disabled={isLearning || isCreatingSOP || isCreatingDoc}
                  className={`px-3 py-1.5 rounded-lg text-xs font-medium flex items-center gap-1.5 transition-colors disabled:opacity-50 ${
                    activePrompt === 'learn'
                      ? 'bg-amber-500/15 text-amber-400 hover:bg-amber-500/25'
                      : activePrompt === 'automation'
                        ? 'bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25'
                        : 'bg-blue-500/15 text-blue-400 hover:bg-blue-500/25'
                  }`}
                >
                  {(isLearning || isCreatingSOP || isCreatingDoc) ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                  Go
                </button>
                <button
                  type="button"
                  onClick={() => { setActivePrompt(null); setPromptInput('') }}
                  disabled={isLearning || isCreatingSOP || isCreatingDoc}
                  className="px-3 py-1.5 rounded-lg text-xs font-medium text-rt-text-muted hover:bg-rt-surface transition-colors"
                >
                  Cancel
                </button>
              </form>
            </div>
          )}
        </div>)
    case 'sop':
      return (
        <div className="card border-emerald-500/30 bg-emerald-500/5">
          <div className="flex items-center gap-2 mb-3 text-xs text-emerald-400">
            <ClipboardList className="w-3.5 h-3.5" />
            <span className="font-medium">Automation</span>
            {msg.meta?.title && <span className="text-rt-text-muted">— {msg.meta.title}</span>}
            {sopApproved && <span className="ml-auto inline-flex items-center gap-1 text-rt-success"><CheckCircle2 className="w-3.5 h-3.5" />Approved</span>}
          </div>
          <div className="prose prose-sm max-w-none">
            <ReactMarkdown>{msg.content}</ReactMarkdown>
          </div>
          <div className="mt-3 pt-2 border-t border-emerald-500/20 flex flex-wrap items-center gap-2">
            {sopApproved ? (
              <span className="inline-flex items-center gap-1.5 text-xs text-rt-success"><CheckCircle2 className="w-3.5 h-3.5" />Approved{msg.meta?.schedule_type && msg.meta.schedule_type !== 'none' && ` — ${_scheduleLabel(msg.meta.schedule_type, msg.meta.schedule_config)}`}</span>
            ) : showSchedulePicker ? (
              <SchedulePicker
                onConfirm={async (scheduleType, scheduleConfig) => {
                  if (!msg.meta?.sop_id || !onApproveSOP) return
                  setIsApproving(true)
                  try {
                    await onApproveSOP(msg.meta.sop_id, scheduleType, scheduleConfig)
                    setSopApproved(true)
                    setShowSchedulePicker(false)
                  } catch { /* toast already shown */ }
                  finally { setIsApproving(false) }
                }}
                onCancel={() => setShowSchedulePicker(false)}
                isApproving={isApproving}
              />
            ) : (
              <button
                onClick={() => setShowSchedulePicker(true)}
                disabled={isApproving || !msg.meta?.sop_id || !onApproveSOP}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                title="Approve and schedule this automation"
              >
                {isApproving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
                Approve Automation
              </button>
            )}
            {!showSchedulePicker && (
              <button
                onClick={() => setShowEditInput(!showEditInput)}
                disabled={isEditing || sopApproved}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                title="Edit this automation"
              >
                <Edit2 className="w-3.5 h-3.5" />
                Edit Automation
              </button>
            )}
          </div>
          {showEditInput && (
            <div className="mt-3 pt-2 border-t border-emerald-500/20">
              <p className="text-xs text-rt-text-muted mb-2">Describe the changes to make:</p>
              <form
                onSubmit={(e) => e.preventDefault()}
                className="flex gap-2 items-end"
              >
                <textarea
                  value={editInstructions}
                  onChange={(e) => setEditInstructions(e.target.value)}
                  placeholder="e.g. Add a rollback step after step 3..."
                  rows={2}
                  className="input flex-1 text-sm min-h-[2.5rem] resize-y"
                  disabled={isEditing}
                  autoFocus
                />
                <button
                  type="button"
                  onClick={async () => {
                    if (!editInstructions.trim() || !msg.meta?.sop_id || !onEditSOP) return
                    setIsEditing(true)
                    try {
                      await onEditSOP(msg.meta.sop_id, editInstructions.trim())
                      setEditInstructions('')
                      setShowEditInput(false)
                    } catch (err: any) {
                      toast.error(err?.response?.data?.detail || 'Failed to edit automation')
                    } finally {
                      setIsEditing(false)
                    }
                  }}
                  disabled={isEditing || !editInstructions.trim()}
                  className="btn-primary text-xs px-3 py-1.5 flex items-center gap-1.5"
                >
                  {isEditing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                  Go
                </button>
                <button
                  type="button"
                  onClick={() => { setShowEditInput(false); setEditInstructions('') }}
                  disabled={isEditing}
                  className="btn-secondary text-xs px-3 py-1.5"
                >
                  Cancel
                </button>
              </form>
            </div>
          )}
        </div>)
    case 'doc':
      return (
        <div className="card border-blue-500/30 bg-blue-500/5">
          <div className="flex items-center gap-2 mb-3 text-xs text-blue-400">
            <FileText className="w-3.5 h-3.5" />
            <span className="font-medium">Documentation</span>
            {msg.meta?.title && <span className="text-rt-text-muted">— {msg.meta.title}</span>}
          </div>
          <div className="prose prose-sm max-w-none">
            <ReactMarkdown>{msg.content}</ReactMarkdown>
          </div>
          <div className="mt-3 pt-2 border-t border-blue-500/20 flex flex-wrap items-center gap-2">
            {!docApproved && (
              <button
                onClick={async () => {
                  if (!msg.meta?.doc_id || !onApproveDoc) return
                  setIsApprovingDoc(true)
                  try {
                    await onApproveDoc(msg.meta.doc_id)
                    setDocApproved(true)
                  } finally {
                    setIsApprovingDoc(false)
                  }
                }}
                disabled={isApprovingDoc || !msg.meta?.doc_id || !onApproveDoc}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                title="Approve this documentation"
              >
                {isApprovingDoc ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
                Approve Documentation
              </button>
            )}
            {docApproved && (
              <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-emerald-500/10 text-emerald-400">
                <CheckCircle2 className="w-3.5 h-3.5" />
                Approved
              </span>
            )}
            <button
              onClick={() => setShowDocEditInput(!showDocEditInput)}
              disabled={isEditingDoc}
              className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              title="Edit this documentation"
            >
              <Edit2 className="w-3.5 h-3.5" />
              Edit Documentation
            </button>
          </div>
          {showDocEditInput && (
            <div className="mt-3 pt-2 border-t border-blue-500/20">
              <p className="text-xs text-rt-text-muted mb-2">Describe the changes to make:</p>
              <form
                onSubmit={(e) => e.preventDefault()}
                className="flex gap-2 items-end"
              >
                <textarea
                  value={docEditInstructions}
                  onChange={(e) => setDocEditInstructions(e.target.value)}
                  placeholder="e.g. Add a troubleshooting section..."
                  rows={2}
                  className="input flex-1 text-sm min-h-[2.5rem] resize-y"
                  disabled={isEditingDoc}
                  autoFocus
                />
                <button
                  type="button"
                  onClick={async () => {
                    if (!docEditInstructions.trim() || !msg.meta?.doc_id || !onEditDoc) return
                    setIsEditingDoc(true)
                    try {
                      await onEditDoc(msg.meta.doc_id, docEditInstructions.trim())
                      setDocEditInstructions('')
                      setShowDocEditInput(false)
                    } catch (err: any) {
                      toast.error(err?.response?.data?.detail || 'Failed to edit documentation')
                    } finally {
                      setIsEditingDoc(false)
                    }
                  }}
                  disabled={isEditingDoc || !docEditInstructions.trim()}
                  className="btn-primary text-xs px-3 py-1.5 flex items-center gap-1.5"
                >
                  {isEditingDoc ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                  Go
                </button>
                <button
                  type="button"
                  onClick={() => { setShowDocEditInput(false); setDocEditInstructions('') }}
                  disabled={isEditingDoc}
                  className="btn-secondary text-xs px-3 py-1.5"
                >
                  Cancel
                </button>
              </form>
            </div>
          )}
        </div>)
    case 'done':
      return (<div className="flex items-center gap-2 text-xs text-rt-success pl-2"><Check className="w-3 h-3" />{msg.content}{msg.meta?.total_ms && <span className="text-rt-text-muted">({msg.meta.total_ms}ms)</span>}</div>)
    case 'error':
      return (<div className="card bg-red-500/5 border-red-500/20"><div className="flex items-center gap-2"><AlertTriangle className="w-4 h-4 text-red-400" /><p className="text-sm text-red-400">{msg.content}</p></div></div>)
    default:
      return null
  }
}

// ---------------------------------------------------------------------------
// Schedule label helper
// ---------------------------------------------------------------------------
function _scheduleLabel(type: string, config?: Record<string, any>): string {
  if (!type || type === 'none') return ''
  if (type === 'once') return `One-time: ${config?.run_at ? new Date(config.run_at).toLocaleString() : 'scheduled'}`
  if (type === 'interval') return `Every ${config?.every || '?'} ${config?.unit || 'minutes'}`
  if (type === 'daily') return `Daily at ${config?.time || '09:00'}`
  if (type === 'weekly') return `Weekly on ${(config?.days || []).join(', ')} at ${config?.time || '09:00'}`
  if (type === 'monthly') return `Monthly on day ${config?.day_of_month || 1} at ${config?.time || '09:00'}`
  if (type === 'cron') return `Cron: ${config?.expression || ''}`
  return type
}

// ---------------------------------------------------------------------------
// Schedule Picker component
// ---------------------------------------------------------------------------
const COMMON_TIMEZONES = [
  'UTC', 'US/Eastern', 'US/Central', 'US/Mountain', 'US/Pacific',
  'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Asia/Tokyo',
  'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dubai', 'Australia/Sydney',
  'Pacific/Auckland', 'America/Sao_Paulo', 'America/Chicago',
  'America/Los_Angeles', 'America/New_York', 'Canada/Eastern',
]

function SchedulePicker({ onConfirm, onCancel, isApproving }: {
  onConfirm: (scheduleType: string, scheduleConfig?: Record<string, any>) => void
  onCancel: () => void
  isApproving: boolean
}) {
  const [scheduleType, setScheduleType] = useState('none')
  const [timezone, setTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone)
  const [intervalEvery, setIntervalEvery] = useState(30)
  const [intervalUnit, setIntervalUnit] = useState('minutes')
  const [dailyTime, setDailyTime] = useState('09:00')
  const [weeklyDays, setWeeklyDays] = useState<string[]>(['monday'])
  const [weeklyTime, setWeeklyTime] = useState('09:00')
  const [monthlyDay, setMonthlyDay] = useState(1)
  const [monthlyTime, setMonthlyTime] = useState('09:00')
  const [onceDate, setOnceDate] = useState('')
  const [cronExpr, setCronExpr] = useState('0 9 * * 1-5')

  const toggleDay = (day: string) => {
    setWeeklyDays(prev => prev.includes(day) ? prev.filter(d => d !== day) : [...prev, day])
  }

  const buildConfig = (): Record<string, any> | undefined => {
    const tz = timezone
    switch (scheduleType) {
      case 'once': return { run_at: onceDate || new Date(Date.now() + 3600000).toISOString(), timezone: tz }
      case 'interval': return { every: intervalEvery, unit: intervalUnit, timezone: tz }
      case 'daily': return { time: dailyTime, timezone: tz }
      case 'weekly': return { days: weeklyDays, time: weeklyTime, timezone: tz }
      case 'monthly': return { day_of_month: monthlyDay, time: monthlyTime, timezone: tz }
      case 'cron': return { expression: cronExpr, timezone: tz }
      default: return undefined
    }
  }

  const scheduleOptions = [
    { value: 'none', label: 'No Schedule', desc: 'Approve without scheduling', icon: '✓' },
    { value: 'once', label: 'One-Time', desc: 'Run once at a specific time', icon: '📅' },
    { value: 'interval', label: 'Interval', desc: 'Run every N minutes/hours/days', icon: '⏱' },
    { value: 'daily', label: 'Daily', desc: 'Run every day at a set time', icon: '🔄' },
    { value: 'weekly', label: 'Weekly', desc: 'Run on specific days of the week', icon: '📆' },
    { value: 'monthly', label: 'Monthly', desc: 'Run on a specific day each month', icon: '🗓' },
    { value: 'cron', label: 'Custom (Cron)', desc: 'Advanced cron expression', icon: '⚙' },
  ]

  const allDays = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

  return (
    <div className="w-full">
      <div className="flex items-center gap-2 mb-3">
        <CalendarClock className="w-4 h-4 text-emerald-400" />
        <span className="text-sm font-semibold text-emerald-400">Approve & Schedule</span>
      </div>

      {/* Schedule type cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5 mb-3">
        {scheduleOptions.map(opt => (
          <button
            key={opt.value}
            type="button"
            onClick={() => setScheduleType(opt.value)}
            className={`p-2 rounded-lg border text-left transition-all text-xs ${
              scheduleType === opt.value
                ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400'
                : 'border-rt-border hover:border-rt-text-muted/30 text-rt-text-muted'
            }`}
          >
            <div className="flex items-center gap-1.5 mb-0.5">
              <span>{opt.icon}</span>
              <span className="font-medium">{opt.label}</span>
            </div>
            <p className="text-[10px] opacity-70 leading-tight">{opt.desc}</p>
          </button>
        ))}
      </div>

      {/* Schedule config based on type */}
      {scheduleType === 'once' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Run at:</label>
          <input type="datetime-local" value={onceDate} onChange={e => setOnceDate(e.target.value)}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500 flex-1" />
        </div>
      )}

      {scheduleType === 'interval' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Every</label>
          <input type="number" min={1} value={intervalEvery} onChange={e => setIntervalEvery(Number(e.target.value))}
            className="w-16 bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs text-center focus:outline-none focus:border-emerald-500" />
          <select value={intervalUnit} onChange={e => setIntervalUnit(e.target.value)}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500">
            <option value="minutes">Minutes</option>
            <option value="hours">Hours</option>
            <option value="days">Days</option>
          </select>
        </div>
      )}

      {scheduleType === 'daily' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Every day at:</label>
          <input type="time" value={dailyTime} onChange={e => setDailyTime(e.target.value)}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500" />
        </div>
      )}

      {scheduleType === 'weekly' && (
        <div className="mb-3">
          <div className="flex flex-wrap gap-1 mb-2">
            {allDays.map(day => (
              <button key={day} type="button" onClick={() => toggleDay(day)}
                className={`px-2 py-1 rounded-md text-[10px] font-medium transition-colors ${
                  weeklyDays.includes(day)
                    ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                    : 'bg-rt-surface text-rt-text-muted border border-rt-border hover:border-rt-text-muted/30'
                }`}>
                {day.charAt(0).toUpperCase() + day.slice(1, 3)}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-rt-text-muted">At:</label>
            <input type="time" value={weeklyTime} onChange={e => setWeeklyTime(e.target.value)}
              className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500" />
          </div>
        </div>
      )}

      {scheduleType === 'monthly' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Day</label>
          <input type="number" min={1} max={28} value={monthlyDay} onChange={e => setMonthlyDay(Number(e.target.value))}
            className="w-14 bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs text-center focus:outline-none focus:border-emerald-500" />
          <label className="text-xs text-rt-text-muted">at</label>
          <input type="time" value={monthlyTime} onChange={e => setMonthlyTime(e.target.value)}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500" />
        </div>
      )}

      {scheduleType === 'cron' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Cron:</label>
          <input type="text" value={cronExpr} onChange={e => setCronExpr(e.target.value)}
            placeholder="0 9 * * 1-5"
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500 flex-1 font-mono" />
          <span className="text-[10px] text-rt-text-muted/60">min hr dom mon dow</span>
        </div>
      )}

      {/* Timezone */}
      {scheduleType !== 'none' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Timezone:</label>
          <select value={timezone} onChange={e => setTimezone(e.target.value)}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500">
            {COMMON_TIMEZONES.map(tz => <option key={tz} value={tz}>{tz}</option>)}
          </select>
        </div>
      )}

      {/* Action buttons */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => onConfirm(scheduleType, buildConfig())}
          disabled={isApproving}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25 transition-colors disabled:opacity-50"
        >
          {isApproving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
          {scheduleType === 'none' ? 'Approve' : 'Approve & Schedule'}
        </button>
        <button
          onClick={onCancel}
          disabled={isApproving}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-rt-text-muted hover:bg-rt-surface transition-colors"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}
