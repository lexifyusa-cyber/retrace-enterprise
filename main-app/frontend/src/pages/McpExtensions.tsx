import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Puzzle, X, Loader2, Check, AlertCircle, Trash2, Settings,
  Zap, Eye, EyeOff,
  Server, UserCheck, Wrench,
  Store, Package, ShieldCheck, Download, Hammer,
} from 'lucide-react'
import toast from 'react-hot-toast'
import axios from 'axios'

const api = axios.create({ baseURL: '/api/v1/mcp-extensions' })
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('rt_access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// ── Types ────────────────────────────────────────────────────────
interface McpExtension {
  extension_id: string
  tenant_id: string | null
  name: string
  description: string | null
  command: string
  args_json: string[] | null
  env_vars_json: Record<string, string> | null
  auth_type: string
  auth_config_json: Record<string, any> | null
  is_enabled: boolean
  is_builtin: boolean
  mcp_server_id: string | null
  is_running: boolean
  created_by: string | null
  created_at: string | null
  updated_at: string | null
}

interface UserExtensionItem {
  user_extension_id: string
  extension_id: string
  user_id: string
  tenant_id: string | null
  is_enabled: boolean
  env_vars_json: Record<string, string> | null
  auth_config_json: Record<string, any> | null
  installed_at: string | null
  updated_at: string | null
  catalog: McpExtension
}

interface DiscoveredTool {
  name: string
  description: string
}

interface EnvVar {
  key: string
  value: string
}

const AUTH_LABELS: Record<string, string> = {
  none: 'None',
  basic: 'Basic Auth',
  auto_passthrough: 'Auto Pass-through',
  oauth_passthrough: 'OAuth Pass-through',
  ldap_passthrough: 'LDAP Pass-through',
  api_key: 'API Key',
}

const PASSTHROUGH_TYPES = ['oauth_passthrough', 'ldap_passthrough', 'auto_passthrough']

// ── Main Component ───────────────────────────────────────────────
export default function McpExtensions() {
  const [activeTab, setActiveTab] = useState<'my' | 'marketplace'>('my')

  const tabs = [
    { id: 'my' as const, label: 'My Extensions', icon: Package },
    { id: 'marketplace' as const, label: 'Marketplace', icon: Store },
  ]

  return (
    <div className="min-h-screen bg-rt-bg text-rt-text">
      {/* Header */}
      <div className="px-8 pt-8 pb-2">
        <div className="flex items-center gap-3 mb-4">
          <Puzzle className="w-6 h-6 text-rt-primary" />
          <div>
            <h1 className="text-xl font-bold">MCP Extensions</h1>
            <p className="text-sm text-rt-text-muted">
              Browse and install MCP extensions to extend the ReTrace agent
            </p>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex items-center gap-1 border-b border-rt-border">
          {tabs.map((tab) => {
            const Icon = tab.icon
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === tab.id
                    ? 'border-rt-primary text-rt-primary'
                    : 'border-transparent text-rt-text-muted hover:text-rt-text'
                }`}
              >
                <Icon className="w-4 h-4" />
                {tab.label}
              </button>
            )
          })}
        </div>
      </div>

      {/* Tab Content */}
      <div className="px-8 pb-8 pt-4">
        {activeTab === 'my' && <MyExtensionsTab />}
        {activeTab === 'marketplace' && <MarketplaceTab />}
      </div>
    </div>
  )
}


// ═══════════════════════════════════════════════════════════════════
// Tab 1: My Extensions
// ═══════════════════════════════════════════════════════════════════
function MyExtensionsTab() {
  const queryClient = useQueryClient()
  const [testResults, setTestResults] = useState<Record<string, { status: string; tools?: DiscoveredTool[]; error?: string }>>({})
  const [verifyResults, setVerifyResults] = useState<Record<string, { has_access: boolean; message: string }>>({})
  const [editingExt, setEditingExt] = useState<UserExtensionItem | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['my-extensions'],
    queryFn: async () => {
      const resp = await api.get('/my-extensions')
      return resp.data as { extensions: UserExtensionItem[] }
    },
  })

  const extensions = [...(data?.extensions || [])].sort((a, b) => {
    if (a.catalog.is_builtin && !b.catalog.is_builtin) return -1
    if (!a.catalog.is_builtin && b.catalog.is_builtin) return 1
    return 0
  })

  const toggleMutation = useMutation({
    mutationFn: async ({ id, enabled }: { id: string; enabled: boolean }) => {
      await api.patch(`/my-extensions/${id}`, { is_enabled: enabled })
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['my-extensions'] }),
  })

  const deleteMutation = useMutation({
    mutationFn: async (id: string) => { await api.delete(`/my-extensions/${id}`) },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['my-extensions'] })
      queryClient.invalidateQueries({ queryKey: ['catalog'] })
      toast.success('Extension deleted')
    },
    onError: () => toast.error('Failed to delete'),
  })

  const testMutation = useMutation({
    mutationFn: async (id: string) => {
      const resp = await api.post(`/my-extensions/${id}/test`)
      return { id, result: resp.data }
    },
    onSuccess: ({ id, result }) => {
      setTestResults((prev) => ({ ...prev, [id]: result }))
      if (result.status === 'ok') toast.success(`Connected! Found ${result.tool_count} tools`)
      else toast.error(`Connection failed: ${result.error}`)
    },
    onError: () => toast.error('Test connection failed'),
  })

  const verifyMutation = useMutation({
    mutationFn: async (id: string) => {
      // Find the extension to get the catalog extension_id for verify endpoint
      const ext = extensions.find((e) => e.user_extension_id === id)
      if (!ext) throw new Error('Extension not found')
      const resp = await api.post(`/${ext.extension_id}/validate-user-access`)
      return { id, result: resp.data }
    },
    onSuccess: ({ id, result }) => {
      setVerifyResults((prev) => ({ ...prev, [id]: result }))
      if (result.has_access) toast.success(result.message || 'Access verified')
      else toast.error(result.message || 'Access denied')
    },
    onError: () => toast.error('Access verification failed'),
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-6 h-6 animate-spin text-rt-text-muted" />
      </div>
    )
  }

  if (extensions.length === 0) {
    return (
      <div className="text-center py-20">
        <Package className="w-12 h-12 text-rt-text-muted/30 mx-auto mb-4" />
        <p className="text-rt-text-muted">No extensions available</p>
        <p className="text-sm text-rt-text-muted/60 mt-1">
          Built-in extensions will appear here automatically. You can also build custom extensions with MCP Builder.
        </p>
      </div>
    )
  }

  return (
    <>
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
        {extensions.map((ext) => (
          <MyExtensionCard
            key={ext.user_extension_id}
            ext={ext}
            onToggleEnabled={(enabled) => toggleMutation.mutate({ id: ext.user_extension_id, enabled })}
            onDelete={() => {
              if (confirm(`Delete "${ext.catalog.name}"? This will remove the extension, server registry, and code files from disk.`)) deleteMutation.mutate(ext.user_extension_id)
            }}
            onTest={() => testMutation.mutate(ext.user_extension_id)}
            onVerifyAccess={() => verifyMutation.mutate(ext.user_extension_id)}
            onEdit={() => setEditingExt(ext)}
            testResult={testResults[ext.user_extension_id]}
            verifyResult={verifyResults[ext.user_extension_id]}
            isTesting={testMutation.isPending && testMutation.variables === ext.user_extension_id}
            isVerifying={verifyMutation.isPending && verifyMutation.variables === ext.user_extension_id}
          />
        ))}
      </div>

      {/* Edit user extension dialog */}
      <AnimatePresence>
        {editingExt && (
          <EditUserExtensionDialog
            ext={editingExt}
            onClose={() => setEditingExt(null)}
            onSaved={() => {
              setEditingExt(null)
              queryClient.invalidateQueries({ queryKey: ['my-extensions'] })
            }}
          />
        )}
      </AnimatePresence>
    </>
  )
}


// ── My Extension Card ────────────────────────────────────────────
function MyExtensionCard({
  ext,
  onToggleEnabled,
  onDelete,
  onTest,
  onVerifyAccess,
  onEdit,
  testResult,
  verifyResult,
  isTesting,
  isVerifying,
}: {
  ext: UserExtensionItem
  onToggleEnabled: (enabled: boolean) => void
  onDelete: () => void
  onTest: () => void
  onVerifyAccess: () => void
  onEdit: () => void
  testResult?: { status: string; tools?: DiscoveredTool[]; error?: string }
  verifyResult?: { has_access: boolean; message: string }
  isTesting: boolean
  isVerifying: boolean
}) {
  const catalog = ext.catalog
  const authLabel = AUTH_LABELS[catalog.auth_type] || catalog.auth_type

  return (
    <motion.div
      layout
      className={`rounded-xl border transition-colors ${
        ext.is_enabled
          ? 'border-rt-border bg-rt-surface'
          : 'border-rt-border/50 bg-rt-surface/50 opacity-60'
      }`}
    >
      <div className="p-4">
        <div className="flex items-start justify-between mb-2">
          <div className="flex items-center gap-2 min-w-0">
            <Server className="w-5 h-5 text-rt-primary flex-shrink-0" />
            <h3 className="font-semibold truncate">{catalog.name}</h3>
            {catalog.is_builtin && (
              <span className="px-1.5 py-0.5 rounded-full text-[9px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 whitespace-nowrap">
                Built-in
              </span>
            )}
            {catalog.mcp_server_id && !catalog.is_builtin && (
              <span className="px-1.5 py-0.5 rounded-full text-[9px] font-medium bg-violet-500/10 text-violet-400 border border-violet-500/20 whitespace-nowrap flex items-center gap-0.5">
                <Hammer className="w-2.5 h-2.5" /> Custom Build
              </span>
            )}
          </div>
          <label className="relative inline-flex items-center cursor-pointer flex-shrink-0">
            <input
              type="checkbox"
              checked={ext.is_enabled}
              onChange={(e) => onToggleEnabled(e.target.checked)}
              className="sr-only peer"
            />
            <div className="w-8 h-4 bg-rt-border rounded-full peer peer-checked:bg-rt-primary transition-colors after:content-[''] after:absolute after:top-0.5 after:left-[2px] after:bg-white after:rounded-full after:h-3 after:w-3 after:transition-all peer-checked:after:translate-x-full" />
          </label>
        </div>

        {catalog.description && (
          <p className="text-xs text-rt-text-muted mb-3 line-clamp-2">{catalog.description}</p>
        )}

        <div className="flex items-center gap-2 mb-3">
          <code className="text-[11px] bg-rt-bg/50 px-2 py-1 rounded text-rt-text-muted truncate flex-1">
            {catalog.command} {(catalog.args_json || []).join(' ')}
          </code>
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-rt-bg/50 text-rt-text-muted border border-rt-border/50">
            {authLabel}
          </span>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1">
          <button onClick={onTest} disabled={isTesting}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded hover:bg-rt-primary/10 text-rt-primary transition-colors disabled:opacity-50">
            {isTesting ? <Loader2 className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />}
            Test
          </button>
          {PASSTHROUGH_TYPES.includes(catalog.auth_type) && (
            <button onClick={onVerifyAccess} disabled={isVerifying}
              className="flex items-center gap-1 px-2 py-1 text-xs rounded hover:bg-emerald-500/10 text-emerald-400 transition-colors disabled:opacity-50">
              {isVerifying ? <Loader2 className="w-3 h-3 animate-spin" /> : <UserCheck className="w-3 h-3" />}
              Verify Access
            </button>
          )}
          <button onClick={onEdit}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded hover:bg-rt-surface text-rt-text-muted transition-colors">
            <Settings className="w-3 h-3" /> {PASSTHROUGH_TYPES.includes(catalog.auth_type) ? 'Details' : 'Edit'}
          </button>
          {!catalog.is_builtin && (
            <button onClick={onDelete}
              className="flex items-center gap-1 px-2 py-1 text-xs rounded hover:bg-red-500/10 text-red-400 transition-colors">
              <Trash2 className="w-3 h-3" /> Delete
            </button>
          )}
        </div>
      </div>

      {/* Test results */}
      {testResult && (
        <div className={`border-t px-4 py-3 ${
          testResult.status === 'ok' ? 'border-emerald-500/20 bg-emerald-500/5' : 'border-red-500/20 bg-red-500/5'
        }`}>
          {testResult.status === 'ok' ? (
            <div>
              <div className="flex items-center gap-1 text-xs text-emerald-400 mb-1">
                <Check className="w-3 h-3" /> Connected — {testResult.tools?.length || 0} tools discovered
              </div>
              {testResult.tools && testResult.tools.length > 0 && (
                <div className="space-y-1 mt-2">
                  {testResult.tools.map((t) => (
                    <div key={t.name} className="text-[11px] text-rt-text-muted">
                      <span className="font-mono text-rt-text">{t.name}</span>
                      {t.description && <span className="ml-1">— {t.description.slice(0, 80)}</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="flex items-center gap-1 text-xs text-red-400">
              <AlertCircle className="w-3 h-3" /> {testResult.error}
            </div>
          )}
        </div>
      )}

      {/* Verify access result */}
      {verifyResult && (
        <div className={`border-t px-4 py-3 ${
          verifyResult.has_access ? 'border-emerald-500/20 bg-emerald-500/5' : 'border-amber-500/20 bg-amber-500/5'
        }`}>
          <div className={`flex items-center gap-1 text-xs ${
            verifyResult.has_access ? 'text-emerald-400' : 'text-amber-400'
          }`}>
            {verifyResult.has_access ? <UserCheck className="w-3 h-3" /> : <AlertCircle className="w-3 h-3" />}
            {verifyResult.message}
          </div>
          {(verifyResult as any).debug && (
            <div className="mt-2 text-[10px] text-rt-text-muted font-mono bg-rt-bg/50 rounded p-2 space-y-0.5">
              <div>token_type: {(verifyResult as any).debug.token_type}</div>
              <div>aud: {(verifyResult as any).debug.aud}</div>
              <div>iss: {(verifyResult as any).debug.iss}</div>
              <div>email: {(verifyResult as any).debug.email || 'null'}</div>
              <div>upn: {(verifyResult as any).debug.upn || 'null'}</div>
              <div>preferred_username: {(verifyResult as any).debug.preferred_username || 'null'}</div>
            </div>
          )}
        </div>
      )}
    </motion.div>
  )
}


// ── Edit User Extension Dialog ───────────────────────────────────
function EditUserExtensionDialog({
  ext,
  onClose,
  onSaved,
}: {
  ext: UserExtensionItem
  onClose: () => void
  onSaved: () => void
}) {
  const isPassthrough = PASSTHROUGH_TYPES.includes(ext.catalog.auth_type)

  // For passthrough types: no editable env vars (admin-configured).
  // For others: merge catalog defaults with user overrides so user sees full picture.
  const [envVars, setEnvVars] = useState<EnvVar[]>(() => {
    if (isPassthrough) return []
    // Merge: start with catalog defaults, overlay user overrides
    const catalogVars = ext.catalog.env_vars_json || {}
    const userVars = ext.env_vars_json || {}
    const merged = { ...catalogVars, ...userVars }
    const entries = Object.entries(merged)
    return entries.length > 0 ? entries.map(([key, value]) => ({ key, value: value as string })) : [{ key: '', value: '' }]
  })
  const [saving, setSaving] = useState(false)
  const [showSecrets, setShowSecrets] = useState(false)

  const handleSave = async () => {
    setSaving(true)
    try {
      const envObj: Record<string, string> = {}
      for (const ev of envVars) {
        if (ev.key.trim() && ev.value !== '••••••••') {
          envObj[ev.key.trim()] = ev.value
        }
      }
      await api.patch(`/my-extensions/${ext.user_extension_id}`, {
        env_vars_json: Object.keys(envObj).length > 0 ? envObj : null,
      })
      toast.success('Extension updated')
      onSaved()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  const addEnvVar = () => setEnvVars([...envVars, { key: '', value: '' }])
  const removeEnvVar = (idx: number) => setEnvVars(envVars.filter((_, i) => i !== idx))
  const updateEnvVar = (idx: number, field: 'key' | 'value', val: string) => {
    const updated = [...envVars]
    updated[idx] = { ...updated[idx], [field]: val }
    setEnvVars(updated)
  }

  // Catalog env vars for read-only display (oauth_passthrough)
  const catalogEnvVars = ext.catalog.env_vars_json || {}

  return (
    <motion.div
      initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.95, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.95, opacity: 0 }}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-xl max-h-[85vh] overflow-y-auto bg-rt-surface border border-rt-border rounded-2xl shadow-2xl"
      >
        <div className="flex items-center justify-between p-5 border-b border-rt-border">
          <h2 className="text-lg font-semibold">{isPassthrough ? 'Details' : 'Edit'} — {ext.catalog.name}</h2>
          <button onClick={onClose} className="text-rt-text-muted hover:text-rt-text"><X className="w-5 h-5" /></button>
        </div>

        <div className="p-5 space-y-4">
          {isPassthrough ? (
            <div className="space-y-3">
              {/* OAuth info banner */}
              <div className="bg-emerald-500/5 border border-emerald-500/15 rounded-lg p-3">
                <div className="flex items-start gap-2">
                  <ShieldCheck className="w-4 h-4 text-emerald-400 mt-0.5 shrink-0" />
                  <div>
                    <p className="text-xs font-medium text-emerald-400">Pre-configured by your admin</p>
                    <p className="text-[11px] text-rt-text-muted mt-1">
                      Your credentials are used automatically for authentication. No configuration needed.
                    </p>
                  </div>
                </div>
              </div>
              {/* Show catalog config as read-only info */}
              {Object.keys(catalogEnvVars).length > 0 && (
                <div>
                  <label className="text-xs font-medium text-rt-text-muted mb-1 block">Connection Details</label>
                  <div className="bg-rt-bg/50 rounded-lg border border-rt-border p-3 space-y-1.5">
                    {Object.entries(catalogEnvVars).map(([key, value]) => (
                      <div key={key} className="flex items-center gap-2 text-xs">
                        <span className="font-mono text-rt-text-muted">{key}:</span>
                        <span className="font-mono text-rt-text">{value as string}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            /* Editable env vars for non-oauth auth types */
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs font-medium text-rt-text-muted">Environment Variables</label>
                <div className="flex items-center gap-2">
                  <button onClick={() => setShowSecrets(!showSecrets)}
                    className="text-[10px] text-rt-text-muted hover:text-rt-text flex items-center gap-1">
                    {showSecrets ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                    {showSecrets ? 'Hide' : 'Show'} values
                  </button>
                  <button onClick={addEnvVar} className="text-[10px] text-rt-primary hover:text-rt-primary/80">+ Add</button>
                </div>
              </div>
              <div className="space-y-1.5">
                {envVars.map((ev, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <input value={ev.key} onChange={(e) => updateEnvVar(i, 'key', e.target.value)}
                      placeholder="KEY"
                      className="flex-1 px-2 py-1.5 bg-rt-bg border border-rt-border rounded text-xs font-mono focus:outline-none focus:ring-1 focus:ring-rt-primary" />
                    <input value={ev.value} onChange={(e) => updateEnvVar(i, 'value', e.target.value)}
                      placeholder="value" type={showSecrets ? 'text' : 'password'}
                      className="flex-1 px-2 py-1.5 bg-rt-bg border border-rt-border rounded text-xs font-mono focus:outline-none focus:ring-1 focus:ring-rt-primary" />
                    {envVars.length > 1 && (
                      <button onClick={() => removeEnvVar(i)} className="text-rt-text-muted hover:text-red-400">
                        <X className="w-3.5 h-3.5" />
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-3 p-5 border-t border-rt-border">
          {isPassthrough ? (
            <button onClick={onClose} className="px-4 py-2 text-sm text-rt-text-muted hover:text-rt-text bg-rt-bg border border-rt-border rounded-lg">Close</button>
          ) : (
            <>
              <button onClick={onClose} className="px-4 py-2 text-sm text-rt-text-muted hover:text-rt-text">Cancel</button>
              <button onClick={handleSave} disabled={saving}
                className="flex items-center gap-2 px-4 py-2 bg-rt-primary text-white rounded-lg hover:bg-rt-primary/90 text-sm font-medium disabled:opacity-50">
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                Save Changes
              </button>
            </>
          )}
        </div>
      </motion.div>
    </motion.div>
  )
}


// ═══════════════════════════════════════════════════════════════════
// Tab 2: Marketplace (Coming Soon)
// ═══════════════════════════════════════════════════════════════════
function MarketplaceTab() {
  return (
    <div className="text-center py-24">
      <div className="w-16 h-16 rounded-2xl bg-rt-primary/10 flex items-center justify-center mx-auto mb-5">
        <Store className="w-8 h-8 text-rt-primary" />
      </div>
      <h2 className="text-lg font-semibold mb-2">Marketplace Coming Soon</h2>
      <p className="text-sm text-rt-text-muted max-w-md mx-auto">
        Browse and install MCP extensions built by the team. New extensions will be available here in a future update.
      </p>
      <div className="mt-6 flex items-center justify-center gap-4">
        <div className="flex items-center gap-2 text-xs text-rt-text-muted/60">
          <Download className="w-4 h-4" /> One-click install
        </div>
        <div className="flex items-center gap-2 text-xs text-rt-text-muted/60">
          <ShieldCheck className="w-4 h-4" /> Pre-configured auth
        </div>
        <div className="flex items-center gap-2 text-xs text-rt-text-muted/60">
          <Wrench className="w-4 h-4" /> Ready-to-use tools
        </div>
      </div>
    </div>
  )
}


