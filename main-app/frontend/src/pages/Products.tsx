import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Package, Trash2, Play, X, Folder, FolderOpen, ChevronRight, ChevronDown, Check, FolderTree, FileCode, FileText, Image, TicketIcon, AlertTriangle, Database, Layers, File, Brain, Download, Monitor, Server, Loader2, Mail, Copy, CheckCircle } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import toast from 'react-hot-toast'
import { podApi, productApi, groupApi, localFilesApi, emailApi } from '@/utils/api'
import { Product, Pod, FileEntry, FolderGroup } from '@/types'
import TrainingProgressModal from '@/components/TrainingProgressModal'
import { useAuth } from '@/context/AuthContext'
import { isAdmin as checkIsAdmin } from '@/utils/roles'

export default function Products() {
  const { user: authUser } = useAuth()
  const isAdmin = checkIsAdmin(authUser?.role)
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [trainingProductId, setTrainingProductId] = useState<string | null>(null)
  const [showProgressModal, setShowProgressModal] = useState(false)
  const queryClient = useQueryClient()
  
  const { data: products = [], isLoading } = useQuery({
    queryKey: ['products'],
    queryFn: () => productApi.list(),
  })
  
  const { data: pods = [] } = useQuery({
    queryKey: ['pods'],
    queryFn: podApi.list,
  })
  
  const deleteMutation = useMutation({
    mutationFn: productApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['products'] })
      toast.success('Product deleted')
    },
    onError: () => {
      toast.error('Failed to delete product')
    },
  })
  
  const trainMutation = useMutation({
    mutationFn: productApi.trainAll,
    onSuccess: (data: any, productId: string) => {
      queryClient.invalidateQueries({ queryKey: ['products'] })
      
      // Open progress modal for this product
      setTrainingProductId(productId)
      setShowProgressModal(true)
      
      toast.success(`Training started for ${data.folder_groups || 0} folder group${(data.folder_groups || 0) === 1 ? '' : 's'}`)
    },
    onError: (error: any) => {
      toast.error(error?.response?.data?.detail || 'Failed to start training')
    },
  })
  
  // Check if any product is currently training (for the floating reopen button)
  const anyTrainingProduct = products.find(p => 
    p.folder_groups.some(g => g.training_status === 'training')
  )

  return (
    <div className="p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-2xl font-display font-bold mb-1">Products</h1>
          <p className="text-rt-text-muted text-sm">
            Manage products and their training data
          </p>
        </div>
        {isAdmin && (
          <button
            onClick={() => setShowCreateModal(true)}
            className="btn-primary flex items-center gap-2"
          >
            <Plus className="w-4 h-4" />
            Create Product
          </button>
        )}
      </div>

      {/* Products */}
      {isLoading ? (
        <div className="space-y-4">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="card animate-pulse">
              <div className="h-6 bg-rt-surface rounded w-48 mb-4" />
              <div className="h-4 bg-rt-surface rounded w-full" />
            </div>
          ))}
        </div>
      ) : products.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="card text-center py-16"
        >
          <Package className="w-16 h-16 mx-auto text-rt-text-muted mb-4" />
          <h3 className="text-xl font-display font-semibold mb-2">No Products</h3>
          <p className="text-rt-text-muted mb-6 max-w-md mx-auto">
            Create a product to organize your folder groups.
          </p>
          <button
            onClick={() => setShowCreateModal(true)}
            className="btn-primary"
          >
            Create Your First Product
          </button>
        </motion.div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {products.map((product) => (
            <ProductCard
              key={product.product_id}
              product={product}
              pods={pods}
              isAdmin={isAdmin}
              onDelete={() => {
                if (confirm(`Delete "${product.product_name}" and all its folder groups?`)) {
                  deleteMutation.mutate(product.product_id)
                }
              }}
              onTrain={() => trainMutation.mutate(product.product_id)}
              isTraining={trainMutation.isPending}
            />
          ))}
        </div>
      )}

      {/* Create Modal */}
      <AnimatePresence>
        {showCreateModal && (
          <CreateProductModal
            pods={pods}
            onClose={() => setShowCreateModal(false)}
          />
        )}
      </AnimatePresence>

      {/* Floating "View Training Progress" button — only when training is actually running */}
      <AnimatePresence>
        {!showProgressModal && anyTrainingProduct && (
          <motion.button
            initial={{ opacity: 0, y: 50 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 50 }}
            onClick={() => {
              setTrainingProductId(anyTrainingProduct.product_id)
              setShowProgressModal(true)
            }}
            className="fixed bottom-6 right-6 z-40 flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-3 rounded-full shadow-lg transition-colors"
          >
            <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
            <span className="text-sm font-medium">View Training Progress</span>
          </motion.button>
        )}
      </AnimatePresence>

      {/* Training Progress Modal */}
      <AnimatePresence>
        {showProgressModal && trainingProductId && (
          <TrainingProgressModal
            productId={trainingProductId}
            onClose={() => {
              setShowProgressModal(false)
              setTrainingProductId(null)
              queryClient.invalidateQueries({ queryKey: ['products'] })
            }}
          />
        )}
      </AnimatePresence>
    </div>
  )
}

function ProductCard({
  product,
  pods,
  isAdmin,
  onDelete,
  onTrain,
  isTraining,
}: {
  product: Product
  pods: Pod[]
  isAdmin: boolean
  onDelete: () => void
  onTrain: () => void
  isTraining: boolean
}) {
  const [showDescription, setShowDescription] = useState(false)
  const [showAddGroupModal, setShowAddGroupModal] = useState(false)
  const [showTrainingLog, setShowTrainingLog] = useState(false)
  const [showTrainingTree, setShowTrainingTree] = useState(false)
  const [emailExpanded, setEmailExpanded] = useState(false)
  const [showDraftsModal, setShowDraftsModal] = useState(false)
  const [showUnreadModal, setShowUnreadModal] = useState(false)
  const queryClient = useQueryClient()

  // Email inbox for this product
  const { data: emailConnections = [] } = useQuery<any[]>({
    queryKey: ['email', 'connections'],
    queryFn: () => emailApi.listConnections(),
  })

  const { data: inbox } = useQuery<any>({
    queryKey: ['email', 'inbox', product.product_id],
    queryFn: () => emailApi.getInbox(product.product_id),
  })

  const createInboxMutation = useMutation({
    mutationFn: (connectionId: string) =>
      emailApi.createInbox(product.product_id, { connection_id: connectionId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['email', 'inbox', product.product_id] })
      toast.success('AI Email inbox created')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.detail || 'Failed to create inbox')
    },
  })

  const updateInboxMutation = useMutation({
    mutationFn: (config: Record<string, any>) =>
      emailApi.updateInbox(product.product_id, { action_config: config }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['email', 'inbox', product.product_id] })
      toast.success('Email settings updated')
    },
    onError: () => {
      toast.error('Failed to update email settings')
    },
  })

  const deleteInboxMutation = useMutation({
    mutationFn: () => emailApi.deleteInbox(product.product_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['email', 'inbox', product.product_id] })
      toast.success('AI Email inbox removed')
    },
    onError: () => {
      toast.error('Failed to remove inbox')
    },
  })

  const removeGroupMutation = useMutation({
    mutationFn: () => productApi.removeFolderGroup(product.product_id, ''),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['products'] })
      toast.success('Folder group removed')
    },
    onError: () => {
      toast.error('Failed to remove folder group')
    },
  })

  const groupTypeColors: Record<string, string> = {
    code: 'bg-blue-500/10 text-blue-400',
    documentation: 'bg-green-500/10 text-green-400',
    diagrams: 'bg-purple-500/10 text-purple-400',
    configuration: 'bg-yellow-500/10 text-yellow-400',
    tickets: 'bg-orange-500/10 text-orange-400',
    other: 'bg-gray-500/10 text-rt-text-muted',
  }

  return (
    <div className="card !p-4 flex flex-col h-full min-h-0">
      <div className="flex items-center justify-between mb-3 flex-shrink-0">
        <div className="flex items-center gap-2.5 flex-1 min-w-0">
          <Package className="w-5 h-5 text-rt-primary flex-shrink-0" />
          <div className="flex-1 min-w-0">
            <button
              onClick={() => product.description && setShowDescription(!showDescription)}
              className={`text-left ${product.description ? 'cursor-pointer hover:text-rt-primary transition-colors' : ''}`}
            >
              <h2 className="text-lg font-display font-semibold truncate">{product.product_name}</h2>
            </button>
            {product.description && showDescription && (
              <div className="mt-1 text-xs text-rt-text-muted leading-relaxed">
                {product.description}
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className="text-xs text-rt-text-muted">
            {product.folder_groups.length} {product.folder_groups.length === 1 ? 'group' : 'groups'}
          </span>
          {isAdmin && product.folder_groups.length > 0 && (
            <button
              onClick={onTrain}
              disabled={isTraining || product.folder_groups.some(g => g.training_status === 'training')}
              className="btn-primary text-xs py-1 px-2.5 flex items-center gap-1.5"
            >
              <Play className="w-3 h-3" />
              {isTraining ? 'Starting...' : 'Train'}
            </button>
          )}
          {isAdmin && (
            <button
              onClick={onDelete}
              className="p-1.5 rounded text-rt-text-muted hover:text-rt-accent hover:bg-rt-accent/10 transition-all"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Training Results — fixed height for consistent card layout */}
      <div className="mb-3 min-h-0 overflow-y-auto rounded-lg border border-rt-border bg-rt-bg-light/50 flex-shrink-0 max-h-[180px]">
      {(() => {
        // Get stats from any group that has training metadata (logs, files_kept, or legacy files_discovered)
        const statsGroup = product.folder_groups.find(g => g.metadata && (
          Array.isArray(g.metadata.logs) ||
          g.metadata.files_kept != null ||
          g.metadata.files_discovered != null ||
          g.metadata.phase === 'completed'
        ))
        const stats = statsGroup?.metadata as Record<string, any> | undefined
        const isAnyTraining = product.folder_groups.some(g => g.training_status === 'training')
        const isAnyCompleted = product.folder_groups.some(g => g.training_status === 'completed' && g.last_trained)
        const isAnyFailed = product.folder_groups.some(g => g.training_status === 'failed')
        
        if (isAnyTraining) {
          return (
            <div className="mb-4 p-3 bg-yellow-500/5 border border-yellow-500/20 rounded-lg flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="w-4 h-4 border-2 border-yellow-500 border-t-transparent rounded-full animate-spin" />
                <span className="text-sm text-yellow-400">Training in progress…</span>
              </div>
              <button
                onClick={async () => {
                  if (!confirm('Stop training?')) return
                  try {
                    await productApi.stopTraining(product.product_id)
                    queryClient.invalidateQueries({ queryKey: ['products'] })
                  } catch (e: any) {
                    // If stop fails, force refresh anyway
                    queryClient.invalidateQueries({ queryKey: ['products'] })
                  }
                }}
                className="text-xs px-2 py-1 rounded bg-red-500/10 text-red-400 hover:bg-red-500/20 border border-red-500/20 transition-colors"
              >
                Stop
              </button>
            </div>
          )
        }
        
        if (isAnyFailed && !isAnyCompleted) {
          const failedGroup = product.folder_groups.find(g => g.training_status === 'failed')
          const failMeta = failedGroup?.metadata as Record<string, any> | undefined
          const failLogs = failMeta?.logs as string[] | undefined
          return (
            <div className="mb-4 p-3 bg-red-500/5 border border-red-500/20 rounded-lg">
              <div className="flex items-center gap-3">
                <AlertTriangle className="w-4 h-4 text-red-400" />
                <span className="text-sm text-red-400">
                  {failMeta?.message || 'Training failed. Check your LLM API key in Settings.'}
                </span>
              </div>
              {failLogs && failLogs.length > 0 && (
                <div className="mt-2 pt-2 border-t border-red-500/20">
                  <button
                    onClick={() => setShowTrainingLog(!showTrainingLog)}
                    className="flex items-center gap-1.5 text-xs text-red-300 hover:text-red-200 transition-colors"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/>
                      <polyline points="14 2 14 8 20 8"/>
                      <line x1="16" y1="13" x2="8" y2="13"/>
                      <line x1="16" y1="17" x2="8" y2="17"/>
                      <line x1="10" y1="9" x2="8" y2="9"/>
                    </svg>
                    {showTrainingLog ? 'Hide Training Log' : 'View Training Log'} ({failLogs.length} entries)
                  </button>
                  {showTrainingLog && (
                    <div className="mt-2 bg-rt-surface rounded-lg border border-red-500/20 p-3 font-mono text-xs text-rt-text max-h-48 overflow-y-auto">
                      {failLogs.map((log: string, i: number) => (
                        <div key={i} className="leading-5 whitespace-pre-wrap">{log}</div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        }

        if (stats && isAnyCompleted) {
          const breakdown = (stats.classification_breakdown || {}) as Record<string, number>
          // Pipeline stores chunk_map_entries (total chunks), files_kept, files_extracted
          const knowledgeTrained = (stats.chunk_map_entries || stats.chunks_created || stats.chunks_indexed || 0) as number
          const filesKept = (stats.files_kept || stats.files_extracted || stats.files_discovered || 0) as number
          
          return (
            <div className="mb-3 p-3 bg-rt-surface rounded-lg border border-rt-border">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-xs font-medium flex items-center gap-1.5">
                  <Database className="w-3.5 h-3.5 text-rt-primary" />
                  Training Results
                </h3>
                {statsGroup?.last_trained && (
                  <span className="text-xs text-rt-text-muted">
                    {new Date(statsGroup.last_trained).toLocaleString()}
                  </span>
                )}
              </div>
              
              {/* Single training metric */}
              <div className="flex items-center gap-2">
                <div className="flex-1 bg-gradient-to-r from-rt-primary/10 to-purple-500/10 rounded-lg p-2 border border-rt-primary/20">
                  <div className="text-[10px] text-rt-text-muted mb-0.5">KB Items Trained</div>
                  <div className="text-lg font-bold text-rt-primary">
                    {knowledgeTrained.toLocaleString()}
                  </div>
                </div>
              </div>
              
              {Object.keys(breakdown).length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {Object.entries(breakdown).map(([type, count]) => (
                    <span key={type} className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-rt-bg text-xs">
                      {type === 'code' && <FileCode className="w-3 h-3 text-blue-400" />}
                      {type === 'doc' && <FileText className="w-3 h-3 text-green-400" />}
                      {type === 'diagram_image' && <Image className="w-3 h-3 text-purple-400" />}
                      {type === 'ticket_export' && <TicketIcon className="w-3 h-3 text-orange-400" />}
                      {!['code', 'doc', 'diagram_image', 'ticket_export'].includes(type) && <Folder className="w-3 h-3 text-rt-text-muted" />}
                      <span className="text-rt-text-muted">{type}:</span>
                      <span className="font-medium">{(count as number).toLocaleString()}</span>
                    </span>
                  ))}
                </div>
              )}

              {/* View Training Tree + Log buttons */}
              <div className="mt-3 pt-3 border-t border-rt-border flex flex-wrap gap-4">
                {/* Training Tree button */}
                <button
                  onClick={() => setShowTrainingTree(!showTrainingTree)}
                  className="flex items-center gap-1.5 text-xs text-rt-text-muted hover:text-rt-text transition-colors"
                >
                  <FolderTree className="w-3.5 h-3.5" />
                  {showTrainingTree ? 'Hide Training Map' : 'View Training Map'}
                </button>

                {/* Training Log button */}
                {stats.logs && Array.isArray(stats.logs) && (stats.logs as string[]).length > 0 && (
                  <button
                    onClick={() => setShowTrainingLog(!showTrainingLog)}
                    className="flex items-center gap-1.5 text-xs text-rt-text-muted hover:text-rt-text transition-colors"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/>
                      <polyline points="14 2 14 8 20 8"/>
                      <line x1="16" y1="13" x2="8" y2="13"/>
                      <line x1="16" y1="17" x2="8" y2="17"/>
                      <line x1="10" y1="9" x2="8" y2="9"/>
                    </svg>
                    {showTrainingLog ? 'Hide Training Log' : 'View Training Log'} ({(stats.logs as string[]).length} entries)
                  </button>
                )}
              </div>

              {/* Training Tree View */}
              {showTrainingTree && (
                <div className="mt-2">
                  <TrainingTreeView productId={product.product_id} />
                </div>
              )}

              {/* Training Log */}
              {showTrainingLog && stats.logs && Array.isArray(stats.logs) && (
                <div className="mt-2 bg-rt-surface rounded-lg border border-rt-border p-3 font-mono text-xs text-rt-text max-h-60 overflow-y-auto">
                  {(stats.logs as string[]).map((log: string, i: number) => (
                    <div key={i} className="leading-5 whitespace-pre-wrap">
                      {log}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        }
        
        return null
      })()}
      </div>

      {/* Remote Agents */}
      <div className="mb-3 max-h-[120px] overflow-y-auto flex-shrink-0">
        <ProductAgents productId={product.product_id} />
      </div>

      {/* Training Data — fixed height for consistent card layout */}
      <div className="space-y-2 mb-3 flex-shrink-0">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-medium text-rt-text-muted">Training Data</h3>
          <button
            onClick={() => setShowAddGroupModal(true)}
            className="text-sm text-rt-primary hover:underline flex items-center gap-1"
          >
            <Plus className="w-3.5 h-3.5" />
            Add Training Data
          </button>
        </div>
        <div className="max-h-[120px] overflow-y-auto rounded-lg border border-rt-border bg-rt-bg-light/50">
        {product.folder_groups.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center py-4 bg-rt-surface/50 rounded-lg border border-dashed border-rt-border">
            <p className="text-xs text-rt-text-muted mb-2">No training data added yet</p>
            <button
              onClick={() => setShowAddGroupModal(true)}
              className="btn-secondary text-sm"
            >
              Add Training Data
            </button>
          </div>
        ) : (
          <div className="p-2 space-y-0.5">
              {product.folder_groups.map((group) => {
                const pod = pods.find(p => p.pod_id === group.pod_id)
                return (
                  <div key={group.group_id} className="flex items-center gap-2 py-1 text-xs group/row">
                    <Folder className="w-3 h-3 text-rt-primary flex-shrink-0" />
                    <span className="font-medium truncate">{group.group_name}</span>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] flex-shrink-0 ${groupTypeColors[group.group_type] || groupTypeColors.other}`}>
                      {group.group_type === 'other' ? 'auto-detect' : group.group_type}
                    </span>
                    <span className={`text-[10px] flex-shrink-0 ${
                      group.training_status === 'completed' ? 'text-green-400' :
                      group.training_status === 'failed' ? 'text-red-400' :
                      group.training_status === 'training' ? 'text-blue-400' :
                      'text-rt-text-muted'
                    }`}>{group.training_status}</span>
                    <button
                      onClick={() => {
                        if (confirm(`Delete "${group.group_name}"?`)) {
                          productApi.removeFolderGroup(product.product_id, group.group_id)
                            .then(() => {
                              queryClient.invalidateQueries({ queryKey: ['products'] })
                              toast.success('Removed')
                            })
                            .catch(() => toast.error('Failed'))
                        }
                      }}
                      className="p-0.5 rounded text-rt-text-muted hover:text-red-400 opacity-0 group-hover/row:opacity-100 transition-all ml-auto"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </div>
                )
              })}
          </div>
        )}
        </div>
      </div>

      {/* AI Email section */}
      <div className="mt-3 border-t border-rt-border/30 pt-3 flex-shrink-0">
        <button
          type="button"
          onClick={() => setEmailExpanded(!emailExpanded)}
          className="w-full flex items-center justify-between gap-2 text-left"
        >
          <div className="flex items-center gap-2">
            {emailExpanded ? (
              <ChevronDown className="w-3.5 h-3.5 text-rt-text-muted" />
            ) : (
              <ChevronRight className="w-3.5 h-3.5 text-rt-text-muted" />
            )}
            <Mail className="w-4 h-4 text-blue-400" />
            <span className="text-sm font-medium">AI Email</span>
          </div>
          <span className="text-xs text-rt-text-muted">
            {inbox ? (inbox.is_active ? 'Active' : 'Inactive') : 'Off'}
          </span>
        </button>

        {emailExpanded && (
          <div className="mt-3 space-y-3">
            {emailConnections.length === 0 ? (
              <div className="text-xs text-rt-text-muted bg-rt-surface/50 rounded-lg p-3 border border-dashed border-rt-border">
                Connect your email provider in <span className="font-semibold text-rt-primary">Settings</span> first to enable AI Email.
              </div>
            ) : !inbox ? (
              <div className="space-y-2">
                <p className="text-xs text-rt-text-muted">Enable AI Email for this product:</p>
                {emailConnections.map((conn: any) => (
                  <button
                    key={conn.connection_id}
                    onClick={() => createInboxMutation.mutate(conn.connection_id)}
                    disabled={createInboxMutation.isPending}
                    className="w-full flex items-center gap-2 p-2 rounded-lg border border-rt-border hover:border-blue-500/50 hover:bg-blue-500/5 transition-all text-left text-xs"
                  >
                    <Mail className="w-3.5 h-3.5 text-blue-400 flex-shrink-0" />
                    <span className="font-medium">{conn.tenant_name}</span>
                    <span className="text-rt-text-muted capitalize">({conn.provider})</span>
                    {createInboxMutation.isPending && (
                      <Loader2 className="w-3 h-3 animate-spin ml-auto" />
                    )}
                  </button>
                ))}
              </div>
            ) : (
              <div className="space-y-3">
                {/* Email address */}
                <div className="flex items-center gap-2 p-2 rounded-lg bg-rt-surface/50 border border-rt-border">
                  <Mail className="w-3.5 h-3.5 text-blue-400 flex-shrink-0" />
                  <span className="text-xs font-mono truncate flex-1">{inbox.email_address}</span>
                  <button
                    onClick={() => {
                      navigator.clipboard.writeText(inbox.email_address)
                      toast.success('Copied!')
                    }}
                    className="text-rt-text-muted hover:text-rt-primary transition-colors"
                  >
                    <Copy className="w-3.5 h-3.5" />
                  </button>
                </div>

                {/* Action mode checkboxes */}
                <div className="space-y-2">
                  <p className="text-xs font-medium text-rt-text-muted">Action Modes</p>
                  {[
                    {
                      key: 'auto_reply',
                      label: 'Auto-Reply',
                      description: '>=90% confidence auto-sends; below that escalates to Slack/Teams',
                    },
                    {
                      key: 'draft_and_review',
                      label: 'Draft & Review',
                      description: 'AI drafts a reply for human approval before sending',
                    },
                    {
                      key: 'classify_and_route',
                      label: 'Classify & Route',
                      description: 'AI categorizes the email and routes to the right channel',
                    },
                    {
                      key: 'summarize_and_notify',
                      label: 'Summarize & Notify',
                      description: 'Sends summary + suggested response to Slack/Teams',
                    },
                  ].map((mode) => {
                    const config = inbox.action_config || {}
                    const checked = !!config[mode.key]
                    return (
                      <label
                        key={mode.key}
                        className="flex items-start gap-2 p-2 rounded-lg border border-rt-border hover:border-rt-text-muted/30 cursor-pointer transition-all"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => {
                            const updated = { ...config, [mode.key]: !checked }
                            updateInboxMutation.mutate(updated)
                          }}
                          className="mt-0.5 accent-rt-primary"
                        />
                        <div>
                          <span className="text-xs font-medium">{mode.label}</span>
                          <p className="text-[10px] text-rt-text-muted leading-tight">{mode.description}</p>
                        </div>
                      </label>
                    )
                  })}
                </div>

                {/* Escalation channel */}
                {(inbox.action_config?.auto_reply || inbox.action_config?.summarize_and_notify) && (
                  <div>
                    <label className="text-xs font-medium text-rt-text-muted block mb-1">Escalation Channel</label>
                    <div className="flex gap-2">
                      {['slack', 'teams'].map((ch) => (
                        <button
                          key={ch}
                          type="button"
                          onClick={() => {
                            updateInboxMutation.mutate({
                              ...inbox.action_config,
                              escalation_channel: ch,
                            })
                          }}
                          className={`px-3 py-1 rounded text-xs font-medium transition-all ${
                            inbox.action_config?.escalation_channel === ch
                              ? 'bg-blue-500/10 text-blue-400 border border-blue-500'
                              : 'border border-rt-border hover:border-rt-text-muted'
                          }`}
                        >
                          {ch === 'slack' ? 'Slack' : 'Teams'}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {/* View Drafts */}
                {inbox.action_config?.draft_and_review && (
                  <button
                    onClick={() => setShowDraftsModal(true)}
                    className="text-xs text-blue-400 hover:text-blue-300 transition-colors flex items-center gap-1"
                  >
                    <Mail className="w-3 h-3" />
                    View Email Drafts
                  </button>
                )}

                {/* View Unread Mails */}
                {inbox && (
                  <button
                    onClick={() => setShowUnreadModal(true)}
                    className="text-xs text-blue-400 hover:text-blue-300 transition-colors flex items-center gap-1"
                  >
                    <Mail className="w-3 h-3" />
                    View Unread Mails
                  </button>
                )}

                {/* Remove */}
                <button
                  onClick={() => {
                    if (confirm('Remove AI Email from this product?')) {
                      deleteInboxMutation.mutate()
                    }
                  }}
                  className="text-xs text-rt-text-muted hover:text-rt-accent transition-colors flex items-center gap-1"
                >
                  <Trash2 className="w-3 h-3" />
                  Remove AI Email
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Email Drafts Modal */}
      <AnimatePresence>
        {showDraftsModal && (
          <EmailDraftsModal
            productId={product.product_id}
            onClose={() => setShowDraftsModal(false)}
          />
        )}
      </AnimatePresence>

      {/* Unread Mails Modal */}
      <AnimatePresence>
        {showUnreadModal && (
          <UnreadMailsModal
            productId={product.product_id}
            onClose={() => setShowUnreadModal(false)}
          />
        )}
      </AnimatePresence>

      {/* Add Group Modal */}
      <AnimatePresence>
        {showAddGroupModal && (
          <AddGroupModal
            productId={product.product_id}
            pods={pods}
            onClose={() => setShowAddGroupModal(false)}
          />
        )}
      </AnimatePresence>
    </div>
  )
}

function EmailDraftsModal({
  productId,
  onClose,
}: {
  productId: string
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editText, setEditText] = useState('')

  const { data: drafts = [], isLoading } = useQuery<any[]>({
    queryKey: ['email', 'drafts', productId],
    queryFn: () => emailApi.listDrafts(productId),
    refetchInterval: 10000,
  })

  const approveMutation = useMutation({
    mutationFn: ({ messageId, edited }: { messageId: string; edited?: string }) =>
      emailApi.approveDraft(productId, messageId, edited),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['email', 'drafts', productId] })
      toast.success('Draft approved and sent')
      setEditingId(null)
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.detail || 'Failed to approve')
    },
  })

  const rejectMutation = useMutation({
    mutationFn: (messageId: string) => emailApi.rejectDraft(productId, messageId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['email', 'drafts', productId] })
      toast.success('Draft rejected')
    },
    onError: () => {
      toast.error('Failed to reject')
    },
  })

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.95 }}
        animate={{ scale: 1 }}
        exit={{ scale: 0.95 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-rt-bg border border-rt-border rounded-xl shadow-2xl w-full max-w-2xl max-h-[80vh] flex flex-col"
      >
        <div className="flex items-center justify-between p-4 border-b border-rt-border">
          <div className="flex items-center gap-2">
            <Mail className="w-5 h-5 text-blue-400" />
            <h2 className="text-lg font-display font-semibold">Email Drafts</h2>
            <span className="text-xs text-rt-text-muted">({drafts.length} pending)</span>
          </div>
          <button onClick={onClose} className="text-rt-text-muted hover:text-rt-text">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {isLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 animate-spin text-rt-text-muted" />
            </div>
          ) : drafts.length === 0 ? (
            <div className="text-center py-8 text-rt-text-muted text-sm">
              No pending drafts. New email drafts will appear here for review.
            </div>
          ) : (
            drafts.map((draft: any) => (
              <div
                key={draft.message_id}
                className="border border-rt-border rounded-lg overflow-hidden"
              >
                <div className="p-3 bg-rt-surface/30 border-b border-rt-border/50">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-medium truncate">{draft.from_address}</span>
                    <span className="text-[10px] text-rt-text-muted">
                      {draft.received_at ? new Date(draft.received_at).toLocaleString() : ''}
                    </span>
                  </div>
                  <p className="text-sm font-medium">{draft.subject}</p>
                </div>

                <div className="p-3">
                  <p className="text-xs text-rt-text-muted mb-2 line-clamp-3">{draft.body_text}</p>

                  <div className="mt-2">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs font-medium text-rt-text-muted">AI Response</span>
                      {draft.confidence_score != null && (
                        <span className="text-[10px] text-rt-text-muted">
                          Confidence: {(draft.confidence_score * 100).toFixed(0)}%
                        </span>
                      )}
                    </div>

                    {editingId === draft.message_id ? (
                      <textarea
                        className="input text-xs w-full h-24 resize-none"
                        value={editText}
                        onChange={(e) => setEditText(e.target.value)}
                      />
                    ) : (
                      <div className="text-xs bg-rt-bg-light rounded p-2 border border-rt-border/50 max-h-24 overflow-y-auto whitespace-pre-wrap">
                        {draft.ai_response || 'No response generated'}
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-2 mt-3 justify-end">
                    {editingId === draft.message_id ? (
                      <>
                        <button
                          onClick={() => setEditingId(null)}
                          className="btn-secondary text-xs py-1 px-2"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={() =>
                            approveMutation.mutate({
                              messageId: draft.message_id,
                              edited: editText,
                            })
                          }
                          disabled={approveMutation.isPending}
                          className="btn-primary text-xs py-1 px-2 flex items-center gap-1"
                        >
                          {approveMutation.isPending ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <CheckCircle className="w-3 h-3" />
                          )}
                          Send Edited
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          onClick={() => rejectMutation.mutate(draft.message_id)}
                          disabled={rejectMutation.isPending}
                          className="text-xs text-rt-text-muted hover:text-rt-accent transition-colors px-2 py-1"
                        >
                          Reject
                        </button>
                        <button
                          onClick={() => {
                            setEditingId(draft.message_id)
                            setEditText(draft.ai_response || '')
                          }}
                          className="btn-secondary text-xs py-1 px-2"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() =>
                            approveMutation.mutate({ messageId: draft.message_id })
                          }
                          disabled={approveMutation.isPending}
                          className="btn-primary text-xs py-1 px-2 flex items-center gap-1"
                        >
                          {approveMutation.isPending ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <CheckCircle className="w-3 h-3" />
                          )}
                          Approve & Send
                        </button>
                      </>
                    )}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </motion.div>
    </motion.div>
  )
}

function UnreadMailsModal({
  productId,
  onClose,
}: {
  productId: string
  onClose: () => void
}) {
  const { data: unread = [], isLoading, error } = useQuery<any[]>({
    queryKey: ['email', 'unread', productId],
    queryFn: () => emailApi.listUnread(productId),
    retry: false,
  })

  const errorMsg = error
    ? (error as any)?.response?.data?.detail || (error as Error).message || 'Failed to load unread emails'
    : null

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.95 }}
        animate={{ scale: 1 }}
        exit={{ scale: 0.95 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-rt-bg border border-rt-border rounded-xl shadow-2xl w-full max-w-2xl max-h-[80vh] flex flex-col"
      >
        <div className="flex items-center justify-between p-4 border-b border-rt-border">
          <div className="flex items-center gap-2">
            <Mail className="w-5 h-5 text-blue-400" />
            <h2 className="text-lg font-display font-semibold">Unread Mails</h2>
            <span className="text-xs text-rt-text-muted">(most recent 5)</span>
          </div>
          <button onClick={onClose} className="text-rt-text-muted hover:text-rt-text">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {isLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 animate-spin text-rt-text-muted" />
            </div>
          ) : errorMsg ? (
            <div className="text-center py-8 space-y-2">
              <AlertTriangle className="w-6 h-6 text-amber-400 mx-auto" />
              <p className="text-sm text-rt-accent">{errorMsg}</p>
              <p className="text-xs text-rt-text-muted">
                If the token expired, go to Settings → Email Provider, remove the connection, and re-add it.
                Make sure to include <code className="bg-rt-surface px-1 rounded">access_type=offline</code> in the Zoho OAuth URL to get a refresh token.
              </p>
            </div>
          ) : unread.length === 0 ? (
            <div className="text-center py-8 text-rt-text-muted text-sm">
              No unread emails for this inbox.
            </div>
          ) : (
            unread.map((msg: any, idx: number) => (
              <div
                key={idx}
                className="border border-rt-border rounded-lg p-3 bg-rt-surface/30"
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-medium truncate">{msg.from_address}</span>
                  <span className="text-[10px] text-rt-text-muted">
                    {msg.received_at ? new Date(msg.received_at).toLocaleString() : ''}
                  </span>
                </div>
                <p className="text-sm font-medium mb-1">{msg.subject}</p>
                <p className="text-xs text-rt-text-muted line-clamp-3">{msg.body_text || '—'}</p>
              </div>
            ))
          )}
        </div>
      </motion.div>
    </motion.div>
  )
}

function CreateProductModal({
  pods,
  onClose,
}: {
  pods: Pod[]
  onClose: () => void
}) {
  const [productName, setProductName] = useState('')
  const [description, setDescription] = useState('')
  const [autoGenerateDescription, setAutoGenerateDescription] = useState(true)
  const queryClient = useQueryClient()
  
  const createMutation = useMutation({
    mutationFn: productApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['products'] })
      toast.success('Product created!')
      onClose()
    },
    onError: () => {
      toast.error('Failed to create product')
    },
  })
  
  const handleCreate = () => {
    if (!productName.trim()) {
      toast.error('Please enter a product name')
      return
    }
    
    createMutation.mutate({
      product_name: productName,
      description: description || undefined,
      auto_generate_description: autoGenerateDescription,
    })
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
        <h2 className="text-xl font-display font-semibold mb-6">Create Product</h2>
        
        <div className="space-y-4 mb-6">
          <div>
            <label className="label">Product Name</label>
            <input
              type="text"
              className="input"
              placeholder="e.g., My Application"
              value={productName}
              onChange={(e) => setProductName(e.target.value)}
            />
          </div>
          
          <div>
            <label className="label">Description (optional)</label>
            <textarea
              className="input"
              placeholder="Describe this product..."
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="auto-generate-desc"
              checked={autoGenerateDescription}
              onChange={(e) => setAutoGenerateDescription(e.target.checked)}
              className="w-4 h-4 rounded border-rt-border bg-rt-bg text-blue-600 focus:ring-2 focus:ring-blue-500"
            />
            <label htmlFor="auto-generate-desc" className="text-sm text-rt-text cursor-pointer">
              Auto-generate description using AI after first training
            </label>
          </div>
        </div>
        
        <div className="flex gap-3 justify-end">
          <button onClick={onClose} className="btn-secondary">
            Cancel
          </button>
          <button
            onClick={handleCreate}
            disabled={createMutation.isPending}
            className="btn-primary"
          >
            {createMutation.isPending ? 'Creating...' : 'Create Product'}
          </button>
        </div>
      </motion.div>
    </motion.div>
  )
}

interface TrainingEntry {
  podId: string
  path: string
  contentType: string
}

const CONTENT_TYPES = [
  { value: 'other', label: 'Auto-detect' },
  { value: 'code', label: 'Code' },
  { value: 'documentation', label: 'Documentation' },
  { value: 'tickets', label: 'Tickets / Issues' },
]

function AddGroupModal({
  productId,
  pods,
  onClose,
}: {
  productId: string
  pods: Pod[]
  onClose: () => void
}) {
  const localOption = { pod_id: '__local__', pod_name: 'Local' }
  const podOptions = [localOption, ...pods]
  const [curPod, setCurPod] = useState(podOptions[0]?.pod_id || '__local__')
  const [curType, setCurType] = useState('other')
  const [showBrowser, setShowBrowser] = useState(false)
  const [entries, setEntries] = useState<TrainingEntry[]>([])
  const [submitting, setSubmitting] = useState(false)
  const queryClient = useQueryClient()

  const removeEntry = (index: number) => {
    setEntries(prev => prev.filter((_, i) => i !== index))
  }

  const handleBrowseConfirm = (selectedPaths: string[]) => {
    const existing = new Set(entries.map(e => e.path))
    const newEntries = selectedPaths
      .filter(p => !existing.has(p))
      .map(p => ({ podId: curPod, path: p, contentType: curType }))
    setEntries(prev => [...prev, ...newEntries])
    setShowBrowser(false)
  }

  const handleSubmit = async () => {
    if (entries.length === 0) {
      toast.error('Add at least one entry')
      return
    }

    setSubmitting(true)
    try {
      // Group entries by (podId, contentType) → one folder group per combo
      const groups: Record<string, { podId: string; contentType: string; paths: string[] }> = {}
      for (const e of entries) {
        const key = `${e.podId}::${e.contentType}`
        if (!groups[key]) groups[key] = { podId: e.podId, contentType: e.contentType, paths: [] }
        groups[key].paths.push(e.path)
      }

      for (const g of Object.values(groups)) {
        const label = CONTENT_TYPES.find(t => t.value === g.contentType)?.label || g.contentType
        const shortName = g.paths.length === 1
          ? g.paths[0].split('/').slice(-1)[0] || 'data'
          : `${label} (${g.paths.length} folders)`

        await productApi.addFolderGroup(productId, {
          pod_id: g.podId,
          group_name: shortName,
          group_type: g.contentType,
          folder_paths: g.paths.map(p => ({ absolute_path: p, scan_recursive: true })),
        })
      }

      queryClient.invalidateQueries({ queryKey: ['products'] })
      toast.success(`Added ${entries.length} training ${entries.length === 1 ? 'path' : 'paths'}`)
      onClose()
    } catch (error: any) {
      toast.error(error?.response?.data?.detail || 'Failed to add training data')
    } finally {
      setSubmitting(false)
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
        className="bg-rt-bg-light border border-rt-border rounded-xl p-5 w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-display font-semibold">Add Training Data</h2>
          <button onClick={onClose} className="p-1 rounded hover:bg-rt-surface"><X className="w-4 h-4" /></button>
        </div>

        {/* Controls */}
        <div className="space-y-2 mb-3">
          <div className="flex items-center gap-2">
            <select
              value={curPod}
              onChange={(e) => setCurPod(e.target.value)}
              className="bg-rt-surface border border-rt-border rounded px-2 py-1.5 text-xs focus:outline-none focus:border-rt-primary flex-1 min-w-0"
            >
              {podOptions.map((pod) => (
                <option key={pod.pod_id} value={pod.pod_id}>{pod.pod_name}</option>
              ))}
            </select>
            <select
              value={curType}
              onChange={(e) => setCurType(e.target.value)}
              className="bg-rt-surface border border-rt-border rounded px-2 py-1.5 text-xs focus:outline-none focus:border-rt-primary w-28"
            >
              {CONTENT_TYPES.map(t => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>
          <button
            type="button"
            onClick={() => setShowBrowser(true)}
            className="w-full py-1.5 rounded bg-rt-primary text-white text-xs hover:bg-rt-primary-dark transition-colors flex items-center justify-center gap-1.5"
          >
            <FolderOpen className="w-3.5 h-3.5" />
            Browse Folders
          </button>
        </div>

        {/* Entries */}
        {entries.length > 0 ? (
          <div className="border border-rt-border rounded overflow-hidden mb-3 max-h-40 overflow-y-auto">
            {entries.map((entry, index) => {
              const typeLabel = CONTENT_TYPES.find(t => t.value === entry.contentType)?.label || entry.contentType
              return (
                <div key={index} className="flex items-center gap-1.5 px-2 py-1 border-b border-rt-border last:border-b-0 hover:bg-rt-surface/30">
                  <Folder className="w-3 h-3 text-rt-primary flex-shrink-0" />
                  <span className="flex-1 font-mono truncate text-[11px]" title={entry.path}>{entry.path.split('/').slice(-2).join('/')}</span>
                  <span className="text-[10px] text-rt-text-muted flex-shrink-0">{typeLabel}</span>
                  <button onClick={() => removeEntry(index)} className="p-0.5 text-rt-text-muted hover:text-red-400"><X className="w-2.5 h-2.5" /></button>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="text-center py-3 rounded border border-dashed border-rt-border mb-3 text-[11px] text-rt-text-muted">
            Select folders to add
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          <button onClick={onClose} className="btn-secondary text-xs py-1 px-2.5">Cancel</button>
          <button
            onClick={handleSubmit}
            disabled={submitting || entries.length === 0}
            className="btn-primary text-xs py-1 px-2.5"
          >
            {submitting ? 'Saving…' : entries.length > 0 ? `Save ${entries.length}` : 'Save'}
          </button>
        </div>

        <AnimatePresence>
          {showBrowser && (
            <FileBrowserModal
              podId={curPod}
              selectedPaths={entries.map(e => e.path)}
              onConfirm={handleBrowseConfirm}
              onClose={() => setShowBrowser(false)}
            />
          )}
        </AnimatePresence>
      </motion.div>
    </motion.div>
  )
}

// ---------------------------------------------------------------------------
// Product Agents – download & manage remote agents for a product
// ---------------------------------------------------------------------------

function ProductAgents({ productId }: { productId: string }) {
  const [showModal, setShowModal] = useState(false)
  const [agentName, setAgentName] = useState('')
  const [selectedOs, setSelectedOs] = useState<'linux' | 'windows'>('linux')
  const [generating, setGenerating] = useState(false)
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null)
  const [wsHost, setWsHost] = useState<string>('')
  const [archiveName, setArchiveName] = useState<string>('')
  const queryClient = useQueryClient()

  const { data: podData } = useQuery({
    queryKey: ['product-pods', productId],
    queryFn: () => productApi.listPods(productId),
    refetchInterval: 30000,
  })

  const pods = podData?.pods || []
  const onlineCount = podData?.online || 0

  const handleOpenModal = () => {
    setAgentName('')
    setSelectedOs('linux')
    setDownloadUrl(null)
    setShowModal(true)
  }

  const handleGenerate = async () => {
    if (!agentName.trim()) {
      toast.error('Please enter an agent name')
      return
    }
    setGenerating(true)
    setDownloadUrl(null)
    try {
      const result = await productApi.generatePod(productId, selectedOs, agentName.trim())
      setDownloadUrl(result.download_url)
      setWsHost(result.ws_host || '')
      setArchiveName(result.archive_name || '')
      queryClient.invalidateQueries({ queryKey: ['product-pods', productId] })
      toast.success('Agent package generated')
    } catch (error: any) {
      toast.error(error?.response?.data?.detail || 'Failed to generate agent')
    } finally {
      setGenerating(false)
    }
  }

  const handleDeletePod = async (podId: string, podName: string) => {
    if (!confirm(`Delete agent "${podName}"?`)) return
    try {
      await podApi.delete(podId)
      queryClient.invalidateQueries({ queryKey: ['product-pods', productId] })
      toast.success('Agent deleted')
    } catch {
      toast.error('Failed to delete agent')
    }
  }

  return (
    <div className="mb-4">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium text-rt-text-muted flex items-center gap-2">
          <Server className="w-3.5 h-3.5" />
          Remote Agents
          {onlineCount > 0 && (
            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-green-500/10 text-green-400 text-xs">
              <span className="w-1.5 h-1.5 rounded-full bg-green-400" />
              {onlineCount} online
            </span>
          )}
        </h3>
        <button
          onClick={handleOpenModal}
          className="text-sm text-rt-primary hover:underline flex items-center gap-1"
        >
          <Download className="w-3.5 h-3.5" />
          Download Agent
        </button>
      </div>

      {/* Connected agents with delete */}
      {pods.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {pods.map((pod: any) => (
            <div
              key={pod.pod_id}
              className="inline-flex items-center gap-2 px-2.5 py-1 rounded-lg bg-rt-surface border border-rt-border text-xs group"
            >
              <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
                pod.status === 'online' ? 'bg-green-400' :
                pod.status === 'pending' ? 'bg-yellow-400' :
                'bg-gray-500'
              }`} />
              <span className="font-medium">{pod.pod_name}</span>
              <span className="text-rt-text-muted">{pod.os_type || '?'}</span>
              <span className={pod.status === 'online' ? 'text-green-400' : 'text-rt-text-muted'}>
                {pod.status}
              </span>
              <button
                onClick={() => handleDeletePod(pod.pod_id, pod.pod_name)}
                className="p-0.5 rounded text-rt-text-muted hover:text-red-400 opacity-0 group-hover:opacity-100 transition-all"
                title="Delete agent"
              >
                <Trash2 className="w-3 h-3" />
              </button>
            </div>
          ))}
        </div>
      )}

      {pods.length === 0 && (
        <p className="text-xs text-rt-text-muted italic">
          No agents yet. Download and install an agent on a remote machine to connect its files.
        </p>
      )}

      {/* Download Agent Modal */}
      <AnimatePresence>
        {showModal && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4"
            onClick={() => setShowModal(false)}
          >
            <motion.div
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              className="bg-rt-bg-light border border-rt-border rounded-xl p-6 w-full max-w-lg"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between mb-5">
                <h2 className="text-lg font-display font-semibold flex items-center gap-2">
                  <Download className="w-5 h-5 text-rt-primary" />
                  Download Remote Agent
                </h2>
                <button onClick={() => setShowModal(false)} className="p-1 rounded hover:bg-rt-surface">
                  <X className="w-5 h-5" />
                </button>
              </div>

              {/* Step 1: Name + Platform (before generation) */}
              {!downloadUrl && (
                <div className="space-y-4">
                  <div>
                    <label className="label">Agent Name</label>
                    <input
                      type="text"
                      className="input"
                      placeholder="e.g., office-server, prod-linux-01"
                      value={agentName}
                      onChange={(e) => setAgentName(e.target.value)}
                    />
                  </div>

                  <div>
                    <label className="label">Platform</label>
                    <div className="grid grid-cols-2 gap-2">
                      <button
                        type="button"
                        onClick={() => setSelectedOs('linux')}
                        className={`p-3 rounded-lg border text-sm transition-all flex items-center gap-2 ${
                          selectedOs === 'linux'
                            ? 'border-rt-primary bg-rt-primary/10 text-rt-primary'
                            : 'border-rt-border hover:border-rt-text-muted'
                        }`}
                      >
                        <Server className="w-4 h-4" />
                        Unix
                      </button>
                      <button
                        type="button"
                        onClick={() => setSelectedOs('windows')}
                        className={`p-3 rounded-lg border text-sm transition-all flex items-center gap-2 ${
                          selectedOs === 'windows'
                            ? 'border-rt-primary bg-rt-primary/10 text-rt-primary'
                            : 'border-rt-border hover:border-rt-text-muted'
                        }`}
                      >
                        <Monitor className="w-4 h-4" />
                        Windows
                      </button>
                    </div>
                  </div>

                  <div className="flex gap-3 justify-end pt-2">
                    <button onClick={() => setShowModal(false)} className="btn-secondary text-sm">
                      Cancel
                    </button>
                    <button
                      onClick={handleGenerate}
                      disabled={generating || !agentName.trim()}
                      className="btn-primary text-sm flex items-center gap-2"
                    >
                      {generating ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : (
                        <Download className="w-4 h-4" />
                      )}
                      {generating ? 'Generating…' : 'Generate Agent'}
                    </button>
                  </div>
                </div>
              )}

              {/* Step 2: Instructions + Download (after generation) */}
              {downloadUrl && (
                <div className="space-y-4">
                  <div className="p-3 bg-green-500/5 border border-green-500/20 rounded-lg flex items-center gap-2 text-sm text-green-400">
                    <Check className="w-4 h-4 flex-shrink-0" />
                    Agent "{agentName}" generated for {selectedOs === 'windows' ? 'Windows' : 'Unix'}
                  </div>

                  <div>
                    <p className="text-sm text-rt-text-muted mb-2">Install on the target machine:</p>
                    {selectedOs === 'windows' ? (
                      <div className="bg-rt-surface rounded-lg border border-rt-border p-4 font-mono text-xs text-rt-text space-y-1.5">
                        <p className="text-rt-success"># Extract and run</p>
                        <p>tar -xf {archiveName || 'retrace-*.zip'}</p>
                        <p>.\retrace-agent.exe --config config.yaml</p>
                      </div>
                    ) : (
                      <div className="bg-rt-surface rounded-lg border border-rt-border p-4 font-mono text-xs text-rt-text space-y-1.5">
                        <p className="text-rt-success"># Extract, make executable, and run</p>
                        <p>tar -xzf {archiveName || 'retrace-*.tar.gz'}</p>
                        <p>chmod +x retrace-agent</p>
                        <p>./retrace-agent --config config.yaml</p>
                        <p></p>
                        <p className="text-rt-success"># Or run in background</p>
                        <p>nohup ./retrace-agent --config config.yaml &amp;</p>
                      </div>
                    )}
                    {wsHost && wsHost !== 'localhost' && (
                      <p className="text-xs text-rt-text-muted mt-2">
                        Agent will connect to <span className="font-mono text-rt-primary">{wsHost}</span>
                      </p>
                    )}
                  </div>

                  <div className="flex gap-3 justify-end pt-2">
                    <button onClick={() => setShowModal(false)} className="btn-secondary text-sm">
                      Close
                    </button>
                    <a
                      href={downloadUrl}
                      download
                      className="btn-primary text-sm flex items-center gap-2"
                    >
                      <Download className="w-4 h-4" />
                      Download Package
                    </a>
                  </div>
                </div>
              )}
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Training Tree View – hierarchical visualization of indexed training data
// ---------------------------------------------------------------------------

interface TreeFile {
  path: string
  name: string
  chunks: number
}

interface TreeSubCategory {
  name: string
  chunks: number
  files: TreeFile[]
  file_count: number
}

interface TreeType {
  type: string
  chunks: number
  file_count: number
  sub_categories: TreeSubCategory[]
}

interface TrainingTree {
  product_id: string
  product_name: string
  total_chunks: number
  total_files: number
  tree: TreeType[]
}

const TYPE_CONFIG: Record<string, { icon: typeof FileCode; color: string; label: string }> = {
  code: { icon: FileCode, color: 'text-blue-400', label: 'Code' },
  doc: { icon: FileText, color: 'text-green-400', label: 'Documentation' },
  ticket_export: { icon: TicketIcon, color: 'text-orange-400', label: 'Tickets / Issues' },
  diagram_image: { icon: Image, color: 'text-purple-400', label: 'Diagrams' },
  doc_with_diagrams: { icon: Image, color: 'text-indigo-400', label: 'Docs with Diagrams' },
  summary: { icon: Brain, color: 'text-yellow-400', label: 'Summaries (auto-generated)' },
  other: { icon: Folder, color: 'text-rt-text-muted', label: 'Other' },
}

function TrainingTreeView({ productId }: { productId: string }) {
  const { data, isLoading, error } = useQuery<TrainingTree>({
    queryKey: ['training-tree', productId],
    queryFn: () => productApi.getTrainingTree(productId),
  })
  const [expandedTypes, setExpandedTypes] = useState<Set<string>>(new Set())
  const [expandedSubCats, setExpandedSubCats] = useState<Set<string>>(new Set())

  const toggleType = (type: string) => {
    const next = new Set(expandedTypes)
    if (next.has(type)) next.delete(type)
    else next.add(type)
    setExpandedTypes(next)
  }

  const toggleSubCat = (key: string) => {
    const next = new Set(expandedSubCats)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    setExpandedSubCats(next)
  }

  if (isLoading) {
    return (
      <div className="p-4 bg-rt-surface rounded-lg border border-rt-border text-center text-sm text-rt-text-muted">
        Loading training map…
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-4 bg-rt-surface rounded-lg border border-rt-border text-center text-sm text-rt-text-muted">
        Error loading training map: {error instanceof Error ? error.message : 'Unknown error'}
      </div>
    )
  }

  if (!data) {
    return (
      <div className="p-4 bg-rt-surface rounded-lg border border-rt-border text-center text-sm text-rt-text-muted">
        Loading training map…
      </div>
    )
  }

  if (data.tree.length === 0) {
    return (
      <div className="p-4 bg-rt-surface rounded-lg border border-rt-border text-center text-sm text-rt-text-muted">
        No training data found. Train this product first.
      </div>
    )
  }

  // Calculate total knowledge coverage
  const totalKnowledgeItems = data.total_chunks
  const totalDocuments = data.total_files
  const knowledgeCoverage = totalKnowledgeItems > 0 ? Math.round((totalKnowledgeItems / Math.max(totalDocuments * 10, 1)) * 100) : 0

  return (
    <div className="bg-rt-surface rounded-lg border border-rt-border p-4 text-rt-text">
      {/* Header with brain visualization */}
      <div className="mb-4 pb-4 border-b border-rt-border">
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2">
            <div className="relative">
              <Brain className="w-6 h-6 text-rt-primary" />
              <div className="absolute -top-1 -right-1 w-3 h-3 bg-rt-primary/20 rounded-full animate-pulse" />
            </div>
            <div>
              <h3 className="text-sm font-semibold">{data.product_name}</h3>
              <p className="text-xs text-rt-text-muted">Knowledge Base Overview</p>
            </div>
          </div>
        </div>
        
        {/* Single training metric */}
        <div className="mt-3">
          <div className="bg-gradient-to-r from-rt-primary/10 to-purple-500/10 rounded-lg p-2 border border-rt-primary/20">
            <div className="text-[10px] text-rt-text-muted mb-0.5">KB Items Trained</div>
            <div className="text-xl font-bold text-rt-primary">{totalKnowledgeItems.toLocaleString()}</div>
          </div>
        </div>
      </div>

      {/* Knowledge categories - brain structure */}
      <div className="space-y-2 max-h-[450px] overflow-y-auto">
        {data.tree.map((typeNode) => {
          const config = TYPE_CONFIG[typeNode.type] || TYPE_CONFIG.other
          const TypeIcon = config.icon
          const isExpanded = expandedTypes.has(typeNode.type)
          const knowledgePercentage = totalKnowledgeItems > 0 ? Math.round((typeNode.chunks / totalKnowledgeItems) * 100) : 0

          return (
            <div key={typeNode.type} className="bg-rt-bg/30 rounded-lg border border-rt-border/50 overflow-hidden">
              {/* Category header */}
              <button
                onClick={() => toggleType(typeNode.type)}
                className="w-full flex items-center gap-3 py-2.5 px-3 hover:bg-rt-bg/50 transition-colors text-left"
              >
                <div className={`p-1.5 rounded-lg ${
                  config.color === 'text-blue-400' ? 'bg-blue-400/10' :
                  config.color === 'text-green-400' ? 'bg-green-400/10' :
                  config.color === 'text-orange-400' ? 'bg-orange-400/10' :
                  config.color === 'text-purple-400' ? 'bg-purple-400/10' :
                  config.color === 'text-indigo-400' ? 'bg-indigo-400/10' :
                  config.color === 'text-yellow-400' ? 'bg-yellow-400/10' :
                  'bg-rt-text-muted/10'
                }`}>
                  <TypeIcon className={`w-4 h-4 ${config.color} flex-shrink-0`} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-rt-text">{config.label}</div>
                  <div className="text-xs text-rt-text-muted mt-0.5">
                    {typeNode.file_count} {typeNode.file_count === 1 ? 'document' : 'documents'} · {typeNode.chunks.toLocaleString()} knowledge items
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <div className="w-16 h-1.5 bg-rt-border rounded-full overflow-hidden">
                    <div 
                      className={`h-full transition-all ${
                        config.color === 'text-blue-400' ? 'bg-blue-400' :
                        config.color === 'text-green-400' ? 'bg-green-400' :
                        config.color === 'text-orange-400' ? 'bg-orange-400' :
                        config.color === 'text-purple-400' ? 'bg-purple-400' :
                        config.color === 'text-indigo-400' ? 'bg-indigo-400' :
                        config.color === 'text-yellow-400' ? 'bg-yellow-400' :
                        'bg-rt-text-muted'
                      }`}
                      style={{ width: `${knowledgePercentage}%` }}
                    />
                  </div>
                  <span className="text-xs text-rt-text-muted w-10 text-right">{knowledgePercentage}%</span>
                  {isExpanded ? <ChevronDown className="w-4 h-4 text-rt-text-muted" /> : <ChevronRight className="w-4 h-4 text-rt-text-muted" />}
                </div>
              </button>

              {/* Sub-categories - expanded view */}
              {isExpanded && typeNode.sub_categories.length > 0 && (
                <div className="border-t border-rt-border/50 bg-rt-bg/20">
                  {typeNode.sub_categories.map((subCat) => {
                    const subKey = `${typeNode.type}:${subCat.name}`
                    const isSubExpanded = expandedSubCats.has(subKey)

                    return (
                      <div key={subKey} className="border-b border-rt-border/30 last:border-b-0">
                        <button
                          onClick={() => toggleSubCat(subKey)}
                          className="w-full flex items-center gap-2 py-2 px-4 hover:bg-rt-bg/30 transition-colors text-left"
                        >
                          <Layers className="w-3.5 h-3.5 text-rt-text-muted/60 flex-shrink-0" />
                          <span className="text-xs text-rt-text flex-1">{subCat.name}</span>
                          <span className="text-xs text-rt-text-muted">
                            {subCat.file_count} docs · {subCat.chunks.toLocaleString()} items
                          </span>
                          {isSubExpanded ? <ChevronDown className="w-3 h-3 text-rt-text-muted ml-2" /> : <ChevronRight className="w-3 h-3 text-rt-text-muted ml-2" />}
                        </button>

                        {/* Individual documents */}
                        {isSubExpanded && subCat.files.length > 0 && (
                          <div className="bg-rt-bg/10 pl-4 pr-2 py-1.5 space-y-1">
                            {subCat.files.map((file) => (
                              <div
                                key={file.path}
                                className="flex items-center gap-2 py-1 px-2 rounded hover:bg-rt-bg/20 transition-colors group"
                                title={file.path}
                              >
                                <File className="w-3 h-3 text-rt-text-muted/60 flex-shrink-0" />
                                <span className="text-xs text-rt-text-muted flex-1 truncate">
                                  {file.name}
                                </span>
                                <span className="text-xs text-rt-text-muted/80 font-medium">
                                  {file.chunks} items
                                </span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}


function FileBrowserModal({
  podId,
  selectedPaths,
  onConfirm,
  onClose,
}: {
  podId: string
  selectedPaths: string[]
  onConfirm: (paths: string[]) => void
  onClose: () => void
}) {
  const isLocal = podId === '__local__'
  const initialPath = isLocal ? '/' : '.'
  const [currentPath, setCurrentPath] = useState(initialPath)
  const [selected, setSelected] = useState<Set<string>>(new Set(selectedPaths))
  
  const { data: directory, isLoading } = useQuery({
    queryKey: isLocal ? ['local-browse', currentPath] : ['pod-browse', podId, currentPath],
    queryFn: () =>
      isLocal
        ? localFilesApi.browse(currentPath)
        : podApi.browse(podId, currentPath, false),
    enabled: !!podId,
  })
  
  const isRoot = isLocal ? currentPath === '/' : (currentPath === '.' || currentPath === '')
  const pathParts = isLocal
    ? currentPath.split('/').filter(Boolean)
    : (currentPath === '.' ? [] : currentPath.split('/').filter(Boolean))
  
  const togglePath = (path: string) => {
    const newSelected = new Set(selected)
    if (newSelected.has(path)) {
      newSelected.delete(path)
    } else {
      newSelected.add(path)
    }
    setSelected(newSelected)
  }
  
  const handleConfirm = () => {
    onConfirm(Array.from(selected))
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
        className="bg-rt-bg-light border border-rt-border rounded-xl p-6 w-full max-w-3xl h-[600px] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-xl font-display font-semibold mb-4">Browse Folders</h2>
        
        <div className="flex items-center gap-1 text-sm mb-4 overflow-x-auto bg-rt-surface rounded-lg px-3 py-2">
          <button
            onClick={() => setCurrentPath(initialPath)}
            className={`px-2 py-1 rounded hover:bg-rt-border transition-colors ${
              isRoot ? 'text-rt-primary' : ''
            }`}
          >
            {isLocal ? 'Root' : 'Agent Root'}
          </button>
          {pathParts.map((part, i) => (
            <div key={i} className="flex items-center">
              <ChevronRight className="w-4 h-4 text-rt-text-muted" />
              <button
                onClick={() => {
                  const sub = pathParts.slice(0, i + 1).join('/')
                  setCurrentPath(isLocal ? '/' + sub : sub)
                }}
                className={`px-2 py-1 rounded hover:bg-rt-border transition-colors ${
                  i === pathParts.length - 1 ? 'text-rt-primary' : ''
                }`}
              >
                {part}
              </button>
            </div>
          ))}
        </div>
        
        <div className="flex-1 border border-rt-border rounded-lg overflow-hidden flex flex-col">
          {isLoading ? (
            <div className="flex-1 flex items-center justify-center text-rt-text-muted">
              Loading...
            </div>
          ) : (
            <div className="flex-1 overflow-y-auto divide-y divide-rt-border">
              {!isRoot && (
                <button
                  onClick={() => {
                    if (isLocal) {
                      const parent = '/' + pathParts.slice(0, -1).join('/')
                      setCurrentPath(parent || '/')
                    } else {
                      const parent = pathParts.slice(0, -1).join('/')
                      setCurrentPath(parent || '.')
                    }
                  }}
                  className="w-full flex items-center gap-3 p-3 hover:bg-rt-surface transition-colors text-left"
                >
                  <FolderOpen className="w-5 h-5 text-rt-primary" />
                  <span>..</span>
                </button>
              )}
              
              {directory?.files
                .filter((f: FileEntry) => f.type === 'directory')
                .map((file: FileEntry) => (
                  <div
                    key={file.path}
                    className="flex items-center gap-3 p-3 hover:bg-rt-surface transition-colors group"
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(file.path)}
                      onChange={() => togglePath(file.path)}
                      className="w-4 h-4 rounded border-rt-border bg-rt-surface text-rt-primary focus:ring-rt-primary"
                    />
                    <button
                      onClick={() => setCurrentPath(file.path)}
                      className="flex items-center gap-3 flex-1 text-left"
                    >
                      <Folder className="w-5 h-5 text-rt-primary" />
                      <span className="truncate">{file.name}</span>
                    </button>
                    {selected.has(file.path) && (
                      <div className="badge badge-success text-xs">
                        <Check className="w-3 h-3" />
                      </div>
                    )}
                  </div>
                ))}
            </div>
          )}
        </div>
        
        <div className="text-sm text-rt-text-muted mt-4">
          {selected.size} {selected.size === 1 ? 'folder' : 'folders'} selected
        </div>
        
        <div className="flex gap-3 justify-end mt-4">
          <button onClick={onClose} className="btn-secondary">
            Cancel
          </button>
          <button
            onClick={handleConfirm}
            disabled={selected.size === 0}
            className="btn-primary"
          >
            Add Selected Paths
          </button>
        </div>
      </motion.div>
    </motion.div>
  )
}
