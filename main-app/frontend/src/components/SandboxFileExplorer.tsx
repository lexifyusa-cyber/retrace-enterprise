import { useState, useEffect, useCallback } from 'react'
import { FolderOpen, File, ChevronRight, Download, RefreshCw, ArrowLeft } from 'lucide-react'

interface FileEntry {
  name: string
  is_dir: boolean
  size: number
  modified_at: number
}

interface SandboxFileExplorerProps {
  conversationId: string | null
}

function formatSize(bytes: number): string {
  if (bytes === 0) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatDate(ts: number): string {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
  })
}

function getFileIcon(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  const icons: Record<string, string> = {
    py: 'text-yellow-400', js: 'text-yellow-300', ts: 'text-blue-400',
    tsx: 'text-blue-400', jsx: 'text-yellow-300', json: 'text-green-400',
    md: 'text-gray-400', txt: 'text-gray-400', html: 'text-orange-400',
    css: 'text-blue-300', sh: 'text-green-300', yml: 'text-pink-400',
    yaml: 'text-pink-400', toml: 'text-orange-300', cfg: 'text-gray-400',
  }
  return icons[ext] || 'text-rt-text-secondary'
}

const authHeaders = () => ({
  Authorization: `Bearer ${localStorage.getItem('rt_access_token') || ''}`,
})

export default function SandboxFileExplorer({ conversationId }: SandboxFileExplorerProps) {
  const [currentPath, setCurrentPath] = useState('/')
  const [entries, setEntries] = useState<FileEntry[]>([])
  const [rootDir, setRootDir] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [fileContent, setFileContent] = useState<string | null>(null)
  const [fileLoading, setFileLoading] = useState(false)

  const loadFiles = useCallback(async (path: string) => {
    if (!conversationId) return
    setLoading(true)
    try {
      const res = await fetch(`/api/v1/agent/sandbox/${conversationId}/files?path=${encodeURIComponent(path)}`, { headers: authHeaders() })
      if (res.ok) {
        const data = await res.json()
        setEntries(data.entries || [])
        setRootDir(data.root_dir ?? null)
        setCurrentPath(path)
      }
    } catch {
      // ignore
    }
    setLoading(false)
  }, [conversationId])

  useEffect(() => {
    loadFiles('/')
  }, [loadFiles])

  // Auto-refresh every 5 seconds
  useEffect(() => {
    const interval = setInterval(() => {
      loadFiles(currentPath)
    }, 5000)
    return () => clearInterval(interval)
  }, [currentPath, loadFiles])

  const openFile = async (name: string) => {
    const filePath = currentPath === '/' ? `/${name}` : `${currentPath}/${name}`
    setSelectedFile(filePath)
    setFileLoading(true)
    setFileContent(null)
    try {
      const res = await fetch(`/api/v1/agent/sandbox/${conversationId}/file?path=${encodeURIComponent(filePath)}`, { headers: authHeaders() })
      if (res.ok) {
        const data = await res.json()
        setFileContent(data.content)
      } else {
        setFileContent('(unable to read file)')
      }
    } catch {
      setFileContent('(error reading file)')
    }
    setFileLoading(false)
  }

  const downloadFile = async (name: string) => {
    const filePath = currentPath === '/' ? `/${name}` : `${currentPath}/${name}`
    try {
      const res = await fetch(`/api/v1/agent/sandbox/${conversationId}/download?path=${encodeURIComponent(filePath)}`, { headers: authHeaders() })
      if (res.ok) {
        const blob = await res.blob()
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = name
        a.click()
        URL.revokeObjectURL(url)
      }
    } catch {
      // ignore
    }
  }

  const navigateUp = () => {
    if (currentPath === '/') return
    const parts = currentPath.split('/').filter(Boolean)
    parts.pop()
    setCurrentPath(parts.length ? '/' + parts.join('/') : '/')
    setSelectedFile(null)
    setFileContent(null)
    loadFiles(parts.length ? '/' + parts.join('/') : '/')
  }

  const navigateInto = (dirName: string) => {
    const newPath = currentPath === '/' ? `/${dirName}` : `${currentPath}/${dirName}`
    setSelectedFile(null)
    setFileContent(null)
    loadFiles(newPath)
  }

  if (!conversationId) {
    return (
      <div className="flex items-center justify-center h-full text-rt-text-secondary text-sm">
        Start a conversation to see sandbox files
      </div>
    )
  }

  // File content viewer
  if (selectedFile && fileContent !== null) {
    return (
      <div className="flex flex-col h-full bg-rt-surface">
        {/* Header */}
        <div className="flex items-center gap-2 px-3 py-2 border-b border-rt-border/50 bg-rt-surface">
          <button
            onClick={() => { setSelectedFile(null); setFileContent(null) }}
            className="p-1 rounded hover:bg-rt-border/30 text-rt-text-secondary"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
          <File className={`w-4 h-4 ${getFileIcon(selectedFile.split('/').pop() || '')}`} />
          <span className="text-sm text-rt-text font-mono truncate">{selectedFile}</span>
          <button
            onClick={() => downloadFile(selectedFile.split('/').pop() || '')}
            className="ml-auto p-1 rounded hover:bg-rt-border/30 text-rt-text-secondary"
            title="Download"
          >
            <Download className="w-4 h-4" />
          </button>
        </div>
        {/* Content */}
        <div className="flex-1 overflow-auto p-3">
          {fileLoading ? (
            <div className="text-rt-text-secondary text-sm">Loading...</div>
          ) : (
            <pre className="text-xs font-mono text-rt-text whitespace-pre-wrap break-words leading-relaxed">
              {fileContent}
            </pre>
          )}
        </div>
      </div>
    )
  }

  // File browser
  return (
    <div className="flex flex-col h-full bg-rt-surface">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-rt-border/50">
        {currentPath !== '/' && (
          <button
            onClick={navigateUp}
            className="p-1 rounded hover:bg-rt-border/30 text-rt-text-secondary"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
        )}
        <FolderOpen className="w-4 h-4 text-rt-primary" />
        <span className="text-sm text-rt-text font-mono truncate">
          {rootDir ? rootDir.replace(/^\/Users\/[^/]+/, '~') : 'agent-workspace'}{currentPath === '/' ? '' : currentPath}
        </span>
        <button
          onClick={() => loadFiles(currentPath)}
          className="ml-auto p-1 rounded hover:bg-rt-border/30 text-rt-text-secondary"
          title="Refresh"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* File list */}
      <div className="flex-1 overflow-auto">
        {entries.length === 0 ? (
          <div className="flex items-center justify-center h-full text-rt-text-secondary text-sm">
            {loading ? 'Loading...' : 'No files yet'}
          </div>
        ) : (
          <div className="divide-y divide-rt-border/20">
            {entries.map((entry) => (
              <div
                key={entry.name}
                className="flex items-center gap-2 px-3 py-1.5 hover:bg-rt-border/20 cursor-pointer group"
                onClick={() => entry.is_dir ? navigateInto(entry.name) : openFile(entry.name)}
              >
                {entry.is_dir ? (
                  <>
                    <ChevronRight className="w-3.5 h-3.5 text-rt-text-secondary" />
                    <FolderOpen className="w-4 h-4 text-rt-primary" />
                  </>
                ) : (
                  <>
                    <span className="w-3.5" />
                    <File className={`w-4 h-4 ${getFileIcon(entry.name)}`} />
                  </>
                )}
                <span className="text-sm text-rt-text truncate flex-1">{entry.name}</span>
                <span className="text-xs text-rt-text-secondary">{formatSize(entry.size)}</span>
                <span className="text-xs text-rt-text-secondary hidden group-hover:block">
                  {formatDate(entry.modified_at)}
                </span>
                {!entry.is_dir && (
                  <button
                    onClick={(e) => { e.stopPropagation(); downloadFile(entry.name) }}
                    className="p-1 rounded hover:bg-rt-border/30 text-rt-text-secondary opacity-0 group-hover:opacity-100"
                    title="Download"
                  >
                    <Download className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
