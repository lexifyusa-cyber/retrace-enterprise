import { useState, useEffect, useRef, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Search, LayoutDashboard, FolderTree, Bot, Settings,
  ClipboardList, FileText, ArrowRight, Server, MessageSquare,
  Zap, Users,
} from 'lucide-react'
import { useAuth } from '@/context/AuthContext'
import { motion, AnimatePresence } from 'framer-motion'

interface CommandPaletteProps {
  isOpen: boolean
  onClose: () => void
}

interface CommandItem {
  id: string
  label: string
  description?: string
  icon: any
  action: () => void
  category: string
  keywords: string[]
}

const ROLE_LEVEL: Record<string, number> = {
  zero_admin: 110,
  admin: 100, super_admin: 100, tenant_admin: 100,
  user_admin: 60,
  user: 20,
}

function hasAccess(userRole: string, minRole?: string): boolean {
  if (!minRole) return true
  return (ROLE_LEVEL[userRole] || 0) >= (ROLE_LEVEL[minRole] || 0)
}

export default function CommandPalette({ isOpen, onClose }: CommandPaletteProps) {
  const { user } = useAuth()
  const [query, setQuery] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const [selectedIndex, setSelectedIndex] = useState(0)
  const navigate = useNavigate()
  const role = user?.role || 'user'

  const commands: CommandItem[] = useMemo(() => {
    const base = [
    // Navigation
    { id: 'nav-dashboard', label: 'Go to Dashboard', description: 'System overview and stats', icon: LayoutDashboard, action: () => { navigate('/dashboard'); onClose() }, category: 'Navigation', keywords: ['home', 'overview', 'stats'] },
    { id: 'nav-teams', label: 'Go to Teams', description: 'Manage teams and training', icon: FolderTree, action: () => { navigate('/teams'); onClose() }, category: 'Navigation', keywords: ['folders', 'groups', 'train'] },
    { id: 'nav-agent', label: 'Go to Agent', description: 'AI-powered task automation', icon: Bot, action: () => { navigate('/agent'); onClose() }, category: 'Navigation', keywords: ['ai', 'chat', 'task', 'automation'] },
    { id: 'nav-automations', label: 'Go to Automations', description: 'Scheduled workflows and SOPs', icon: ClipboardList, action: () => { navigate('/sops'); onClose() }, category: 'Navigation', keywords: ['sop', 'schedule', 'workflow'] },
    { id: 'nav-docs', label: 'Go to Documentation', description: 'Generated documentation', icon: FileText, action: () => { navigate('/docs'); onClose() }, category: 'Navigation', keywords: ['docs', 'documentation', 'wiki'] },
    { id: 'nav-pods', label: 'Go to PODs', description: 'Manage distributed PODs', icon: Server, action: () => { navigate('/pods'); onClose() }, category: 'Navigation', keywords: ['servers', 'nodes', 'distributed'] },
    { id: 'nav-settings', label: 'Go to Settings', description: 'API keys and configuration', icon: Settings, action: () => { navigate('/settings'); onClose() }, category: 'Navigation', keywords: ['config', 'api', 'llm', 'preferences'] },
    ...(hasAccess(role, 'user_admin') ? [{ id: 'nav-users', label: 'Go to User Management', description: 'Manage users, roles, and access', icon: Users, action: () => { navigate('/users'); onClose() }, category: 'Navigation' as const, keywords: ['users', 'people', 'roles', 'admin'] }] : []),
    // Actions
    { id: 'action-ask', label: 'Ask a Question', description: 'Query your knowledge base', icon: MessageSquare, action: () => { navigate('/agent'); onClose() }, category: 'Actions', keywords: ['query', 'question', 'search', 'ask'] },
    { id: 'action-create-team', label: 'Create New Team', description: 'Set up a new team', icon: FolderTree, action: () => { navigate('/teams'); onClose() }, category: 'Actions', keywords: ['new', 'create', 'add'] },
    { id: 'action-agent', label: 'Start Agent Task', description: 'Run an AI-powered task', icon: Zap, action: () => { navigate('/agent'); onClose() }, category: 'Actions', keywords: ['run', 'execute', 'task'] },
    ]
    return base
  }, [navigate, onClose, role])

  // Filter commands based on query
  const filtered = useMemo(() => {
    if (!query.trim()) return commands
    const q = query.toLowerCase()
    return commands.filter(cmd =>
      cmd.label.toLowerCase().includes(q) ||
      cmd.description?.toLowerCase().includes(q) ||
      cmd.keywords.some(k => k.includes(q))
    )
  }, [query, commands])

  // Group by category
  const grouped = useMemo(() => {
    const groups: Record<string, CommandItem[]> = {}
    filtered.forEach(cmd => {
      if (!groups[cmd.category]) groups[cmd.category] = []
      groups[cmd.category].push(cmd)
    })
    return groups
  }, [filtered])

  const flatFiltered = useMemo(() => filtered, [filtered])

  useEffect(() => {
    setSelectedIndex(0)
  }, [query])

  useEffect(() => {
    if (isOpen) {
      setQuery('')
      setSelectedIndex(0)
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [isOpen])

  // Keyboard navigation
  useEffect(() => {
    if (!isOpen) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      } else if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedIndex(i => Math.min(i + 1, flatFiltered.length - 1))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedIndex(i => Math.max(i - 1, 0))
      } else if (e.key === 'Enter') {
        e.preventDefault()
        if (flatFiltered[selectedIndex]) {
          flatFiltered[selectedIndex].action()
        }
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [isOpen, onClose, flatFiltered, selectedIndex])

  if (!isOpen) return null

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-[100] flex items-start justify-center pt-[15vh]"
      >
        {/* Backdrop */}
        <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

        {/* Palette */}
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: -10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: -10 }}
          transition={{ duration: 0.15 }}
          className="relative w-full max-w-lg bg-rt-bg-light border border-rt-border/60 rounded-xl shadow-2xl shadow-black/40 overflow-hidden"
        >
          {/* Search input */}
          <div className="flex items-center gap-3 px-4 py-3 border-b border-rt-border/40">
            <Search className="w-5 h-5 text-rt-text-muted flex-shrink-0" />
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Type a command or search..."
              className="flex-1 bg-transparent text-sm text-rt-text placeholder-rt-text-muted/60 focus:outline-none"
              autoComplete="off"
            />
            <kbd className="px-1.5 py-0.5 rounded bg-rt-surface border border-rt-border/50 text-[10px] font-mono text-rt-text-muted">
              ESC
            </kbd>
          </div>

          {/* Results */}
          <div className="max-h-[50vh] overflow-y-auto py-2">
            {flatFiltered.length === 0 ? (
              <div className="px-4 py-8 text-center text-sm text-rt-text-muted">
                No results found for "{query}"
              </div>
            ) : (
              Object.entries(grouped).map(([category, items]) => (
                <div key={category}>
                  <div className="px-4 py-1.5 text-[10px] font-semibold text-rt-text-muted/60 uppercase tracking-wider">
                    {category}
                  </div>
                  {items.map((cmd) => {
                    const globalIndex = flatFiltered.indexOf(cmd)
                    const isSelected = globalIndex === selectedIndex
                    return (
                      <button
                        key={cmd.id}
                        onClick={cmd.action}
                        onMouseEnter={() => setSelectedIndex(globalIndex)}
                        className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                          isSelected ? 'bg-rt-primary/10 text-rt-primary' : 'text-rt-text hover:bg-rt-surface/50'
                        }`}
                      >
                        <cmd.icon className={`w-4 h-4 flex-shrink-0 ${isSelected ? 'text-rt-primary' : 'text-rt-text-muted'}`} />
                        <div className="flex-1 min-w-0">
                          <div className="text-sm font-medium">{cmd.label}</div>
                          {cmd.description && (
                            <div className="text-[11px] text-rt-text-muted truncate">{cmd.description}</div>
                          )}
                        </div>
                        {isSelected && <ArrowRight className="w-3.5 h-3.5 flex-shrink-0" />}
                      </button>
                    )
                  })}
                </div>
              ))
            )}
          </div>

          {/* Footer hint */}
          <div className="px-4 py-2 border-t border-rt-border/30 flex items-center gap-4 text-[10px] text-rt-text-muted/50">
            <span className="flex items-center gap-1">
              <kbd className="px-1 py-0.5 rounded bg-rt-surface/50 border border-rt-border/30 font-mono">↑↓</kbd>
              Navigate
            </span>
            <span className="flex items-center gap-1">
              <kbd className="px-1 py-0.5 rounded bg-rt-surface/50 border border-rt-border/30 font-mono">↵</kbd>
              Select
            </span>
            <span className="flex items-center gap-1">
              <kbd className="px-1 py-0.5 rounded bg-rt-surface/50 border border-rt-border/30 font-mono">esc</kbd>
              Close
            </span>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}
