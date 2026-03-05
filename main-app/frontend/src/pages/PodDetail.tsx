import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Server, Folder, FolderOpen, File, RefreshCw } from 'lucide-react'
import { motion } from 'framer-motion'
import { podApi, productApi } from '@/utils/api'
import { FileEntry } from '@/types'

export default function PodDetail() {
  const { podId } = useParams<{ podId: string }>()
  const [currentPath, setCurrentPath] = useState('/')
  
  const { data: pod, isLoading: podLoading } = useQuery({
    queryKey: ['pod', podId],
    queryFn: () => podApi.get(podId!),
    enabled: !!podId,
  })
  
  const { data: products = [] } = useQuery({
    queryKey: ['products'],
    queryFn: () => productApi.list(),
    enabled: !!podId,
  })
  
  // Get all folder groups from all products that belong to this POD
  const groups = products
    .flatMap(p => p.folder_groups)
    .filter(g => g.pod_id === podId)
  
  const { data: directory, isLoading: dirLoading, refetch: refetchDir } = useQuery({
    queryKey: ['pod-browse', podId, currentPath],
    queryFn: () => podApi.browse(podId!, currentPath, false),
    enabled: !!podId && pod?.status === 'online',
  })
  
  if (podLoading) {
    return (
      <div className="p-8">
        <div className="animate-pulse">
          <div className="h-8 bg-rt-surface rounded w-48 mb-4" />
          <div className="h-4 bg-rt-surface rounded w-96" />
        </div>
      </div>
    )
  }
  
  if (!pod) {
    return (
      <div className="p-8">
        <p className="text-rt-accent">POD not found</p>
        <Link to="/pods" className="text-rt-primary hover:underline">
          Back to PODs
        </Link>
      </div>
    )
  }

  const pathParts = currentPath.split('/').filter(Boolean)

  return (
    <div className="p-8">
      {/* Header */}
      <div className="mb-8">
        <Link
          to="/pods"
          className="inline-flex items-center gap-2 text-rt-text-muted hover:text-rt-text mb-4"
        >
          <ArrowLeft className="w-4 h-4" />
          Back to PODs
        </Link>
        
        <div className="flex items-center gap-4">
          <div className={`w-12 h-12 rounded-xl flex items-center justify-center ${
            pod.status === 'online' ? 'bg-rt-success/10' : 'bg-rt-surface'
          }`}>
            <Server className={`w-6 h-6 ${
              pod.status === 'online' ? 'text-rt-success' : 'text-rt-text-muted'
            }`} />
          </div>
          <div>
            <h1 className="text-3xl font-display font-bold">{pod.pod_name}</h1>
            <div className="flex items-center gap-4 text-sm text-rt-text-muted">
              <span>{pod.machine_hostname || 'Not connected'}</span>
              <span>•</span>
              <span className={`badge ${
                pod.status === 'online' ? 'badge-success' : 'badge-error'
              }`}>
                {pod.status}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        {/* File Browser */}
        <motion.div
          initial={{ opacity: 0, x: -20 }}
          animate={{ opacity: 1, x: 0 }}
          className="lg:col-span-2 card"
        >
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-display font-semibold">File Browser</h2>
            <button
              onClick={() => refetchDir()}
              className="p-2 rounded-lg hover:bg-rt-surface transition-colors"
              title="Refresh"
            >
              <RefreshCw className={`w-4 h-4 ${dirLoading ? 'animate-spin' : ''}`} />
            </button>
          </div>
          
          {pod.status !== 'online' ? (
            <p className="text-rt-text-muted text-center py-8">
              POD must be online to browse files
            </p>
          ) : (
            <>
              {/* Breadcrumb */}
              <div className="flex items-center gap-1 text-sm mb-4 overflow-x-auto">
                <button
                  onClick={() => setCurrentPath('/')}
                  className="px-2 py-1 rounded hover:bg-rt-surface"
                >
                  Root
                </button>
                {pathParts.map((part, i) => (
                  <div key={i} className="flex items-center">
                    <span className="text-rt-text-muted">/</span>
                    <button
                      onClick={() => setCurrentPath('/' + pathParts.slice(0, i + 1).join('/'))}
                      className="px-2 py-1 rounded hover:bg-rt-surface"
                    >
                      {part}
                    </button>
                  </div>
                ))}
              </div>
              
              {/* File List */}
              <div className="border border-rt-border rounded-lg divide-y divide-rt-border max-h-[500px] overflow-y-auto">
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
                
                {dirLoading ? (
                  <div className="p-8 text-center text-rt-text-muted">
                    Loading...
                  </div>
                ) : directory?.files.length === 0 ? (
                  <div className="p-8 text-center text-rt-text-muted">
                    Empty directory
                  </div>
                ) : (
                  directory?.files.map((file: FileEntry) => (
                    <div
                      key={file.path}
                      className="flex items-center gap-3 p-3 hover:bg-rt-surface transition-colors cursor-pointer"
                      onClick={() => {
                        if (file.type === 'directory') {
                          setCurrentPath(file.path)
                        }
                      }}
                    >
                      {file.type === 'directory' ? (
                        <Folder className="w-5 h-5 text-rt-primary" />
                      ) : (
                        <File className="w-5 h-5 text-rt-text-muted" />
                      )}
                      <span className="flex-1 truncate">{file.name}</span>
                      <span className="text-xs text-rt-text-muted">
                        {file.type === 'file' && formatSize(file.size)}
                      </span>
                    </div>
                  ))
                )}
              </div>
              
              {directory && (
                <p className="text-xs text-rt-text-muted mt-2">
                  {directory.total_count} items, {formatSize(directory.total_size)} total
                </p>
              )}
            </>
          )}
        </motion.div>

        {/* POD Info & Groups */}
        <motion.div
          initial={{ opacity: 0, x: 20 }}
          animate={{ opacity: 1, x: 0 }}
          className="space-y-6"
        >
          {/* POD Info */}
          <div className="card">
            <h2 className="text-lg font-display font-semibold mb-4">POD Information</h2>
            <div className="space-y-3 text-sm">
              <div className="flex justify-between">
                <span className="text-rt-text-muted">ID</span>
                <span className="font-mono text-xs">{pod.pod_id.slice(0, 8)}...</span>
              </div>
              <div className="flex justify-between">
                <span className="text-rt-text-muted">OS</span>
                <span>{pod.os_type || 'Unknown'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-rt-text-muted">Created</span>
                <span>{pod.created_at ? new Date(pod.created_at).toLocaleDateString() : 'N/A'}</span>
              </div>
              {pod.last_heartbeat && (
                <div className="flex justify-between">
                  <span className="text-rt-text-muted">Last Heartbeat</span>
                  <span>{new Date(pod.last_heartbeat).toLocaleTimeString()}</span>
                </div>
              )}
            </div>
          </div>

          {/* Folder Groups */}
          <div className="card">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-display font-semibold">Folder Groups</h2>
              <Link
                to="/products"
                className="text-sm text-rt-primary hover:underline"
              >
                Manage
              </Link>
            </div>
            
            {groups.length === 0 ? (
              <p className="text-rt-text-muted text-center py-4">
                No folder groups yet
              </p>
            ) : (
              <div className="space-y-2">
                {groups.map((group) => (
                  <div
                    key={group.group_id}
                    className="p-3 rounded-lg bg-rt-surface/50"
                  >
                    <div className="flex items-center justify-between mb-1">
                      <span className="font-medium">{group.group_name}</span>
                      <span className={`badge ${
                        group.training_status === 'completed' ? 'badge-success' :
                        group.training_status === 'training' ? 'badge-warning' :
                        group.training_status === 'failed' ? 'badge-error' :
                        'badge-info'
                      }`}>
                        {group.training_status}
                      </span>
                    </div>
                    <span className="text-xs text-rt-text-muted">
                      {group.group_type} • {group.folder_paths.length} paths
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </motion.div>
      </div>
    </div>
  )
}

function formatSize(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}
