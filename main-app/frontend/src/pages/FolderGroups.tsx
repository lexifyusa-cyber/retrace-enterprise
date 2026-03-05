import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, FolderTree, Trash2, Play, X, Folder, FolderOpen, ChevronRight, Check } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import toast from 'react-hot-toast'
import { podApi, groupApi } from '@/utils/api'
import { FolderGroup, Pod, FileEntry } from '@/types'

export default function FolderGroups() {
  const [showCreateModal, setShowCreateModal] = useState(false)
  const queryClient = useQueryClient()
  
  const { data: groups = [], isLoading } = useQuery({
    queryKey: ['groups'],
    queryFn: () => groupApi.list(),
  })
  
  const { data: pods = [] } = useQuery({
    queryKey: ['pods'],
    queryFn: podApi.list,
  })
  
  const deleteMutation = useMutation({
    mutationFn: groupApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      toast.success('Folder group deleted')
    },
    onError: () => {
      toast.error('Failed to delete folder group')
    },
  })
  
  const trainMutation = useMutation({
    mutationFn: groupApi.startTraining,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      queryClient.invalidateQueries({ queryKey: ['training-jobs'] })
      toast.success('Training started')
    },
    onError: () => {
      toast.error('Failed to start training')
    },
  })

  // Group by POD
  const groupsByPod = groups.reduce((acc, group) => {
    const pod = pods.find(p => p.pod_id === group.pod_id)
    const podName = pod?.pod_name || 'Unknown POD'
    if (!acc[podName]) {
      acc[podName] = []
    }
    acc[podName].push(group)
    return acc
  }, {} as Record<string, FolderGroup[]>)

  return (
    <div className="p-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-display font-bold mb-2">Folder Groups</h1>
          <p className="text-rt-text-muted">
            Organize and train knowledge from your POD folders
          </p>
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className="btn-primary flex items-center gap-2"
          disabled={pods.length === 0}
        >
          <Plus className="w-4 h-4" />
          Create Group
        </button>
      </div>

      {/* Groups */}
      {isLoading ? (
        <div className="space-y-4">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="card animate-pulse">
              <div className="h-6 bg-rt-surface rounded w-48 mb-4" />
              <div className="h-4 bg-rt-surface rounded w-full" />
            </div>
          ))}
        </div>
      ) : groups.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="card text-center py-16"
        >
          <FolderTree className="w-16 h-16 mx-auto text-rt-text-muted mb-4" />
          <h3 className="text-xl font-display font-semibold mb-2">No Folder Groups</h3>
          <p className="text-rt-text-muted mb-6 max-w-md mx-auto">
            {pods.length === 0
              ? 'Create a POD first before adding folder groups.'
              : 'Create folder groups to organize and train knowledge from your POD machines.'}
          </p>
          {pods.length > 0 && (
            <button
              onClick={() => setShowCreateModal(true)}
              className="btn-primary"
            >
              Create Your First Group
            </button>
          )}
        </motion.div>
      ) : (
        <div className="space-y-8">
          {Object.entries(groupsByPod).map(([podName, podGroups]) => (
            <motion.div
              key={podName}
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
            >
              <h2 className="text-lg font-display font-semibold mb-4 flex items-center gap-2">
                <FolderTree className="w-5 h-5 text-rt-primary" />
                {podName}
              </h2>
              
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {podGroups.map((group) => (
                  <GroupCard
                    key={group.group_id}
                    group={group}
                    onDelete={() => {
                      if (confirm(`Delete "${group.group_name}"?`)) {
                        deleteMutation.mutate(group.group_id)
                      }
                    }}
                    onTrain={() => trainMutation.mutate(group.group_id)}
                    isTraining={trainMutation.isPending}
                  />
                ))}
              </div>
            </motion.div>
          ))}
        </div>
      )}

      {/* Create Modal */}
      <AnimatePresence>
        {showCreateModal && (
          <CreateGroupModal
            pods={pods}
            onClose={() => setShowCreateModal(false)}
          />
        )}
      </AnimatePresence>

    </div>
  )
}

function GroupCard({
  group,
  onDelete,
  onTrain,
  isTraining,
}: {
  group: FolderGroup
  onDelete: () => void
  onTrain: () => void
  isTraining: boolean
}) {
  const groupTypeColors: Record<string, string> = {
    code: 'bg-blue-500/10 text-blue-400',
    documentation: 'bg-green-500/10 text-green-400',
    diagrams: 'bg-purple-500/10 text-purple-400',
    configuration: 'bg-yellow-500/10 text-yellow-400',
    tickets: 'bg-orange-500/10 text-orange-400',
    other: 'bg-gray-500/10 text-rt-text-muted',
  }

  return (
    <div className="card group">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-display font-semibold">{group.group_name}</h3>
          <span className={`badge ${groupTypeColors[group.group_type] || groupTypeColors.other}`}>
            {group.group_type}
          </span>
        </div>
        <button
          onClick={onDelete}
          className="p-1.5 rounded text-rt-text-muted hover:text-rt-accent hover:bg-rt-accent/10 opacity-0 group-hover:opacity-100 transition-all"
        >
          <Trash2 className="w-4 h-4" />
        </button>
      </div>
      
      <div className="text-sm text-rt-text-muted mb-4">
        <p className="mb-1">{group.folder_paths.length} paths configured</p>
        <p className="font-mono text-xs truncate">
          {group.folder_paths[0]?.absolute_path || 'No paths'}
        </p>
      </div>
      
      <div className="flex items-center justify-between">
        <span className={`badge ${
          group.training_status === 'completed' ? 'badge-success' :
          group.training_status === 'training' ? 'badge-warning' :
          group.training_status === 'failed' ? 'badge-error' :
          'badge-info'
        }`}>
          {group.training_status}
        </span>
        
        <button
          onClick={onTrain}
          disabled={isTraining || group.training_status === 'training'}
          className="btn-secondary text-sm py-1.5 px-3 flex items-center gap-1.5"
        >
          <Play className="w-3.5 h-3.5" />
          {group.training_status === 'training' ? 'Training...' : 'Train'}
        </button>
      </div>
      
      {group.last_trained && (
        <p className="text-xs text-rt-text-muted mt-3">
          Last trained: {new Date(group.last_trained).toLocaleDateString()}
        </p>
      )}
    </div>
  )
}

function CreateGroupModal({
  pods,
  onClose,
}: {
  pods: Pod[]
  onClose: () => void
}) {
  const [selectedPod, setSelectedPod] = useState(pods[0]?.pod_id || '')
  const [groupName, setGroupName] = useState('')
  const [groupType, setGroupType] = useState('code')
  const [paths, setPaths] = useState<string[]>([])
  const [showBrowser, setShowBrowser] = useState(false)
  const queryClient = useQueryClient()
  
  const selectedPodObj = pods.find(p => p.pod_id === selectedPod)
  const isPodOnline = selectedPodObj?.status === 'online'
  
  const createMutation = useMutation({
    mutationFn: groupApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      toast.success('Folder group created!')
      onClose()
    },
    onError: () => {
      toast.error('Failed to create folder group')
    },
  })
  
  const handleCreate = () => {
    if (!groupName.trim()) {
      toast.error('Please enter a group name')
      return
    }
    
    const validPaths = paths.filter(p => p.trim())
    if (validPaths.length === 0) {
      toast.error('Please add at least one path')
      return
    }
    
    createMutation.mutate({
      pod_id: selectedPod,
      group_name: groupName,
      group_type: groupType,
      folder_paths: validPaths.map(p => ({
        absolute_path: p,
        scan_recursive: true,
      })),
    })
  }
  
  const handlePathsFromBrowser = (selectedPaths: string[]) => {
    setPaths(selectedPaths)
    setShowBrowser(false)
  }

  const groupTypes = [
    { value: 'other', label: 'Auto-detect' },
    { value: 'code', label: 'Code' },
    { value: 'documentation', label: 'Documentation' },
    { value: 'tickets', label: 'Tickets/Issues' },
  ]

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
        <h2 className="text-xl font-display font-semibold mb-6">Create Folder Group</h2>
        
        <div className="space-y-4 mb-6">
          {/* POD Selection */}
          <div>
            <label className="label">POD</label>
            <select
              value={selectedPod}
              onChange={(e) => setSelectedPod(e.target.value)}
              className="input"
            >
              {pods.map((pod) => (
                <option key={pod.pod_id} value={pod.pod_id}>
                  {pod.pod_name} {pod.status !== 'online' && `(${pod.status})`}
                </option>
              ))}
            </select>
          </div>
          
          {/* Group Name */}
          <div>
            <label className="label">Group Name</label>
            <input
              type="text"
              className="input"
              placeholder="e.g., Application Codebase"
              value={groupName}
              onChange={(e) => setGroupName(e.target.value)}
            />
          </div>
          
          {/* Group Type */}
          <div>
            <label className="label">Content Type</label>
            <div className="grid grid-cols-3 gap-2">
              {groupTypes.map((type) => (
                <button
                  key={type.value}
                  type="button"
                  onClick={() => setGroupType(type.value)}
                  className={`p-2 rounded-lg border text-sm transition-all ${
                    groupType === type.value
                      ? 'border-rt-primary bg-rt-primary/10 text-rt-primary'
                      : 'border-rt-border hover:border-rt-text-muted'
                  }`}
                >
                  {type.label}
                </button>
              ))}
            </div>
          </div>
          
          {/* Folder Paths */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="label mb-0">Folder Paths</label>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => {
                    const path = prompt('Enter folder path:')
                    if (path?.trim()) {
                      setPaths([...paths, path.trim()])
                    }
                  }}
                  className="text-sm text-rt-text-muted hover:text-rt-text flex items-center gap-1"
                >
                  <Plus className="w-3.5 h-3.5" />
                  Add Manually
                </button>
                {isPodOnline && (
                  <button
                    type="button"
                    onClick={() => setShowBrowser(true)}
                    className="text-sm text-rt-primary hover:underline flex items-center gap-1"
                  >
                    <FolderOpen className="w-3.5 h-3.5" />
                    Browse POD
                  </button>
                )}
              </div>
            </div>
            
            {paths.length === 0 ? (
              <div className="text-center py-8 bg-rt-surface rounded-lg border border-dashed border-rt-border">
                <Folder className="w-8 h-8 mx-auto text-rt-text-muted mb-2" />
                <p className="text-sm text-rt-text-muted mb-3">
                  {isPodOnline 
                    ? 'No paths selected. Click "Browse POD" to select folders.'
                    : 'POD is offline. Click "Add Manually" to enter paths.'
                  }
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                {paths.map((path, index) => (
                  <div key={index} className="flex items-center gap-2 p-2 bg-rt-surface rounded-lg">
                    <Folder className="w-4 h-4 text-rt-primary flex-shrink-0" />
                    <span className="text-sm flex-1 truncate font-mono">{path}</span>
                    <button
                      type="button"
                      onClick={() => setPaths(paths.filter((_, i) => i !== index))}
                      className="p-1 rounded hover:bg-rt-border text-rt-text-muted"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
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
            {createMutation.isPending ? 'Creating...' : 'Create Group'}
          </button>
        </div>
      </motion.div>
      
      {/* File Browser Modal */}
      <AnimatePresence>
        {showBrowser && (
          <FileBrowserModal
            podId={selectedPod}
            selectedPaths={paths}
            onConfirm={handlePathsFromBrowser}
            onClose={() => setShowBrowser(false)}
          />
        )}
      </AnimatePresence>
    </motion.div>
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
  const [currentPath, setCurrentPath] = useState('/')
  const [selected, setSelected] = useState<Set<string>>(new Set(selectedPaths))
  
  const { data: directory, isLoading, refetch } = useQuery({
    queryKey: ['pod-browse', podId, currentPath],
    queryFn: () => podApi.browse(podId, currentPath, false),
    enabled: !!podId,
  })
  
  const pathParts = currentPath.split('/').filter(Boolean)
  
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
        
        {/* Breadcrumb */}
        <div className="flex items-center gap-1 text-sm mb-4 overflow-x-auto bg-rt-surface rounded-lg px-3 py-2">
          <button
            onClick={() => setCurrentPath('/')}
            className={`px-2 py-1 rounded hover:bg-rt-border transition-colors ${
              currentPath === '/' ? 'text-rt-primary' : ''
            }`}
          >
            Root
          </button>
          {pathParts.map((part, i) => (
            <div key={i} className="flex items-center">
              <ChevronRight className="w-4 h-4 text-rt-text-muted" />
              <button
                onClick={() => setCurrentPath('/' + pathParts.slice(0, i + 1).join('/'))}
                className={`px-2 py-1 rounded hover:bg-rt-border transition-colors ${
                  i === pathParts.length - 1 ? 'text-rt-primary' : ''
                }`}
              >
                {part}
              </button>
            </div>
          ))}
        </div>
        
        {/* File List */}
        <div className="flex-1 border border-rt-border rounded-lg overflow-hidden flex flex-col">
          {isLoading ? (
            <div className="flex-1 flex items-center justify-center text-rt-text-muted">
              Loading...
            </div>
          ) : (
            <div className="flex-1 overflow-y-auto divide-y divide-rt-border">
              {/* Parent directory */}
              {currentPath !== '/' && (
                <button
                  onClick={() => {
                    const parent = '/' + pathParts.slice(0, -1).join('/')
                    setCurrentPath(parent || '/')
                  }}
                  className="w-full flex items-center gap-3 p-3 hover:bg-rt-surface transition-colors text-left"
                >
                  <FolderOpen className="w-5 h-5 text-rt-primary" />
                  <span>..</span>
                </button>
              )}
              
              {/* Directories */}
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
        
        {/* Selected count */}
        <div className="text-sm text-rt-text-muted mt-4">
          {selected.size} {selected.size === 1 ? 'folder' : 'folders'} selected
        </div>
        
        {/* Actions */}
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
