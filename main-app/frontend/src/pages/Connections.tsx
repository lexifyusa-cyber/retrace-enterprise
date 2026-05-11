/**
 * Connections settings page — manage stored credentials for external
 * data sources (SFTP, SMB, S3, Azure Blob).
 */

import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Plus, Server, Trash2, RefreshCw, Loader2, X, CheckCircle2, AlertCircle,
  HardDrive, Cloud, FolderArchive, Box,
} from 'lucide-react'
import toast from 'react-hot-toast'
import {
  connectionsApi,
  type SourceConnection,
  type SourceSchemaField,
} from '@/utils/api'

type SourceTypeValue = 'sftp' | 'smb' | 's3' | 'azure_blob' | 'dropbox' | 'google_drive' | 'box'

const SOURCE_TYPES: {
  value: SourceTypeValue
  label: string
  description: string
  icon: React.ComponentType<{ className?: string }>
  oauth?: boolean
}[] = [
  { value: 'sftp',         label: 'SFTP',          description: 'Secure FTP / SSH file transfer',                icon: Server },
  { value: 'smb',          label: 'SMB / CIFS',    description: 'Windows network share',                          icon: HardDrive },
  { value: 's3',           label: 'AWS S3',        description: 'S3 bucket (or any S3-compatible: Minio, R2, …)', icon: FolderArchive },
  { value: 'azure_blob',   label: 'Azure Blob',    description: 'Azure Storage container',                        icon: Cloud },
  { value: 'dropbox',      label: 'Dropbox',       description: 'Connect a Dropbox account via OAuth',            icon: Box,   oauth: true },
  { value: 'google_drive', label: 'Google Drive',  description: 'Connect a Google Drive account via OAuth',       icon: Cloud, oauth: true },
  { value: 'box',          label: 'Box',           description: 'Connect a Box account via OAuth',                icon: Box,   oauth: true },
]

/**
 * ConnectionsManager — the body content (no page wrapper). Reused inside
 * Settings.tsx as a collapsible section. The default export wraps it in a
 * full-page layout so the /connections route still works as a standalone view.
 */
export function ConnectionsManager({ compact = false }: { compact?: boolean }) {
  const [showCreate, setShowCreate] = useState(false)
  const queryClient = useQueryClient()

  const { data: connections = [], isLoading } = useQuery<SourceConnection[]>({
    queryKey: ['connections'],
    queryFn: () => connectionsApi.list(),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => connectionsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connections'] })
      toast.success('Connection removed')
    },
    onError: (err: any) => toast.error(err?.response?.data?.detail || 'Delete failed'),
  })

  const testMutation = useMutation({
    mutationFn: (id: string) => connectionsApi.test(id),
    onSuccess: (res, _id) => {
      queryClient.invalidateQueries({ queryKey: ['connections'] })
      toast[res.ok ? 'success' : 'error'](res.message)
    },
  })

  return (
    <div className={compact ? '' : 'p-6 max-w-5xl mx-auto'}>
      {!compact && (
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-display font-semibold mb-1">Data Source Connections</h1>
            <p className="text-sm text-rt-text-muted">
              Stored credentials for SFTP / SMB / S3 / Azure used to ingest training data.
            </p>
          </div>
          <button
            onClick={() => setShowCreate(true)}
            className="btn-primary flex items-center gap-2"
          >
            <Plus className="w-4 h-4" /> Add Connection
          </button>
        </div>
      )}
      {compact && (
        <div className="flex items-center justify-end mb-3">
          <button
            onClick={() => setShowCreate(true)}
            className="btn-primary flex items-center gap-1.5 text-xs py-1.5 px-3"
          >
            <Plus className="w-3.5 h-3.5" /> Add Connection
          </button>
        </div>
      )}

      {isLoading ? (
        <div className="text-center py-12 text-rt-text-muted text-sm">
          <Loader2 className="w-5 h-5 mx-auto animate-spin mb-2" />
          Loading connections…
        </div>
      ) : connections.length === 0 ? (
        <div className="border border-dashed border-rt-border rounded-lg p-10 text-center">
          <Server className="w-8 h-8 mx-auto text-rt-text-muted/60 mb-3" />
          <p className="text-sm text-rt-text-muted mb-3">No connections yet.</p>
          <p className="text-xs text-rt-text-muted/80 mb-5 max-w-md mx-auto">
            Add an SFTP server, SMB share, S3 bucket, or Azure Blob container to use as a
            training-data source on any team's "Add Training Data" dialog.
          </p>
          <button onClick={() => setShowCreate(true)} className="btn-primary">
            Add Your First Connection
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {connections.map((c) => {
            const meta = SOURCE_TYPES.find((s) => s.value === c.source_type)
            const Icon = meta?.icon ?? Server
            return (
              <div key={c.connection_id} className="bg-rt-bg-light border border-rt-border rounded-lg p-4">
                <div className="flex items-start justify-between gap-3 mb-3">
                  <div className="flex items-start gap-3 flex-1 min-w-0">
                    <div className="p-2 rounded-md bg-rt-primary/10 text-rt-primary flex-shrink-0">
                      <Icon className="w-5 h-5" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <h3 className="font-medium truncate">{c.name}</h3>
                      <p className="text-xs text-rt-text-muted">{meta?.label ?? c.source_type}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-1 flex-shrink-0">
                    <button
                      onClick={() => testMutation.mutate(c.connection_id)}
                      disabled={testMutation.isPending}
                      title="Test"
                      className="p-1.5 rounded text-rt-text-muted hover:text-rt-primary hover:bg-rt-primary/10"
                    >
                      <RefreshCw className={`w-3.5 h-3.5 ${testMutation.isPending ? 'animate-spin' : ''}`} />
                    </button>
                    <button
                      onClick={() => {
                        if (confirm(`Remove "${c.name}"? Folder groups using this connection will fail to refresh until re-attached.`)) {
                          deleteMutation.mutate(c.connection_id)
                        }
                      }}
                      title="Delete"
                      className="p-1.5 rounded text-rt-text-muted hover:text-red-500 hover:bg-red-500/10"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>

                <div className="flex items-center gap-2 text-xs">
                  {c.last_test_status === 'ok' ? (
                    <span className="inline-flex items-center gap-1 text-green-500">
                      <CheckCircle2 className="w-3.5 h-3.5" /> Connected
                    </span>
                  ) : c.last_test_status === 'error' ? (
                    <span className="inline-flex items-center gap-1 text-amber-500" title={c.last_test_error || ''}>
                      <AlertCircle className="w-3.5 h-3.5" /> Error
                    </span>
                  ) : (
                    <span className="text-rt-text-muted">Not tested yet</span>
                  )}
                  {c.last_tested_at && (
                    <span className="text-rt-text-muted">
                      · {new Date(c.last_tested_at).toLocaleString()}
                    </span>
                  )}
                </div>
                {c.last_test_status === 'error' && c.last_test_error && (
                  <div className="mt-2 text-[11px] text-amber-500/90 leading-relaxed bg-amber-500/5 border border-amber-500/15 rounded px-2 py-1.5">
                    {c.last_test_error}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      <AnimatePresence>
        {showCreate && (
          <CreateConnectionModal
            onClose={() => setShowCreate(false)}
            onCreated={() => {
              setShowCreate(false)
              queryClient.invalidateQueries({ queryKey: ['connections'] })
            }}
          />
        )}
      </AnimatePresence>
    </div>
  )
}


// Default export = standalone page (kept so /connections route still resolves)
export default function ConnectionsPage() {
  return <ConnectionsManager />
}


function CreateConnectionModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [step, setStep] = useState<'pick-type' | 'configure'>('pick-type')
  const [sourceType, setSourceType] = useState<SourceTypeValue | null>(null)
  const [name, setName] = useState('')
  const [config, setConfig] = useState<Record<string, any>>({})
  const [creating, setCreating] = useState(false)

  const sourceMeta = SOURCE_TYPES.find((s) => s.value === sourceType)
  const isOAuth = !!sourceMeta?.oauth

  const { data: schema } = useQuery({
    queryKey: ['connection-schema', sourceType],
    queryFn: () => connectionsApi.schema(sourceType!),
    enabled: !!sourceType && step === 'configure' && !isOAuth,
  })

  const startOAuth = async () => {
    if (!sourceType) return
    if (!name.trim()) {
      toast.error('Give the connection a name')
      return
    }
    setCreating(true)
    try {
      const { auth_url } = await connectionsApi.oauthInit(sourceType, name.trim())
      const popup = window.open(auth_url, 'retrace_oauth', 'width=520,height=720')
      if (!popup) {
        toast.error('Popup blocked. Allow popups for this site and try again.')
        setCreating(false)
        return
      }
      // Poll for popup close, then refresh the connections list.
      const onMsg = (e: MessageEvent) => {
        if (e?.data?.type === 'retrace-oauth') {
          if (e.data.ok) {
            toast.success(e.data.message || 'Connected')
          } else {
            toast.error(e.data.message || 'OAuth failed')
          }
        }
      }
      window.addEventListener('message', onMsg)
      const timer = setInterval(() => {
        if (popup.closed) {
          clearInterval(timer)
          window.removeEventListener('message', onMsg)
          setCreating(false)
          onCreated()
        }
      }, 500)
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to start OAuth')
      setCreating(false)
    }
  }

  // Initialize config with defaults when schema arrives
  useMemo(() => {
    if (schema) {
      const init: Record<string, any> = {}
      for (const f of schema.fields) {
        if (f.default !== undefined) init[f.name] = f.default
      }
      setConfig(init)
    }
  }, [schema])

  const handleCreate = async () => {
    if (!sourceType) return
    if (!name.trim()) {
      toast.error('Give the connection a name')
      return
    }
    // Required fields check
    if (schema) {
      for (const f of schema.fields) {
        if (f.required && !config[f.name]) {
          toast.error(`Missing: ${f.label}`)
          return
        }
      }
    }
    setCreating(true)
    try {
      const res = await connectionsApi.create({ name: name.trim(), source_type: sourceType, config })
      if (res.last_test_status === 'ok') {
        toast.success(`Connected "${res.name}"`)
      } else {
        toast.error(`Saved but test failed: ${res.last_test_error || 'unknown'}`)
      }
      onCreated()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to create')
    } finally {
      setCreating(false)
    }
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
        className="bg-rt-bg-light border border-rt-border rounded-xl p-6 w-full max-w-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-display font-semibold">
            {step === 'pick-type' ? 'Add Connection' : `Configure ${SOURCE_TYPES.find(s => s.value === sourceType)?.label}`}
          </h2>
          <button onClick={onClose} className="p-1.5 rounded hover:bg-rt-surface">
            <X className="w-4 h-4" />
          </button>
        </div>

        {step === 'pick-type' ? (
          <div className="space-y-2">
            <p className="text-sm text-rt-text-muted mb-3">Pick a source type:</p>
            {SOURCE_TYPES.map((s) => {
              const Icon = s.icon
              return (
                <button
                  key={s.value}
                  onClick={() => { setSourceType(s.value); setStep('configure') }}
                  className="w-full flex items-start gap-3 p-3 rounded-lg border border-rt-border hover:border-rt-primary/50 hover:bg-rt-primary/5 text-left transition-colors"
                >
                  <div className="p-2 rounded bg-rt-primary/10 text-rt-primary flex-shrink-0">
                    <Icon className="w-5 h-5" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-sm">{s.label}</div>
                    <div className="text-xs text-rt-text-muted">{s.description}</div>
                  </div>
                </button>
              )
            })}
          </div>
        ) : (
          <div className="space-y-3">
            <div>
              <label className="block text-xs text-rt-text-muted mb-1.5">Connection name</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Production SFTP"
                className="w-full bg-rt-surface border border-rt-border rounded px-3 py-2 text-sm focus:outline-none focus:border-rt-primary"
              />
            </div>
            {isOAuth ? (
              <div className="text-xs text-rt-text-muted bg-rt-surface/50 border border-rt-border rounded p-3 leading-relaxed">
                You'll be redirected to {sourceMeta?.label} to authorise Retrace. After you
                approve, the popup closes and the connection appears below.
              </div>
            ) : !schema ? (
              <div className="py-4 text-center text-sm text-rt-text-muted">
                <Loader2 className="w-4 h-4 mx-auto animate-spin" />
              </div>
            ) : (
              schema.fields.map((f: SourceSchemaField) => (
                <div key={f.name}>
                  <label className="block text-xs text-rt-text-muted mb-1.5">
                    {f.label}{f.required && <span className="text-red-500"> *</span>}
                  </label>
                  <input
                    type={f.type === 'password' ? 'password' : f.type === 'int' ? 'number' : 'text'}
                    value={config[f.name] ?? ''}
                    onChange={(e) => setConfig({ ...config, [f.name]: f.type === 'int' ? Number(e.target.value) : e.target.value })}
                    placeholder={f.default ? String(f.default) : ''}
                    className="w-full bg-rt-surface border border-rt-border rounded px-3 py-2 text-sm font-mono focus:outline-none focus:border-rt-primary"
                  />
                </div>
              ))
            )}

            <div className="flex items-center justify-between pt-2">
              <button onClick={() => setStep('pick-type')} className="text-xs text-rt-text-muted hover:text-rt-text">
                ← Back
              </button>
              <div className="flex gap-2">
                <button onClick={onClose} className="btn-secondary text-xs">Cancel</button>
                {isOAuth ? (
                  <button
                    onClick={startOAuth}
                    disabled={creating}
                    className="btn-primary text-xs flex items-center gap-1.5"
                  >
                    {creating && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                    Connect with {sourceMeta?.label}
                  </button>
                ) : (
                  <button
                    onClick={handleCreate}
                    disabled={creating}
                    className="btn-primary text-xs flex items-center gap-1.5"
                  >
                    {creating && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                    Create &amp; Test
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </motion.div>
    </motion.div>
  )
}
