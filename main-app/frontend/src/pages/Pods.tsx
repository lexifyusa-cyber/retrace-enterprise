import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Server, Trash2, ExternalLink, Download, AlertTriangle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import toast from 'react-hot-toast'
import { podApi } from '@/utils/api'
import { Pod } from '@/types'

export default function Pods() {
  const [showGenerateModal, setShowGenerateModal] = useState(false)
  const queryClient = useQueryClient()
  
  const { data: pods = [], isLoading } = useQuery({
    queryKey: ['pods'],
    queryFn: podApi.list,
  })
  
  const deleteMutation = useMutation({
    mutationFn: podApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['pods'] })
      toast.success('POD deleted successfully')
    },
    onError: () => {
      toast.error('Failed to delete POD')
    },
  })

  return (
    <div className="p-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-display font-bold mb-2">POD Agents</h1>
          <p className="text-rt-text-muted">
            Manage your distributed POD agents across different machines
          </p>
        </div>
        <button
          onClick={() => setShowGenerateModal(true)}
          className="btn-primary flex items-center gap-2"
        >
          <Plus className="w-4 h-4" />
          Generate POD
        </button>
      </div>

      {/* POD Grid */}
      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="card animate-pulse">
              <div className="h-6 bg-rt-surface rounded w-1/2 mb-4" />
              <div className="h-4 bg-rt-surface rounded w-full mb-2" />
              <div className="h-4 bg-rt-surface rounded w-2/3" />
            </div>
          ))}
        </div>
      ) : pods.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="card text-center py-16"
        >
          <Server className="w-16 h-16 mx-auto text-rt-text-muted mb-4" />
          <h3 className="text-xl font-display font-semibold mb-2">No PODs Yet</h3>
          <p className="text-rt-text-muted mb-6 max-w-md mx-auto">
            Generate your first POD agent to start collecting knowledge from remote machines.
          </p>
          <button
            onClick={() => setShowGenerateModal(true)}
            className="btn-primary"
          >
            Generate Your First POD
          </button>
        </motion.div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {pods.map((pod, index) => (
            <PodCard
              key={pod.pod_id}
              pod={pod}
              index={index}
              onDelete={() => {
                if (confirm(`Delete POD "${pod.pod_name}"?`)) {
                  deleteMutation.mutate(pod.pod_id)
                }
              }}
            />
          ))}
        </div>
      )}

      {/* Generate Modal */}
      <AnimatePresence>
        {showGenerateModal && (
          <GeneratePodModal onClose={() => setShowGenerateModal(false)} />
        )}
      </AnimatePresence>
    </div>
  )
}

function PodCard({ pod, index, onDelete }: { pod: Pod; index: number; onDelete: () => void }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.1 }}
      className="card group"
    >
      <div className="flex items-start justify-between mb-4">
        <div className="flex items-center gap-3">
          <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${
            pod.status === 'online' ? 'bg-rt-success/10' :
            pod.status === 'pending' ? 'bg-rt-warning/10' :
            'bg-rt-surface'
          }`}>
            <Server className={`w-5 h-5 ${
              pod.status === 'online' ? 'text-rt-success' :
              pod.status === 'pending' ? 'text-rt-warning' :
              'text-rt-text-muted'
            }`} />
          </div>
          <div>
            <h3 className="font-display font-semibold">{pod.pod_name}</h3>
            <span className={`badge ${
              pod.status === 'online' ? 'badge-success' :
              pod.status === 'pending' ? 'badge-warning' :
              'badge-error'
            }`}>
              {pod.status}
            </span>
          </div>
        </div>
        <button
          onClick={onDelete}
          className="p-2 rounded-lg text-rt-text-muted hover:text-rt-accent hover:bg-rt-accent/10 opacity-0 group-hover:opacity-100 transition-all"
        >
          <Trash2 className="w-4 h-4" />
        </button>
      </div>

      <div className="space-y-2 text-sm mb-4">
        <div className="flex items-center justify-between">
          <span className="text-rt-text-muted">Hostname</span>
          <span className="font-mono text-xs">{pod.machine_hostname || 'Pending'}</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-rt-text-muted">OS</span>
          <span>{pod.os_type || 'Unknown'}</span>
        </div>
        {pod.last_heartbeat && (
          <div className="flex items-center justify-between">
            <span className="text-rt-text-muted">Last Seen</span>
            <span>{new Date(pod.last_heartbeat).toLocaleTimeString()}</span>
          </div>
        )}
      </div>

      <Link
        to={`/pods/${pod.pod_id}`}
        className="btn-secondary w-full flex items-center justify-center gap-2"
      >
        <ExternalLink className="w-4 h-4" />
        View Details
      </Link>
    </motion.div>
  )
}

function GeneratePodModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState('')
  const [os, setOs] = useState('linux')
  const [generatedPod, setGeneratedPod] = useState<{ 
    pod_id: string
    archive_name: string
    instructions: string
    binary_included: boolean
    download_url: string
  } | null>(null)
  const queryClient = useQueryClient()
  
  const generateMutation = useMutation({
    mutationFn: podApi.generate,
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['pods'] })
      setGeneratedPod(data)
      toast.success('POD generated successfully!')
    },
    onError: () => {
      toast.error('Failed to generate POD')
    },
  })
  
  const handleGenerate = () => {
    if (!name.trim()) {
      toast.error('Please enter a POD name')
      return
    }
    generateMutation.mutate({ pod_name: name, target_os: os })
  }

  return (
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
        className="bg-rt-bg-light border border-rt-border rounded-xl p-6 w-full max-w-lg max-h-[80vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-xl font-display font-semibold mb-6">
          {generatedPod ? 'POD Generated!' : 'Generate New POD'}
        </h2>
        
        {!generatedPod ? (
          <>
            <div className="space-y-4 mb-6">
              <div>
                <label className="label">POD Name</label>
                <input
                  type="text"
                  className="input"
                  placeholder="e.g., Production Server A"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              
              <div>
                <label className="label">Target Operating System</label>
                <div className="grid grid-cols-3 gap-3">
                  {['linux', 'macos', 'windows'].map((osType) => (
                    <button
                      key={osType}
                      type="button"
                      onClick={() => setOs(osType)}
                      className={`p-3 rounded-lg border text-sm font-medium transition-all ${
                        os === osType
                          ? 'border-rt-primary bg-rt-primary/10 text-rt-primary'
                          : 'border-rt-border hover:border-rt-text-muted'
                      }`}
                    >
                      {osType.charAt(0).toUpperCase() + osType.slice(1)}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            
            <div className="flex gap-3 justify-end">
              <button onClick={onClose} className="btn-secondary">
                Cancel
              </button>
              <button
                onClick={handleGenerate}
                disabled={generateMutation.isPending}
                className="btn-primary flex items-center gap-2"
              >
                {generateMutation.isPending ? (
                  <>
                    <div className="w-4 h-4 border-2 border-rt-bg border-t-transparent rounded-full animate-spin" />
                    Generating...
                  </>
                ) : (
                  <>
                    <Download className="w-4 h-4" />
                    Generate
                  </>
                )}
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="mb-6">
              <div className="flex items-center gap-3 mb-4">
                <div className="w-12 h-12 rounded-xl bg-rt-success/10 flex items-center justify-center">
                  <Download className="w-6 h-6 text-rt-success" />
                </div>
                <div>
                  <h3 className="font-semibold">POD Package Ready!</h3>
                  <p className="text-sm text-rt-text-muted">{generatedPod.archive_name}</p>
                </div>
              </div>
              
              <a
                href={`http://localhost:8000${generatedPod.download_url}`}
                download
                className="btn-primary w-full flex items-center justify-center gap-2 mb-4"
              >
                <Download className="w-4 h-4" />
                Download POD Package
              </a>
              
              {!generatedPod.binary_included && (
                <div className="bg-rt-warning/10 border border-rt-warning/20 rounded-lg p-3 mb-4">
                  <p className="text-sm text-rt-warning flex items-center gap-2">
                    <AlertTriangle className="w-4 h-4" />
                    Binary not included. You'll need to build it separately.
                  </p>
                </div>
              )}
              
              <div className="bg-rt-surface rounded-lg p-4">
                <p className="text-sm font-medium mb-2">Quick Start:</p>
                <pre className="text-xs font-mono whitespace-pre-wrap text-rt-text-muted">
{generatedPod.instructions}
                </pre>
              </div>
            </div>
            
            <button onClick={onClose} className="btn-secondary w-full">
              Close
            </button>
          </>
        )}
      </motion.div>
    </motion.div>
  )
}
