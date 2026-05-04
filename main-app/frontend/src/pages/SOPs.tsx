import { useState, useEffect, useCallback, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ClipboardList, Loader2, Trash2, CheckCircle2, Clock,
  Package, ChevronDown, ChevronRight, Play, Send,
  CalendarClock, Edit2, History, XCircle, Timer, Layers,
} from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import toast from 'react-hot-toast'
import { teamApi, agentApi, AGENT_GENERAL_SCOPE } from '@/utils/api'
import { Team } from '@/types'

export default function SOPs() {
  const queryClient = useQueryClient()
  const [sopsByTeam, setSopsByTeam] = useState<Record<string, any[]>>({})
  const [isLoading, setIsLoading] = useState(false)
  const [expandedTeams, setExpandedTeams] = useState<Set<string>>(new Set())
  const [expandedSopId, setExpandedSopId] = useState<string | null>(null)
  const [expandedSection, setExpandedSection] = useState<'steps' | 'logs' | 'schedule' | null>('steps')
  const [runLogs, setRunLogs] = useState<Record<string, any[]>>({})
  const [loadingLogs, setLoadingLogs] = useState<string | null>(null)
  const [runningManual, setRunningManual] = useState<string | null>(null)
  const [showLogsDropdown, setShowLogsDropdown] = useState<string | null>(null)

  // Inline edit (gt2-style: form below row, no expand)
  const [editingSopId, setEditingSopId] = useState<string | null>(null)
  const [editInstructions, setEditInstructions] = useState('')
  const [isEditingSop, setIsEditingSop] = useState(false)
  const [streamingSop, setStreamingSop] = useState<string | null>(null)

  // Schedule edit state
  const [editingScheduleId, setEditingScheduleId] = useState<string | null>(null)
  const [scheduleType, setScheduleType] = useState('none')
  const [scheduleConfig, setScheduleConfig] = useState<Record<string, any>>({})

  const { data: teams = [] } = useQuery({
    queryKey: ['teams'],
    queryFn: () => teamApi.list(),
  })

  const trainedTeams = teams.filter((p: Team) =>
    p.folder_groups?.some(g => g.training_status === 'completed') === true
  )

  const loadAllSOPs = useCallback(async () => {
    setIsLoading(true)
    try {
      const results: Record<string, any[]> = {}
      try {
        const general = await agentApi.listSOPs(AGENT_GENERAL_SCOPE)
        results[AGENT_GENERAL_SCOPE] = Array.isArray(general) ? general : []
      } catch {
        results[AGENT_GENERAL_SCOPE] = []
      }
      await Promise.all(
        trainedTeams.map(async (p: Team) => {
          try {
            const sops = await agentApi.listSOPs(p.team_id)
            results[p.team_id] = Array.isArray(sops) ? sops : []
          } catch {
            results[p.team_id] = []
          }
        })
      )
      setSopsByTeam(results)
    } catch {
      toast.error('Failed to load automations')
    } finally {
      setIsLoading(false)
    }
  }, [trainedTeams.map(p => p.team_id).join(',')])

  useEffect(() => {
    loadAllSOPs()
  }, [loadAllSOPs])

  const handleDelete = useCallback(async (sopId: string, teamId: string) => {
    try {
      await agentApi.deleteSOP(sopId)
      setSopsByTeam(prev => ({
        ...prev,
        [teamId]: (prev[teamId] || []).filter(s => s.sop_id !== sopId),
      }))
      queryClient.invalidateQueries({ queryKey: ['dashboard-stats'] })
      toast.success('Automation deleted')
    } catch {
      toast.error('Failed to delete automation')
    }
  }, [queryClient])

  const handleApprove = useCallback(async (sopId: string, teamId: string) => {
    try {
      await agentApi.approveSOP(sopId)
      setSopsByTeam(prev => ({
        ...prev,
        [teamId]: (prev[teamId] || []).map(s =>
          s.sop_id === sopId ? { ...s, status: 'approved' } : s
        ),
      }))
      toast.success('Automation approved!')
    } catch {
      toast.error('Failed to approve automation')
    }
  }, [])

  const handleManualRun = useCallback(async (sopId: string) => {
    setRunningManual(sopId)
    try {
      await agentApi.manualRun(sopId)
      toast.success('Automation run started!')
      queryClient.invalidateQueries({ queryKey: ['dashboard-stats'] })
      setTimeout(() => {
        loadRunLogs(sopId)
        queryClient.invalidateQueries({ queryKey: ['dashboard-stats'] })
      }, 2000)
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to start run')
    } finally {
      setRunningManual(null)
    }
  }, [queryClient])

  const loadRunLogs = useCallback(async (sopId: string) => {
    setLoadingLogs(sopId)
    try {
      const logs = await agentApi.listRuns(sopId)
      setRunLogs(prev => ({ ...prev, [sopId]: logs }))
    } catch {
      toast.error('Failed to load run history')
    } finally {
      setLoadingLogs(null)
    }
  }, [])

  const handleSaveSchedule = useCallback(async (sopId: string, teamId: string) => {
    try {
      const result = await agentApi.updateSchedule(sopId, scheduleType, scheduleType !== 'none' ? scheduleConfig : undefined)
      setSopsByTeam(prev => ({
        ...prev,
        [teamId]: (prev[teamId] || []).map(s => s.sop_id === sopId ? { ...s, ...result } : s),
      }))
      setEditingScheduleId(null)
      toast.success('Schedule updated!')
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to update schedule')
    }
  }, [scheduleType, scheduleConfig])

  const handleEditSop = useCallback(async (sopId: string, teamId: string) => {
    if (!editInstructions.trim()) return
    setIsEditingSop(true)
    try {
      const response = await fetch('/api/v1/agent/edit-sop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sop_id: sopId, edit_instructions: editInstructions.trim() }),
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
                setStreamingSop(eventData.chunk)
              } else if (currentEvent === 'sop_done') {
                editResult = { sop_markdown: eventData.sop_markdown, title: eventData.title }
                setStreamingSop(null)
              } else if (currentEvent === 'sop_error') {
                setStreamingSop(null)
                throw new Error(eventData.detail || 'Edit failed')
              }
            } catch (e) { if (e instanceof Error && e.message !== 'Edit failed') { /* ignore */ } else throw e }
            currentEvent = ''
          }
        }
      }
      setSopsByTeam(prev => ({
        ...prev,
        [teamId]: (prev[teamId] || []).map(s =>
          s.sop_id === sopId ? { ...s, sop_markdown: editResult.sop_markdown, title: editResult.title } : s
        ),
      }))
      setEditingSopId(null)
      setEditInstructions('')
      toast.success('Automation updated!')
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || err?.message || 'Failed to edit automation')
      setStreamingSop(null)
    } finally {
      setIsEditingSop(false)
    }
  }, [editInstructions])

  const generalSopsList = sopsByTeam[AGENT_GENERAL_SCOPE] || []
  const displaySopSections: { sectionId: string; sectionName: string; isGeneral: boolean }[] = []
  if (generalSopsList.length > 0) {
    displaySopSections.push({ sectionId: AGENT_GENERAL_SCOPE, sectionName: 'General', isGeneral: true })
  }
  for (const p of trainedTeams) {
    if ((sopsByTeam[p.team_id] || []).length > 0) {
      displaySopSections.push({ sectionId: p.team_id, sectionName: p.team_name, isGeneral: false })
    }
  }
  const totalSOPs =
    generalSopsList.length +
    trainedTeams.reduce((sum, p) => sum + (sopsByTeam[p.team_id] || []).length, 0)
  const sopSectionCount =
    (generalSopsList.length > 0 ? 1 : 0) +
    trainedTeams.filter((p: Team) => (sopsByTeam[p.team_id] || []).length > 0).length

  const toggleTeam = (teamId: string) => {
    setExpandedTeams(prev => {
      const next = new Set(prev)
      if (next.has(teamId)) next.delete(teamId)
      else next.add(teamId)
      return next
    })
  }

  const handleExpand = (sopId: string) => {
    if (expandedSopId === sopId) {
      setExpandedSopId(null)
    } else {
      setExpandedSopId(sopId)
      setExpandedSection('steps')
      // Auto-load logs
      if (!runLogs[sopId]) loadRunLogs(sopId)
    }
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header — match Documentation */}
      <motion.div
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="p-8 pb-4 border-b border-rt-border"
      >
        <div className="flex items-center gap-3 mb-2">
          <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-emerald-500/20 to-teal-500/20 flex items-center justify-center">
            <ClipboardList className="w-5 h-5 text-emerald-400" />
          </div>
          <div>
            <h1 className="text-3xl font-display font-bold">Automations</h1>
            <p className="text-rt-text-muted text-sm">Automated workflows generated from agent conversations</p>
          </div>
        </div>
      </motion.div>

      {/* Sub-header — match Documentation */}
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.05 }}
        className="px-8 py-3 border-b border-rt-border bg-rt-bg-light flex items-center gap-4"
      >
        <span className="text-xs text-rt-text-muted">
          {totalSOPs} automation{totalSOPs !== 1 ? 's' : ''}
          {sopSectionCount > 0 ? ` · ${sopSectionCount} section${sopSectionCount !== 1 ? 's' : ''}` : ''}
        </span>
      </motion.div>

      {/* Grouped automation list — match Documentation */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.1 }}
        className="flex-1 overflow-auto p-8"
      >
        {isLoading ? (
          <div className="flex items-center justify-center gap-2 text-rt-text-muted py-16">
            <Loader2 className="w-5 h-5 animate-spin" /> Loading automations...
          </div>
        ) : displaySopSections.length === 0 ? (
          <div className="text-center py-16">
            <ClipboardList className="w-12 h-12 mx-auto mb-4 text-rt-text-muted/30" />
            <p className="text-rt-text-muted max-w-md mx-auto">
              No automations yet. In the Agent, get an answer then use <strong>Build Automation</strong> — with a team or under <strong>General</strong> when no team is selected.
            </p>
          </div>
        ) : (
          <div className="max-w-6xl mx-auto space-y-6">
            {displaySopSections.map((section) => {
              const teamSOPs = sopsByTeam[section.sectionId] || []
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
                      {teamSOPs.length}
                    </span>
                  </button>

                  <AnimatePresence>
                    {isExpanded && (
                      <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="overflow-hidden"
                      >
                        <div className="pl-4 space-y-3">
                          {teamSOPs.map(sop => (
                            <div key={sop.sop_id} className="rounded-xl border border-rt-border bg-rt-surface/20 overflow-hidden hover:border-rt-border/80 transition-colors">
                              <div
                                className="flex items-center gap-4 px-5 py-3.5 cursor-pointer hover:bg-rt-surface/30 transition-colors"
                                onClick={() => handleExpand(sop.sop_id)}
                              >
                                <ClipboardList className="w-5 h-5 text-emerald-400 flex-shrink-0" />
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center gap-2 mb-0.5 flex-wrap">
                                    <span className="text-sm font-semibold truncate">{sop.title}</span>
                                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/15 text-emerald-400 flex-shrink-0">
                                      <ClipboardList className="w-2.5 h-2.5" />
                                      Automation
                                    </span>
                                    {(sop.status || 'draft') === 'approved' ? (
                                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/15 text-emerald-400 flex-shrink-0">
                                        <CheckCircle2 className="w-2.5 h-2.5" />
                                        Approved
                                      </span>
                                    ) : (
                                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-rt-surface text-rt-text-muted flex-shrink-0">
                                        Draft
                                      </span>
                                    )}
                                    {sop.schedule_type && sop.schedule_type !== 'none' && (
                                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-blue-500/15 text-blue-400 flex-shrink-0">
                                        <Timer className="w-2.5 h-2.5" />
                                        {scheduleLabel(sop.schedule_type, sop.schedule_config)}
                                      </span>
                                    )}
                                  </div>
                                  <div className="flex items-center gap-2 flex-wrap">
                                    <p className="text-xs text-rt-text-muted truncate">{sop.goal}</p>
                                    {sop.last_run_at && (
                                      <span className="text-[10px] text-rt-text-muted/60">Last: {new Date(sop.last_run_at).toLocaleString()}</span>
                                    )}
                                  </div>
                                </div>
                                <div className="flex items-center gap-1.5 flex-shrink-0">
                                  <span className="text-[10px] text-rt-text-muted/60 mr-2">
                                    {sop.created_at ? new Date(sop.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : ''}
                                  </span>
                                  {(sop.status || 'draft') !== 'approved' && (
                                    <button
                                      onClick={(e) => { e.stopPropagation(); handleApprove(sop.sop_id, section.sectionId) }}
                                      className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs font-medium bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors"
                                      title="Approve this automation"
                                    >
                                      <CheckCircle2 className="w-3.5 h-3.5" />
                                      Approve
                                    </button>
                                  )}
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      setEditingSopId(editingSopId === sop.sop_id ? null : sop.sop_id)
                                      setEditInstructions('')
                                    }}
                                    className="p-1.5 rounded-lg hover:bg-blue-500/15 text-blue-400/60 hover:text-blue-400 transition-colors"
                                    title="Edit automation"
                                  >
                                    <Edit2 className="w-4 h-4" />
                                  </button>
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      if (showLogsDropdown === sop.sop_id) setShowLogsDropdown(null)
                                      else { setShowLogsDropdown(sop.sop_id); if (!runLogs[sop.sop_id]) loadRunLogs(sop.sop_id) }
                                    }}
                                    className="p-1.5 rounded-lg hover:bg-indigo-500/15 text-indigo-400/60 hover:text-indigo-400 transition-colors relative"
                                    title="Execution logs"
                                  >
                                    <History className="w-4 h-4" />
                                    {runLogs[sop.sop_id]?.length > 0 && (
                                      <span className="absolute -top-0.5 -right-0.5 w-3.5 h-3.5 bg-indigo-500 rounded-full text-[8px] text-white flex items-center justify-center">
                                        {runLogs[sop.sop_id].length > 9 ? '9+' : runLogs[sop.sop_id].length}
                                      </span>
                                    )}
                                  </button>
                                  {sop.status === 'approved' && sop.sop_json && (
                                    <button
                                      onClick={(e) => { e.stopPropagation(); handleManualRun(sop.sop_id) }}
                                      disabled={runningManual === sop.sop_id}
                                      className="p-1.5 rounded-lg hover:bg-blue-500/15 text-blue-400/60 hover:text-blue-400 transition-colors disabled:opacity-50"
                                      title="Run now"
                                    >
                                      {runningManual === sop.sop_id ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                                    </button>
                                  )}
                                  {sop.status === 'approved' && (
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation()
                                        setEditingScheduleId(editingScheduleId === sop.sop_id ? null : sop.sop_id)
                                        setScheduleType(sop.schedule_type || 'none')
                                        setScheduleConfig(sop.schedule_config || {})
                                        if (expandedSopId !== sop.sop_id) handleExpand(sop.sop_id)
                                        setExpandedSection('schedule')
                                      }}
                                      className="p-1.5 rounded-lg hover:bg-purple-500/15 text-purple-400/60 hover:text-purple-400 transition-colors"
                                      title="Edit schedule"
                                    >
                                      <CalendarClock className="w-4 h-4" />
                                    </button>
                                  )}
                                  <button
                                    onClick={(e) => { e.stopPropagation(); handleDelete(sop.sop_id, section.sectionId) }}
                                    className="p-1.5 rounded-lg hover:bg-red-500/15 text-red-400/60 hover:text-red-400 transition-colors"
                                    title="Delete automation"
                                  >
                                    <Trash2 className="w-4 h-4" />
                                  </button>
                                  <ChevronRight className={`w-4 h-4 text-rt-text-muted transition-transform ${expandedSopId === sop.sop_id ? 'rotate-90' : ''}`} />
                                </div>
                              </div>

                              {editingSopId === sop.sop_id && streamingSop && (
                                <div className="border-t border-rt-border/50 px-5 pb-3 pt-3 flex items-center gap-2 text-sm text-rt-text-muted">
                                  <Loader2 className="w-4 h-4 animate-spin" />
                                  Updating automation...
                                </div>
                              )}
                              {editingSopId === sop.sop_id && !streamingSop && (
                                <div className="border-t border-rt-border/50 px-5 pb-4 pt-3">
                                  <p className="text-xs text-rt-text-muted mb-2">Describe the changes to make:</p>
                                  <div className="flex gap-2 items-end">
                                    <textarea
                                      value={editInstructions}
                                      onChange={(e) => setEditInstructions(e.target.value)}
                                      placeholder="e.g. Add a rollback step after step 3..."
                                      rows={2}
                                      className="input flex-1 text-sm min-h-[2.5rem] resize-y"
                                      disabled={isEditingSop}
                                      autoFocus
                                    />
                                    <button
                                      type="button"
                                      onClick={() => handleEditSop(sop.sop_id, section.sectionId)}
                                      disabled={isEditingSop || !editInstructions.trim()}
                                      className="btn-primary text-xs px-3 py-1.5 flex items-center gap-1.5"
                                    >
                                      {isEditingSop ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                                      Go
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => { setEditingSopId(null); setEditInstructions('') }}
                                      disabled={isEditingSop}
                                      className="btn-secondary text-xs px-3 py-1.5"
                                    >
                                      Cancel
                                    </button>
                                  </div>
                                </div>
                              )}

                              {showLogsDropdown === sop.sop_id && (
                                <div className="border-t border-rt-border/50 px-5 pb-4 pt-3">
                                  <div className="flex items-center justify-between mb-3">
                                    <span className="text-xs font-medium text-rt-text">Execution History</span>
                                    <button onClick={(e) => { e.stopPropagation(); loadRunLogs(sop.sop_id) }} className="text-xs text-rt-primary hover:underline">
                                      {loadingLogs === sop.sop_id ? <Loader2 className="w-3 h-3 animate-spin inline" /> : 'Refresh'}
                                    </button>
                                  </div>
                                  {!runLogs[sop.sop_id] || runLogs[sop.sop_id].length === 0 ? (
                                    <p className="text-xs text-rt-text-muted/60 italic py-4 text-center">No executions yet</p>
                                  ) : (
                                    <div className="space-y-2 max-h-80 overflow-y-auto">
                                      {runLogs[sop.sop_id].map((run: any) => (
                                        <RunLogEntry key={run.run_id} run={run} compact />
                                      ))}
                                    </div>
                                  )}
                                </div>
                              )}

                              <AnimatePresence>
                                {expandedSopId === sop.sop_id && (
                                  <motion.div
                                    initial={{ height: 0, opacity: 0 }}
                                    animate={{ height: 'auto', opacity: 1 }}
                                    exit={{ height: 0, opacity: 0 }}
                                    className="overflow-hidden"
                                  >
                                    <div className="border-t border-rt-border/50">
                                      {/* Section tabs */}
                                      <div className="flex gap-1 px-5 pt-3 pb-2">
                          {[
                            { key: 'steps' as const, label: 'Steps', icon: ClipboardList },
                            { key: 'logs' as const, label: 'Execution Logs', icon: History },
                            ...(sop.status === 'approved' ? [{ key: 'schedule' as const, label: 'Schedule', icon: CalendarClock }] : []),
                          ].map(tab => (
                            <button
                              key={tab.key}
                              onClick={() => {
                                setExpandedSection(tab.key)
                                if (tab.key === 'logs' && !runLogs[sop.sop_id]) loadRunLogs(sop.sop_id)
                                if (tab.key === 'schedule') {
                                  setEditingScheduleId(sop.sop_id)
                                  setScheduleType(sop.schedule_type || 'none')
                                  setScheduleConfig(sop.schedule_config || {})
                                }
                              }}
                              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                                expandedSection === tab.key
                                  ? 'bg-rt-primary/10 text-rt-primary'
                                  : 'text-rt-text-muted hover:text-rt-text hover:bg-rt-surface'
                              }`}
                            >
                              <tab.icon className="w-3.5 h-3.5" />
                              {tab.label}
                              {tab.key === 'logs' && runLogs[sop.sop_id]?.length ? (
                                <span className="text-[10px] opacity-60">({runLogs[sop.sop_id].length})</span>
                              ) : null}
                            </button>
                          ))}
                                      </div>

                                      {expandedSection === 'steps' && (
                                        <div className="px-5 pb-4">
                                          <div className="prose prose-sm max-w-none">
                                            <ReactMarkdown>{sop.sop_markdown}</ReactMarkdown>
                                          </div>
                                        </div>
                                      )}

                                      {expandedSection === 'logs' && (
                                        <div className="px-5 pb-4">
                                          <div className="flex items-center justify-between mb-3">
                                            <span className="text-xs text-rt-text-muted">Execution History</span>
                                            <button onClick={() => loadRunLogs(sop.sop_id)} className="text-xs text-rt-primary hover:underline">
                                              {loadingLogs === sop.sop_id ? <Loader2 className="w-3 h-3 animate-spin inline" /> : 'Refresh'}
                                            </button>
                                          </div>
                                          {!runLogs[sop.sop_id] || runLogs[sop.sop_id].length === 0 ? (
                                            <p className="text-xs text-rt-text-muted/60 italic py-4 text-center">No executions yet</p>
                                          ) : (
                                            <div className="space-y-2">
                                              {runLogs[sop.sop_id].map((run: any) => (
                                                <RunLogEntry key={run.run_id} run={run} />
                                              ))}
                                            </div>
                                          )}
                                        </div>
                                      )}

                                      {expandedSection === 'schedule' && editingScheduleId === sop.sop_id && (
                                        <div className="px-5 pb-4">
                                          <InlineScheduleEditor
                                            scheduleType={scheduleType}
                                            scheduleConfig={scheduleConfig}
                                            onChangeType={setScheduleType}
                                            onChangeConfig={setScheduleConfig}
                                            onSave={() => handleSaveSchedule(sop.sop_id, section.sectionId)}
                                            onCancel={() => { setEditingScheduleId(null); setExpandedSection('steps') }}
                                          />
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

// ---------------------------------------------------------------------------
// Run log entry component
// ---------------------------------------------------------------------------
function RunLogEntry({ run, compact = false }: { run: any; compact?: boolean }) {
  const [showOutput, setShowOutput] = useState(false)
  const statusColors: Record<string, string> = {
    completed: 'text-emerald-400 bg-emerald-500/15',
    failed: 'text-red-400 bg-red-500/15',
    running: 'text-blue-400 bg-blue-500/15',
  }

  if (compact) {
    return (
      <div className="rounded border border-rt-border/30 bg-rt-surface/10 overflow-hidden">
        <div
          className="flex items-center gap-2 px-2 py-1.5 cursor-pointer hover:bg-rt-surface/30 transition-colors"
          onClick={() => setShowOutput(!showOutput)}
        >
          <span className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[9px] font-medium ${statusColors[run.status] || 'text-rt-text-muted bg-rt-surface'}`}>
            {run.status === 'completed' ? <CheckCircle2 className="w-2 h-2" /> : run.status === 'failed' ? <XCircle className="w-2 h-2" /> : <Loader2 className="w-2 h-2 animate-spin" />}
            {run.status}
          </span>
          <span className="text-[9px] text-rt-text-muted">
            {run.trigger === 'manual' ? '🖱' : '⏰'}
          </span>
          <span className="text-[9px] text-rt-text-muted">
            {run.steps_completed}/{run.steps_total}
          </span>
          {run.duration_ms && (
            <span className="text-[9px] text-rt-text-muted">{(run.duration_ms / 1000).toFixed(1)}s</span>
          )}
          <span className="text-[9px] text-rt-text-muted/60 ml-auto">
            {run.started_at ? new Date(run.started_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}
          </span>
          <ChevronRight className={`w-2.5 h-2.5 text-rt-text-muted transition-transform ${showOutput ? 'rotate-90' : ''}`} />
        </div>
        {showOutput && run.output_log && (
          <div className="px-2 pb-1.5 border-t border-rt-border/20">
            <pre className="bg-rt-surface border border-rt-border text-rt-text text-[9px] p-1.5 rounded overflow-x-auto font-mono whitespace-pre-wrap max-h-32 overflow-y-auto mt-1">
              {run.output_log}
            </pre>
            {run.error && (
              <div className="mt-1 text-[9px] text-red-400 bg-red-500/10 rounded px-1.5 py-0.5">{run.error}</div>
            )}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-rt-border/50 bg-rt-surface/20 overflow-hidden">
      <div
        className="flex items-center gap-3 px-3 py-2 cursor-pointer hover:bg-rt-surface/30 transition-colors"
        onClick={() => setShowOutput(!showOutput)}
      >
        <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium ${statusColors[run.status] || 'text-rt-text-muted bg-rt-surface'}`}>
          {run.status === 'completed' ? <CheckCircle2 className="w-2.5 h-2.5" /> : run.status === 'failed' ? <XCircle className="w-2.5 h-2.5" /> : <Loader2 className="w-2.5 h-2.5 animate-spin" />}
          {run.status}
        </span>
        <span className="text-[10px] text-rt-text-muted">
          {run.trigger === 'manual' ? '🖱 Manual' : '⏰ Scheduled'}
        </span>
        <span className="text-[10px] text-rt-text-muted">
          Steps: {run.steps_completed}/{run.steps_total}
        </span>
        {run.duration_ms && (
          <span className="text-[10px] text-rt-text-muted">{(run.duration_ms / 1000).toFixed(1)}s</span>
        )}
        <span className="text-[10px] text-rt-text-muted/60 ml-auto">
          {run.started_at ? new Date(run.started_at).toLocaleString() : ''}
        </span>
        <ChevronRight className={`w-3 h-3 text-rt-text-muted transition-transform ${showOutput ? 'rotate-90' : ''}`} />
      </div>
      {showOutput && run.output_log && (
        <div className="px-3 pb-2 border-t border-rt-border/30">
          <pre className="bg-rt-surface border border-rt-border text-rt-text text-[10px] p-2 rounded-lg overflow-x-auto font-mono whitespace-pre-wrap max-h-48 overflow-y-auto mt-2">
            {run.output_log}
          </pre>
          {run.error && (
            <div className="mt-1 text-[10px] text-red-400 bg-red-500/10 rounded px-2 py-1">{run.error}</div>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Inline schedule editor
// ---------------------------------------------------------------------------
function InlineScheduleEditor({ scheduleType, scheduleConfig, onChangeType, onChangeConfig, onSave, onCancel }: {
  scheduleType: string
  scheduleConfig: Record<string, any>
  onChangeType: (t: string) => void
  onChangeConfig: (c: Record<string, any>) => void
  onSave: () => void
  onCancel: () => void
}) {
  const options = [
    { value: 'none', label: 'No Schedule', icon: '✕' },
    { value: 'once', label: 'One-Time', icon: '📅' },
    { value: 'interval', label: 'Interval', icon: '⏱' },
    { value: 'daily', label: 'Daily', icon: '🔄' },
    { value: 'weekly', label: 'Weekly', icon: '📆' },
    { value: 'monthly', label: 'Monthly', icon: '🗓' },
    { value: 'cron', label: 'Cron', icon: '⚙' },
  ]
  const allDays = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

  return (
    <div>
      <div className="grid grid-cols-4 sm:grid-cols-7 gap-1.5 mb-3">
        {options.map(opt => (
          <button key={opt.value} type="button" onClick={() => { onChangeType(opt.value); onChangeConfig({}) }}
            className={`p-2 rounded-lg border text-center transition-all text-xs ${
              scheduleType === opt.value
                ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400'
                : 'border-rt-border hover:border-rt-text-muted/30 text-rt-text-muted'
            }`}>
            <div>{opt.icon}</div>
            <div className="font-medium mt-0.5">{opt.label}</div>
          </button>
        ))}
      </div>

      {scheduleType === 'once' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Run at:</label>
          <input type="datetime-local" value={scheduleConfig.run_at || ''} onChange={e => onChangeConfig({ ...scheduleConfig, run_at: e.target.value })}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500 flex-1" />
        </div>
      )}

      {scheduleType === 'interval' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Every</label>
          <input type="number" min={1} value={scheduleConfig.every || 30} onChange={e => onChangeConfig({ ...scheduleConfig, every: Number(e.target.value) })}
            className="w-16 bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs text-center focus:outline-none focus:border-emerald-500" />
          <select value={scheduleConfig.unit || 'minutes'} onChange={e => onChangeConfig({ ...scheduleConfig, unit: e.target.value })}
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
          <input type="time" value={scheduleConfig.time || '09:00'} onChange={e => onChangeConfig({ ...scheduleConfig, time: e.target.value })}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500" />
        </div>
      )}

      {scheduleType === 'weekly' && (
        <div className="mb-3">
          <div className="flex flex-wrap gap-1 mb-2">
            {allDays.map(day => (
              <button key={day} type="button"
                onClick={() => {
                  const days = scheduleConfig.days || []
                  onChangeConfig({ ...scheduleConfig, days: days.includes(day) ? days.filter((d: string) => d !== day) : [...days, day] })
                }}
                className={`px-2 py-1 rounded-md text-[10px] font-medium transition-colors ${
                  (scheduleConfig.days || []).includes(day)
                    ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                    : 'bg-rt-surface text-rt-text-muted border border-rt-border'
                }`}>
                {day.charAt(0).toUpperCase() + day.slice(1, 3)}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-rt-text-muted">At:</label>
            <input type="time" value={scheduleConfig.time || '09:00'} onChange={e => onChangeConfig({ ...scheduleConfig, time: e.target.value })}
              className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500" />
          </div>
        </div>
      )}

      {scheduleType === 'monthly' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Day</label>
          <input type="number" min={1} max={28} value={scheduleConfig.day_of_month || 1} onChange={e => onChangeConfig({ ...scheduleConfig, day_of_month: Number(e.target.value) })}
            className="w-14 bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs text-center focus:outline-none focus:border-emerald-500" />
          <label className="text-xs text-rt-text-muted">at</label>
          <input type="time" value={scheduleConfig.time || '09:00'} onChange={e => onChangeConfig({ ...scheduleConfig, time: e.target.value })}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500" />
        </div>
      )}

      {scheduleType === 'cron' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Cron:</label>
          <input type="text" value={scheduleConfig.expression || '0 9 * * 1-5'} onChange={e => onChangeConfig({ ...scheduleConfig, expression: e.target.value })}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500 flex-1 font-mono" />
          <span className="text-[10px] text-rt-text-muted/60">min hr dom mon dow</span>
        </div>
      )}

      <div className="flex items-center gap-2">
        <button onClick={onSave}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25 transition-colors">
          <CheckCircle2 className="w-3.5 h-3.5" />
          Save Schedule
        </button>
        <button onClick={onCancel}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-rt-text-muted hover:bg-rt-surface transition-colors">
          Cancel
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------
function scheduleLabel(type: string, config?: Record<string, any>): string {
  if (!type || type === 'none') return ''
  if (type === 'once') return 'One-time'
  if (type === 'interval') return `Every ${config?.every || '?'} ${config?.unit || 'min'}`
  if (type === 'daily') return `Daily ${config?.time || ''}`
  if (type === 'weekly') return `Weekly`
  if (type === 'monthly') return `Monthly`
  if (type === 'cron') return `Cron`
  return type
}
