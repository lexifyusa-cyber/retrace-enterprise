import { useEffect, useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, CheckCircle, AlertCircle, Loader2, FolderTree, RefreshCw, ScrollText, StopCircle, FileText, Brain, Shield, FolderSearch } from 'lucide-react'
import toast from 'react-hot-toast'
import { productApi } from '@/utils/api'

interface GroupStatus {
  group_id: string
  group_name: string
  group_type: string
  training_status: string
  last_trained: string | null
  folder_paths: string[]
  stats: Record<string, unknown>
}

interface TrainingStatus {
  product_id: string
  product_name: string
  is_training: boolean
  all_completed: boolean
  any_failed: boolean
  groups: GroupStatus[]
}

interface TrainingProgressModalProps {
  productId: string
  onClose: () => void
}

const PHASES = ['phase1', 'phase2', 'phase3', 'phase4', 'completed'] as const
type Phase = typeof PHASES[number]

const PHASE_CONFIG: Record<string, { label: string; icon: string; color: string; weight: number }> = {
  phase1: { label: 'Scan & Exclude',  icon: '📂', color: 'bg-cyan-500',    weight: 5 },
  phase2: { label: 'LLM Analysis',    icon: '🤖', color: 'bg-amber-500',   weight: 30 },
  phase3: { label: 'Text Extraction',  icon: '📝', color: 'bg-blue-500',    weight: 50 },
  phase4: { label: 'Encrypt KB',       icon: '🔐', color: 'bg-emerald-500', weight: 15 },
  completed: { label: 'Done',          icon: '✅', color: 'bg-green-500',   weight: 0 },
}

export default function TrainingProgressModal({ productId, onClose }: TrainingProgressModalProps) {
  const [status, setStatus] = useState<TrainingStatus | null>(null)
  const [isPolling, setIsPolling] = useState(true)
  const [startedAt] = useState(() => new Date())
  const [elapsed, setElapsed] = useState('0s')
  const [expandedLogs, setExpandedLogs] = useState<Set<string>>(new Set())
  const [isStopping, setIsStopping] = useState(false)

  const handleStop = async () => {
    if (!confirm('Stop training? This will cancel all in-progress work.')) return
    setIsStopping(true)
    try {
      await productApi.stopTraining(productId)
      toast.success('Training stop requested')
    } catch (error: any) {
      toast.error(error?.response?.data?.detail || 'Failed to stop training')
    }
  }

  useEffect(() => {
    const timer = setInterval(() => {
      const diff = Math.floor((Date.now() - startedAt.getTime()) / 1000)
      if (diff < 60) {
        setElapsed(`${diff}s`)
      } else if (diff < 3600) {
        setElapsed(`${Math.floor(diff / 60)}m ${diff % 60}s`)
      } else {
        setElapsed(`${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`)
      }
    }, 1000)
    return () => clearInterval(timer)
  }, [startedAt])

  const pollStatus = useCallback(async () => {
    try {
      const data = await productApi.getTrainingStatus(productId)
      setStatus(data)
      if (!data.is_training) {
        setIsPolling(false)
      }
    } catch (error) {
      console.error('Error polling training status:', error)
    }
  }, [productId])

  useEffect(() => {
    if (!isPolling) return
    pollStatus()
    const pollInterval = setInterval(pollStatus, 2000)
    return () => clearInterval(pollInterval)
  }, [isPolling, pollStatus])

  // Extract live stats from the active training group
  const activeGroup = status?.groups.find(g => g.training_status === 'training')
  const completedGroup = status?.groups.find(g => g.training_status === 'completed' || g.training_status === 'failed')
  const liveStats = ((activeGroup || completedGroup)?.stats || {}) as Record<string, any>
  const currentPhase: string = liveStats.phase || (status?.all_completed ? 'completed' : status?.any_failed ? 'failed' : 'initializing')

  // Compute progress percentage based on 4-phase pipeline
  const phaseProgress = (() => {
    if (status?.all_completed || currentPhase === 'completed') return 100
    if (currentPhase === 'failed' || currentPhase === 'cancelled') return 0

    switch (currentPhase) {
      case 'initializing':
        return 1

      case 'phase1':
        return 3

      case 'phase2': {
        // Phase 2 is 5%–35% of total (LLM analysis is the bulk)
        const msg = String(liveStats.message || '')
        if (msg.includes('2b:')) return 20  // file evaluation step
        if (msg.includes('2a')) return 10   // folder evaluation step
        return 8
      }

      case 'phase3': {
        // Phase 3 is 35%–85% of total (text extraction scales with file count)
        const extracted = Number(liveStats.files_extracted || 0)
        const total = Number(liveStats.files_total || 1)
        const extractPct = total > 0 ? extracted / total : 0
        return 35 + Math.round(extractPct * 50)
      }

      case 'phase4': {
        // Phase 4 is 85%–100% (compress + encrypt)
        const msg = String(liveStats.message || '')
        if (msg.includes('4d') || msg.includes('Compress') || msg.includes('Encrypt')) return 92
        if (msg.includes('4c')) return 90
        if (msg.includes('4b')) return 88
        return 86
      }

      default:
        return 0
    }
  })()

  // Phase label shown above the progress bar
  const phaseLabel = (() => {
    switch (currentPhase) {
      case 'initializing': return '🔧 Initializing pipeline…'
      case 'phase1': {
        const msg = liveStats.message || ''
        if (String(msg).includes('done')) return `📂 ${msg}`
        return '📂 Phase 1: Scanning folders & excluding binaries…'
      }
      case 'phase2': {
        const msg = String(liveStats.message || 'LLM analysing project…')
        return `🤖 ${msg}`
      }
      case 'phase3': {
        const extracted = liveStats.files_extracted || 0
        const total = liveStats.files_total || 0
        const current = liveStats.current_file
          ? String(liveStats.current_file).split('/').slice(-2).join('/')
          : ''
        if (total > 0) {
          return `📝 Phase 3: Extracting text [${extracted}/${total}]${current ? ` — ${current}` : ''}`
        }
        return String(liveStats.message || 'Phase 3: Extracting text…')
      }
      case 'phase4': {
        return `🔐 ${String(liveStats.message || 'Phase 4: Building encrypted KB…')}`
      }
      case 'completed': return '✅ Training complete!'
      case 'failed': return '❌ Training failed'
      case 'cancelled': return '🛑 Training stopped'
      default: return status?.is_training ? '⏳ Starting…' : '—'
    }
  })()

  // Progress bar color
  const barColor = (() => {
    if (currentPhase === 'completed') return 'bg-green-500'
    if (currentPhase === 'failed') return 'bg-red-500'
    if (currentPhase === 'cancelled') return 'bg-gray-500'
    return PHASE_CONFIG[currentPhase]?.color || 'bg-blue-500'
  })()

  const getStatusIcon = (s: string) => {
    switch (s) {
      case 'completed': return <CheckCircle className="w-4 h-4 text-green-400" />
      case 'failed': return <AlertCircle className="w-4 h-4 text-red-400" />
      case 'training': return <Loader2 className="w-4 h-4 text-blue-400 animate-spin" />
      default: return <div className="w-4 h-4 rounded-full border-2 border-rt-border" />
    }
  }

  const getStatusBadge = (s: string) => {
    switch (s) {
      case 'completed': return 'bg-green-500/10 text-green-400 border-green-500/20'
      case 'failed': return 'bg-red-500/10 text-red-400 border-red-500/20'
      case 'training': return 'bg-blue-500/10 text-blue-400 border-blue-500/20'
      default: return 'bg-gray-500/10 text-rt-text-muted border-gray-500/20'
    }
  }

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4"
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.95, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.95, opacity: 0 }}
          className="bg-rt-bg-light border border-rt-border rounded-xl p-6 w-full max-w-2xl max-h-[85vh] overflow-y-auto"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className="flex items-center justify-between mb-6">
            <div>
              <h2 className="text-xl font-display font-semibold flex items-center gap-2">
                {isPolling ? (
                  <Loader2 className="w-5 h-5 text-blue-400 animate-spin" />
                ) : status?.all_completed ? (
                  <CheckCircle className="w-5 h-5 text-green-400" />
                ) : status?.any_failed ? (
                  <AlertCircle className="w-5 h-5 text-red-400" />
                ) : (
                  <CheckCircle className="w-5 h-5 text-green-400" />
                )}
                Training Progress
              </h2>
              {status && (
                <p className="text-sm text-rt-text-muted mt-1">
                  {status.product_name} · {elapsed} elapsed
                </p>
              )}
            </div>
            <div className="flex items-center gap-2">
              {isPolling && status?.is_training && (
                <button
                  onClick={handleStop}
                  disabled={isStopping}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 hover:bg-red-500/20 border border-red-500/20 transition-colors text-sm disabled:opacity-50"
                  title="Stop training"
                >
                  <StopCircle className="w-4 h-4" />
                  {isStopping ? 'Stopping…' : 'Stop'}
                </button>
              )}
              {!isPolling && (
                <button
                  onClick={() => setIsPolling(true)}
                  className="p-2 rounded-lg hover:bg-rt-surface transition-colors text-rt-text-muted"
                  title="Resume polling"
                >
                  <RefreshCw className="w-4 h-4" />
                </button>
              )}
              <button
                onClick={onClose}
                className="p-2 rounded-lg hover:bg-rt-surface transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>

          {/* Main Progress Section */}
          <div className="mb-6 p-4 bg-rt-surface rounded-lg border border-rt-border">
            {/* Phase label + percentage */}
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium">{phaseLabel}</span>
              <span className="text-sm font-semibold tabular-nums">{phaseProgress}%</span>
            </div>

            {/* Progress bar */}
            <div className="w-full h-5 bg-rt-border rounded-full overflow-hidden">
              <motion.div
                className={`h-full rounded-full ${barColor}`}
                initial={{ width: 0 }}
                animate={{ width: `${phaseProgress}%` }}
                transition={{ duration: 0.3 }}
              />
            </div>

            {/* 4-phase step indicators */}
            <div className="flex items-center justify-between mt-3 text-xs text-rt-text-muted">
              {PHASES.map((phase) => {
                const currentIdx = PHASES.indexOf(currentPhase as Phase)
                const phaseIdx = PHASES.indexOf(phase)
                const isDone = currentPhase === 'completed' || phaseIdx < currentIdx
                const isActive = phase === currentPhase && currentPhase !== 'completed'
                const config = PHASE_CONFIG[phase]

                return (
                  <span
                    key={phase}
                    className={`flex items-center gap-1 ${
                      isDone ? 'text-green-400' :
                      isActive ? 'text-blue-400 font-medium' :
                      'text-rt-text-muted'
                    }`}
                  >
                    {isDone ? (
                      <CheckCircle className="w-3 h-3" />
                    ) : isActive ? (
                      <Loader2 className="w-3 h-3 animate-spin" />
                    ) : (
                      <div className="w-3 h-3 rounded-full border border-current opacity-40" />
                    )}
                    {config.label}
                  </span>
                )
              })}
            </div>

            {/* Current file display during Phase 3 */}
            {currentPhase === 'phase3' && liveStats.current_file && (
              <div className="mt-2 text-xs text-rt-text-muted truncate">
                📄 {String(liveStats.current_file).split('/').slice(-2).join('/')}
              </div>
            )}

            {/* Live counters */}
            {status?.is_training && (
              <div className="flex items-center gap-4 mt-3 text-xs text-rt-text-muted flex-wrap">
                {liveStats.total_files !== undefined && (
                  <span>📄 {liveStats.total_files} files scanned</span>
                )}
                {liveStats.phase1_excluded !== undefined && Number(liveStats.phase1_excluded) > 0 && (
                  <span>🚫 {liveStats.phase1_excluded} binary excluded</span>
                )}
                {liveStats.phase2_excluded !== undefined && Number(liveStats.phase2_excluded) > 0 && (
                  <span>🤖 {liveStats.phase2_excluded} LLM excluded</span>
                )}
                {liveStats.kept_files !== undefined && (
                  <span>✅ {liveStats.kept_files} kept</span>
                )}
                {liveStats.files_extracted !== undefined && liveStats.files_total !== undefined && (
                  <span>📝 {liveStats.files_extracted}/{liveStats.files_total} extracted</span>
                )}
              </div>
            )}
          </div>

          {/* Per-Group Status */}
          <div className="space-y-3">
            <h3 className="text-sm font-medium text-rt-text-muted">Folder Groups</h3>

            {!status ? (
              <div className="text-center py-8 text-rt-text-muted">
                <Loader2 className="w-8 h-8 mx-auto mb-2 animate-spin" />
                <p>Connecting to training pipeline...</p>
              </div>
            ) : (
              status.groups.map((group) => {
                const stats = group.stats as Record<string, any>
                return (
                  <div
                    key={group.group_id}
                    className="p-4 bg-rt-surface rounded-lg border border-rt-border"
                  >
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <FolderTree className="w-4 h-4 text-rt-text-muted" />
                        <span className="font-medium text-sm">{group.group_name}</span>
                        <span className={`text-xs px-2 py-0.5 rounded-full border ${getStatusBadge(group.group_type)}`}>
                          {group.group_type}
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        {getStatusIcon(group.training_status)}
                        <span className={`text-xs px-2 py-0.5 rounded-full border ${getStatusBadge(group.training_status)}`}>
                          {group.training_status}
                        </span>
                      </div>
                    </div>

                    {/* Live progress when training */}
                    {group.training_status === 'training' && stats && Object.keys(stats).length > 0 && (
                      <div className="mt-2 space-y-2">
                        {stats.message && (
                          <div className="flex items-center gap-2 text-sm text-blue-400">
                            <Loader2 className="w-3.5 h-3.5 animate-spin flex-shrink-0" />
                            <span className="truncate">{String(stats.message)}</span>
                          </div>
                        )}

                        {/* File extraction progress bar (Phase 3) */}
                        {stats.files_total && Number(stats.files_total) > 0 && (
                          <div>
                            <div className="flex items-center justify-between text-xs text-rt-text-muted mb-1">
                              <span>Files: {String(stats.files_extracted || 0)}/{String(stats.files_total)}</span>
                              <span>{Math.round((Number(stats.files_extracted || 0) / Number(stats.files_total)) * 100)}%</span>
                            </div>
                            <div className="w-full h-2 bg-rt-border rounded-full overflow-hidden">
                              <motion.div
                                className="h-full bg-blue-500 rounded-full"
                                initial={{ width: 0 }}
                                animate={{ width: `${(Number(stats.files_extracted || 0) / Number(stats.files_total)) * 100}%` }}
                                transition={{ duration: 0.3 }}
                              />
                            </div>
                          </div>
                        )}

                        {stats.current_file && (
                          <div className="text-xs text-rt-text-muted truncate">
                            <span>Current: </span>
                            <span className="font-mono" title={String(stats.current_file)}>
                              {String(stats.current_file).split('/').slice(-2).join('/')}
                            </span>
                          </div>
                        )}
                      </div>
                    )}

                    {/* Folder paths (shown when not training) */}
                    {group.training_status !== 'training' && (
                      <div className="text-xs text-rt-text-muted mb-2">
                        {group.folder_paths.map((p, i) => (
                          <div key={i} className="font-mono truncate" title={p}>
                            📁 {p}
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Final stats when completed or failed */}
                    {(group.training_status === 'completed' || group.training_status === 'failed') && stats && Object.keys(stats).length > 0 && (
                      <div className="mt-2">
                        {group.training_status === 'failed' && stats.message && (
                          <div className="p-2 mb-2 bg-red-500/10 border border-red-500/20 rounded text-xs text-red-400">
                            {String(stats.message)}
                          </div>
                        )}
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-2 border-t border-rt-border">
                          {stats.total_files !== undefined && (
                            <div className="text-center">
                              <div className="text-lg font-semibold text-rt-text">{String(stats.total_files)}</div>
                              <div className="text-xs text-rt-text-muted">Files Scanned</div>
                            </div>
                          )}
                          {stats.files_kept !== undefined && (
                            <div className="text-center">
                              <div className="text-lg font-semibold text-blue-400">{String(stats.files_kept)}</div>
                              <div className="text-xs text-rt-text-muted">Files Kept</div>
                            </div>
                          )}
                          {stats.files_extracted !== undefined && (
                            <div className="text-center">
                              <div className="text-lg font-semibold text-green-400">{String(stats.files_extracted)}</div>
                              <div className="text-xs text-rt-text-muted">Extracted</div>
                            </div>
                          )}
                          {stats.kb_compressed_bytes !== undefined && (
                            <div className="text-center">
                              <div className="text-lg font-semibold text-emerald-400">
                                {(Number(stats.kb_compressed_bytes) / 1024).toFixed(0)}KB
                              </div>
                              <div className="text-xs text-rt-text-muted">KB Size</div>
                            </div>
                          )}
                        </div>

                        {/* Timing breakdown */}
                        {stats.timings && (
                          <div className="mt-3 pt-2 border-t border-rt-border">
                            <div className="grid grid-cols-4 gap-2 text-xs text-rt-text-muted">
                              {stats.timings.phase1 !== undefined && (
                                <span>P1: {stats.timings.phase1}s</span>
                              )}
                              {stats.timings.phase2 !== undefined && (
                                <span>P2: {stats.timings.phase2}s</span>
                              )}
                              {stats.timings.phase3 !== undefined && (
                                <span>P3: {stats.timings.phase3}s</span>
                              )}
                              {stats.timings.phase4 !== undefined && (
                                <span>P4: {stats.timings.phase4}s</span>
                              )}
                            </div>
                            {stats.total_time !== undefined && (
                              <div className="text-xs text-rt-text-muted mt-1">
                                Total: {stats.total_time}s
                              </div>
                            )}
                          </div>
                        )}

                        {/* Project summary */}
                        {stats.project_summary && (
                          <div className="mt-3 pt-2 border-t border-rt-border">
                            <div className="text-xs text-rt-text-muted mb-1 flex items-center gap-1">
                              <Brain className="w-3 h-3" /> LLM Project Analysis
                            </div>
                            <p className="text-xs text-rt-text leading-relaxed">
                              {String(stats.project_summary)}
                            </p>
                            {stats.technologies && Array.isArray(stats.technologies) && stats.technologies.length > 0 && (
                              <div className="flex flex-wrap gap-1 mt-2">
                                {(stats.technologies as string[]).slice(0, 8).map((tech, i) => (
                                  <span key={i} className="text-xs px-1.5 py-0.5 bg-rt-bg rounded border border-rt-border">
                                    {tech}
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )}

                    {/* View Training Log button */}
                    {(group.training_status === 'completed' || group.training_status === 'failed') &&
                      stats && Array.isArray(stats.logs) && (stats.logs as string[]).length > 0 && (
                      <div className="mt-3">
                        <button
                          onClick={() => {
                            const next = new Set(expandedLogs)
                            if (next.has(group.group_id)) {
                              next.delete(group.group_id)
                            } else {
                              next.add(group.group_id)
                            }
                            setExpandedLogs(next)
                          }}
                          className="flex items-center gap-1.5 text-xs text-rt-text-muted hover:text-rt-text transition-colors"
                        >
                          <ScrollText className="w-3.5 h-3.5" />
                          {expandedLogs.has(group.group_id) ? 'Hide Training Log' : 'View Training Log'}
                        </button>

                        {expandedLogs.has(group.group_id) && (
                          <div className="mt-2 bg-rt-surface rounded-lg border border-rt-border p-3 font-mono text-xs text-rt-text max-h-48 overflow-y-auto">
                            {(stats.logs as string[]).map((log, i) => (
                              <div key={i} className="leading-5 whitespace-pre-wrap">{String(log)}</div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}

                    {group.last_trained && (
                      <div className="text-xs text-rt-text-muted mt-2">
                        Last trained: {new Date(group.last_trained).toLocaleString()}
                      </div>
                    )}
                  </div>
                )
              })
            )}
          </div>

          {/* Real-time Activity Log */}
          {status && status.groups.some(g => {
            const s = g.stats as Record<string, unknown>
            return s && Array.isArray(s.logs) && s.logs.length > 0
          }) && (
            <div className="mt-4">
              <h3 className="text-sm font-medium text-rt-text-muted mb-2">📋 Activity Log</h3>
              <div
                className="bg-rt-surface rounded-lg border border-rt-border p-3 font-mono text-xs text-rt-text max-h-48 overflow-y-auto"
                ref={(el) => {
                  if (el) el.scrollTop = el.scrollHeight
                }}
              >
                {(() => {
                  const allLogs: string[] = []
                  for (const g of status.groups) {
                    const s = g.stats as Record<string, unknown>
                    if (s && Array.isArray(s.logs)) {
                      for (const log of s.logs as string[]) {
                        if (!allLogs.includes(log)) allLogs.push(log)
                      }
                    }
                  }
                  return allLogs.map((log, i) => (
                    <div key={i} className="leading-5 whitespace-pre-wrap">{log}</div>
                  ))
                })()}
              </div>
            </div>
          )}

          {/* Footer */}
          <div className="mt-6 flex items-center justify-between">
            <p className="text-xs text-rt-text-muted">
              {isPolling ? 'Auto-refreshing every 2s…' : 'Training finished.'}
            </p>
            <button onClick={onClose} className="btn-primary text-sm py-2 px-4">
              {isPolling ? 'Close (training continues)' : 'Close'}
            </button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}
