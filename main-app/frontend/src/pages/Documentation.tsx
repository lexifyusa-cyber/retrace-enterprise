import { useState, useEffect, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  FileText, Loader2, Trash2,
  Package, ChevronDown, ChevronRight, Send,
  Edit2, CheckCircle2, Tag, Layers,
} from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import toast from 'react-hot-toast'
import { teamApi, agentApi, AGENT_GENERAL_SCOPE } from '@/utils/api'
import { Team } from '@/types'

export default function Documentation() {
  const queryClient = useQueryClient()
  const [docsByTeam, setDocsByTeam] = useState<Record<string, any[]>>({})
  const [isLoading, setIsLoading] = useState(false)
  const [expandedTeams, setExpandedTeams] = useState<Set<string>>(new Set())
  const [expandedDocId, setExpandedDocId] = useState<string | null>(null)
  const [streamingDoc, setStreamingDoc] = useState<string | null>(null)
  const [editingDocId, setEditingDocId] = useState<string | null>(null)
  const [editInstructions, setEditInstructions] = useState('')
  const [isEditing, setIsEditing] = useState(false)

  const { data: teams = [] } = useQuery({
    queryKey: ['teams'],
    queryFn: () => teamApi.list(),
  })

  const trainedTeams = teams.filter((p: Team) =>
    p.folder_groups?.some(g => g.training_status === 'completed') === true
  )

  const loadAllDocs = useCallback(async () => {
    setIsLoading(true)
    try {
      const results: Record<string, any[]> = {}
      try {
        const general = await agentApi.listDocs(AGENT_GENERAL_SCOPE)
        results[AGENT_GENERAL_SCOPE] = Array.isArray(general) ? general : []
      } catch {
        results[AGENT_GENERAL_SCOPE] = []
      }
      await Promise.all(
        trainedTeams.map(async (p: Team) => {
          try {
            const docs = await agentApi.listDocs(p.team_id)
            results[p.team_id] = Array.isArray(docs) ? docs : []
          } catch {
            results[p.team_id] = []
          }
        })
      )
      setDocsByTeam(results)
    } catch {
      toast.error('Failed to load documentation')
    } finally {
      setIsLoading(false)
    }
  }, [trainedTeams.map(p => p.team_id).join(',')])

  useEffect(() => {
    loadAllDocs()
  }, [loadAllDocs])

  const handleDelete = useCallback(async (docId: string, teamId: string) => {
    try {
      await agentApi.deleteDoc(docId)
      setDocsByTeam(prev => ({
        ...prev,
        [teamId]: (prev[teamId] || []).filter(d => d.doc_id !== docId),
      }))
      queryClient.invalidateQueries({ queryKey: ['dashboard-stats'] })
      toast.success('Documentation deleted')
    } catch {
      toast.error('Failed to delete documentation')
    }
  }, [queryClient])

  const handleApproveDoc = useCallback(async (docId: string, teamId: string) => {
    try {
      const updated = await agentApi.approveDoc(docId)
      setDocsByTeam(prev => ({
        ...prev,
        [teamId]: (prev[teamId] || []).map(d => d.doc_id === docId ? { ...d, status: updated.status } : d),
      }))
      queryClient.invalidateQueries({ queryKey: ['dashboard-stats'] })
      toast.success('Documentation approved')
    } catch {
      toast.error('Failed to approve documentation')
    }
  }, [])

  const handleEditDoc = useCallback(async (docId: string, teamId: string) => {
    if (!editInstructions.trim()) return
    setIsEditing(true)
    try {
      const response = await fetch('/api/v1/agent/edit-doc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ doc_id: docId, edit_instructions: editInstructions.trim() }),
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
                throw new Error(eventData.detail || 'Edit failed')
              }
            } catch (e) { if (e instanceof Error && e.message !== 'Edit failed') { /* ignore parse errors */ } else throw e }
            currentEvent = ''
          }
        }
      }
      setDocsByTeam(prev => ({
        ...prev,
        [teamId]: (prev[teamId] || []).map(d =>
          d.doc_id === docId ? { ...d, doc_markdown: editResult.doc_markdown, title: editResult.title } : d
        ),
      }))
      setEditingDocId(null)
      setEditInstructions('')
      toast.success('Documentation updated!')
    } catch (err: any) {
      toast.error(err?.message || 'Failed to edit documentation')
      setStreamingDoc(null)
    } finally {
      setIsEditing(false)
    }
  }, [editInstructions])

  const generalDocsList = docsByTeam[AGENT_GENERAL_SCOPE] || []
  const displayDocSections: { sectionId: string; sectionName: string; isGeneral: boolean }[] = []
  if (generalDocsList.length > 0) {
    displayDocSections.push({ sectionId: AGENT_GENERAL_SCOPE, sectionName: 'General', isGeneral: true })
  }
  for (const p of trainedTeams) {
    if ((docsByTeam[p.team_id] || []).length > 0) {
      displayDocSections.push({ sectionId: p.team_id, sectionName: p.team_name, isGeneral: false })
    }
  }
  const totalDocs =
    generalDocsList.length +
    trainedTeams.reduce((sum, p) => sum + (docsByTeam[p.team_id] || []).length, 0)
  const sectionCount = (generalDocsList.length > 0 ? 1 : 0) + trainedTeams.filter((p: Team) => (docsByTeam[p.team_id] || []).length > 0).length

  const toggleTeam = (teamId: string) => {
    setExpandedTeams(prev => {
      const next = new Set(prev)
      if (next.has(teamId)) next.delete(teamId)
      else next.add(teamId)
      return next
    })
  }

  const handleExpand = (docId: string) => {
    setExpandedDocId(expandedDocId === docId ? null : docId)
    setEditingDocId(null)
    setEditInstructions('')
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <motion.div
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="p-8 pb-4 border-b border-rt-border"
      >
        <div className="flex items-center gap-3 mb-2">
          <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-500/20 to-indigo-500/20 flex items-center justify-center">
            <FileText className="w-5 h-5 text-blue-400" />
          </div>
          <div>
            <h1 className="text-3xl font-display font-bold">Documentation</h1>
            <p className="text-rt-text-muted text-sm">Knowledge articles generated from agent conversations</p>
          </div>
        </div>
      </motion.div>

      {/* Sub-header */}
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.05 }}
        className="px-8 py-3 border-b border-rt-border bg-rt-bg-light flex items-center gap-4"
      >
        <span className="text-xs text-rt-text-muted">
          {totalDocs} article{totalDocs !== 1 ? 's' : ''}
          {sectionCount > 0 ? ` · ${sectionCount} section${sectionCount !== 1 ? 's' : ''}` : ''}
        </span>
      </motion.div>

      {/* Grouped documentation list */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.1 }}
        className="flex-1 overflow-auto p-8"
      >
        {isLoading ? (
          <div className="flex items-center justify-center gap-2 text-rt-text-muted py-16">
            <Loader2 className="w-5 h-5 animate-spin" /> Loading documentation...
          </div>
        ) : displayDocSections.length === 0 ? (
          <div className="text-center py-16">
            <FileText className="w-12 h-12 mx-auto mb-4 text-rt-text-muted/30" />
            <p className="text-rt-text-muted max-w-md mx-auto">
              No documentation yet. In the Agent, get an answer then use <strong>Build Documentation</strong> — with a team selected or under <strong>General</strong> when no team is selected.
            </p>
          </div>
        ) : (
          <div className="max-w-6xl mx-auto space-y-6">
            {displayDocSections.map((section) => {
              const teamDocs = docsByTeam[section.sectionId] || []
              const isExpanded = expandedTeams.has(section.sectionId)
              return (
                <div key={section.sectionId}>
                  {/* Team group header */}
                  <button
                    type="button"
                    onClick={() => toggleTeam(section.sectionId)}
                    className="w-full flex items-center gap-3 px-4 py-2.5 rounded-lg hover:bg-rt-surface/50 transition-colors mb-2"
                  >
                    <ChevronDown className={`w-4 h-4 text-rt-text-muted transition-transform ${isExpanded ? '' : '-rotate-90'}`} />
                    {section.isGeneral ? (
                      <Layers className="w-4 h-4 text-amber-400" />
                    ) : (
                      <Package className="w-4 h-4 text-rt-primary" />
                    )}
                    <span className="text-sm font-semibold">{section.sectionName}</span>
                    <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-rt-surface text-rt-text-muted">
                      {teamDocs.length}
                    </span>
                  </button>

                  {/* Team items */}
                  <AnimatePresence>
                    {isExpanded && (
                      <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="overflow-hidden"
                      >
                        <div className="pl-4 space-y-3">
                          {teamDocs.map(doc => (
                            <div key={doc.doc_id} className="rounded-xl border border-rt-border bg-rt-surface/20 overflow-hidden hover:border-rt-border/80 transition-colors">
                              <div
                                className="flex items-center gap-4 px-5 py-3.5 cursor-pointer hover:bg-rt-surface/30 transition-colors"
                                onClick={() => handleExpand(doc.doc_id)}
                              >
                                <FileText className="w-5 h-5 text-blue-400 flex-shrink-0" />
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center gap-2 mb-0.5 flex-wrap">
                                    <span className="text-sm font-semibold truncate">{doc.title}</span>
                                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-blue-500/15 text-blue-400 flex-shrink-0">
                                      <FileText className="w-2.5 h-2.5" />
                                      {doc.doc_type === 'knowledge-article' ? 'Knowledge Article' : doc.doc_type}
                                    </span>
                                    {(doc.status || 'draft') === 'approved' ? (
                                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/15 text-emerald-400 flex-shrink-0">
                                        <CheckCircle2 className="w-2.5 h-2.5" />
                                        Approved
                                      </span>
                                    ) : (
                                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-rt-surface text-rt-text-muted flex-shrink-0">
                                        Draft
                                      </span>
                                    )}
                                  </div>
                                  <div className="flex items-center gap-2 flex-wrap">
                                    <p className="text-xs text-rt-text-muted truncate">{doc.goal}</p>
                                    {doc.doc_json?.tags && doc.doc_json.tags.length > 0 && (
                                      <div className="flex items-center gap-1">
                                        {doc.doc_json.tags.slice(0, 4).map((tag: string) => (
                                          <span key={tag} className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[9px] font-medium bg-rt-surface text-rt-text-muted">
                                            <Tag className="w-2 h-2" />{tag}
                                          </span>
                                        ))}
                                        {doc.doc_json.tags.length > 4 && (
                                          <span className="text-[9px] text-rt-text-muted/60">+{doc.doc_json.tags.length - 4}</span>
                                        )}
                                      </div>
                                    )}
                                  </div>
                                </div>
                                <div className="flex items-center gap-1.5 flex-shrink-0">
                                  <span className="text-[10px] text-rt-text-muted/60 mr-2">
                                    {doc.created_at ? new Date(doc.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : ''}
                                  </span>
                                  {(doc.status || 'draft') !== 'approved' && (
                                    <button
                                      onClick={(e) => { e.stopPropagation(); handleApproveDoc(doc.doc_id, section.sectionId) }}
                                      className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs font-medium bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors"
                                      title="Approve this documentation"
                                    >
                                      <CheckCircle2 className="w-3.5 h-3.5" />
                                      Approve
                                    </button>
                                  )}
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      setEditingDocId(editingDocId === doc.doc_id ? null : doc.doc_id)
                                      setEditInstructions('')
                                    }}
                                    className="p-1.5 rounded-lg hover:bg-blue-500/15 text-blue-400/60 hover:text-blue-400 transition-colors"
                                    title="Edit documentation"
                                  >
                                    <Edit2 className="w-4 h-4" />
                                  </button>
                                  <button
                                    onClick={(e) => { e.stopPropagation(); handleDelete(doc.doc_id, section.sectionId) }}
                                    className="p-1.5 rounded-lg hover:bg-red-500/15 text-red-400/60 hover:text-red-400 transition-colors"
                                    title="Delete documentation"
                                  >
                                    <Trash2 className="w-4 h-4" />
                                  </button>
                                  <ChevronRight className={`w-4 h-4 text-rt-text-muted transition-transform ${expandedDocId === doc.doc_id ? 'rotate-90' : ''}`} />
                                </div>
                              </div>

                              {editingDocId === doc.doc_id && !streamingDoc && (
                                <div className="border-t border-rt-border/50 px-5 pb-4 pt-3">
                                  <p className="text-xs text-rt-text-muted mb-2">Describe the changes to make:</p>
                                  <div className="flex gap-2 items-end">
                                    <textarea
                                      value={editInstructions}
                                      onChange={(e) => setEditInstructions(e.target.value)}
                                      placeholder="e.g. Add a troubleshooting section..."
                                      rows={2}
                                      className="input flex-1 text-sm min-h-[2.5rem] resize-y"
                                      disabled={isEditing}
                                      autoFocus
                                    />
                                    <button
                                      type="button"
                                      onClick={() => handleEditDoc(doc.doc_id, section.sectionId)}
                                      disabled={isEditing || !editInstructions.trim()}
                                      className="btn-primary text-xs px-3 py-1.5 flex items-center gap-1.5"
                                    >
                                      {isEditing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                                      Go
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => { setEditingDocId(null); setEditInstructions('') }}
                                      disabled={isEditing}
                                      className="btn-secondary text-xs px-3 py-1.5"
                                    >
                                      Cancel
                                    </button>
                                  </div>
                                </div>
                              )}

                              <AnimatePresence>
                                {(expandedDocId === doc.doc_id || (streamingDoc !== null && editingDocId === doc.doc_id)) && (
                                  <motion.div
                                    initial={{ height: 0, opacity: 0 }}
                                    animate={{ height: 'auto', opacity: 1 }}
                                    exit={{ height: 0, opacity: 0 }}
                                    className="overflow-hidden"
                                  >
                                    <div className="border-t border-rt-border/50 px-5 pb-4 pt-4">
                                      {streamingDoc !== null && editingDocId === doc.doc_id ? (
                                        <div className="prose prose-sm max-w-none">
                                          <ReactMarkdown>{streamingDoc}</ReactMarkdown>
                                        </div>
                                      ) : (
                                        <div className="prose prose-sm max-w-none">
                                          <ReactMarkdown>{doc.doc_markdown}</ReactMarkdown>
                                        </div>
                                      )}
                                    </div>
                                  </motion.div>
                                )}
                              </AnimatePresence>
                            </div>
                          ))}
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              )
            })}
          </div>
        )}
      </motion.div>
    </div>
  )
}
