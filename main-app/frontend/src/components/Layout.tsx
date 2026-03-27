import { useState, useEffect, useRef, useCallback } from 'react'
import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import RetraceLogo from '@/components/RetraceLogo'
import {
  LayoutDashboard,
  FolderTree,
  Bot,
  ClipboardList,
  FileText,
  Settings,
  LogOut,
  User,
  Bell,
  Search,
  CheckCircle2,
  XCircle,
  Loader2,
  Clock,
  Package,
  Zap,
  BookOpen,
  SlidersHorizontal,
  ArrowRight,
  Command,
  Blocks,
  Users,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { clsx } from 'clsx'
import { useAuth } from '@/context/AuthContext'
import { agentApi, productApi, AGENT_GENERAL_SCOPE } from '@/utils/api'
import ProfilePanel from './ProfilePanel'

function UserMenu({ onOpenProfile }: { onOpenProfile?: () => void }) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  if (!user) return null
  return (
    <div className="flex items-center gap-3 px-4 pt-6">
      <button
        type="button"
        onClick={onOpenProfile}
        className="flex items-center gap-3 flex-1 min-w-0 text-left rounded-xl hover:bg-rt-primary-fixed/20 transition-colors p-1"
        title="Open profile"
      >
        <div className="w-10 h-10 rounded-full bg-rt-primary-fixed flex items-center justify-center flex-shrink-0 overflow-hidden">
          <User className="w-5 h-5 text-rt-primary" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-bold truncate">{user.display_name || user.email}</p>
          <p className="text-[10px] text-rt-text-muted/50 truncate">Enterprise Edition</p>
        </div>
      </button>
      <button
        onClick={async () => {
          await logout()
          navigate('/login')
        }}
        className="text-rt-text-muted hover:text-rt-accent transition-colors p-1"
        title="Sign out"
      >
        <LogOut className="w-4 h-4" />
      </button>
    </div>
  )
}

interface NavItem {
  name: string
  href: string
  icon: LucideIcon
}

const navigation: NavItem[] = [
  { name: 'Dashboard', href: '/dashboard', icon: LayoutDashboard },
  { name: 'Products', href: '/products', icon: FolderTree },
  { name: 'Agent', href: '/agent', icon: Bot },
  { name: 'Automations', href: '/sops', icon: ClipboardList },
  { name: 'Documentation', href: '/docs', icon: FileText },
  { name: 'MCP Builder', href: '/mcp-builder', icon: Blocks },
  { name: 'User Management', href: '/users', icon: Users },
]

// ---------------------------------------------------------------------------
// Notification dropdown
// ---------------------------------------------------------------------------
function NotificationDropdown({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [notifications, setNotifications] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [readIds, setReadIds] = useState<Set<string>>(() => {
    try {
      const stored = localStorage.getItem('rt_read_notifications')
      return stored ? new Set(JSON.parse(stored)) : new Set()
    } catch { return new Set() }
  })
  const ref = useRef<HTMLDivElement>(null)

  // Close on outside click
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open, onClose])

  // Fetch recent runs on open
  useEffect(() => {
    if (!open) return
    setLoading(true)
    agentApi.getDashboardStats()
      .then(data => {
        setNotifications(data?.recent_runs || [])
      })
      .catch(() => setNotifications([]))
      .finally(() => setLoading(false))
  }, [open])

  const markAsRead = (runId: string) => {
    setReadIds(prev => {
      const next = new Set(prev)
      next.add(runId)
      localStorage.setItem('rt_read_notifications', JSON.stringify([...next]))
      return next
    })
  }

  const markAllRead = () => {
    const allIds = new Set(notifications.map(n => n.run_id))
    setReadIds(prev => {
      const next = new Set([...prev, ...allIds])
      localStorage.setItem('rt_read_notifications', JSON.stringify([...next]))
      return next
    })
  }

  const unreadCount = notifications.filter(n => !readIds.has(n.run_id)).length

  if (!open) return null

  return (
    <div ref={ref} className="absolute right-16 top-14 w-96 bg-rt-bg-light border border-rt-border/50 rounded-2xl shadow-2xl z-50 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-3.5 border-b border-rt-border/30">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-bold">Notifications</h3>
          {unreadCount > 0 && (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-rt-primary-container/20 text-rt-primary-container">
              {unreadCount} new
            </span>
          )}
        </div>
        {unreadCount > 0 && (
          <button onClick={markAllRead} className="text-[11px] font-medium text-rt-primary-container hover:underline">
            Mark all read
          </button>
        )}
      </div>

      {/* Content */}
      <div className="max-h-80 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center gap-2 py-10 text-rt-text-muted">
            <Loader2 className="w-4 h-4 animate-spin" />
            <span className="text-xs">Loading...</span>
          </div>
        ) : notifications.length === 0 ? (
          <div className="text-center py-10">
            <Bell className="w-6 h-6 text-rt-text-muted/30 mx-auto mb-2" />
            <p className="text-xs text-rt-text-muted">No recent activity</p>
          </div>
        ) : (
          notifications.map((run: any) => {
            const isRead = readIds.has(run.run_id)
            return (
              <div
                key={run.run_id}
                onClick={() => markAsRead(run.run_id)}
                className={`flex items-start gap-3 px-5 py-3 cursor-pointer transition-colors border-b border-rt-border/10 last:border-0 ${
                  isRead ? 'opacity-50 hover:opacity-70' : 'hover:bg-rt-surface/30'
                }`}
              >
                <div className={`w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 mt-0.5 ${
                  run.status === 'completed' ? 'bg-emerald-500/10' : run.status === 'failed' ? 'bg-red-500/10' : 'bg-blue-500/10'
                }`}>
                  {run.status === 'completed' ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                  ) : run.status === 'failed' ? (
                    <XCircle className="w-3.5 h-3.5 text-red-400" />
                  ) : (
                    <Loader2 className="w-3.5 h-3.5 text-blue-400 animate-spin" />
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-semibold truncate">{run.sop_title || 'Automation Run'}</p>
                  <p className="text-[10px] text-rt-text-muted mt-0.5">
                    {run.status === 'completed' ? 'Completed successfully' : run.status === 'failed' ? 'Failed' : 'Running'}
                    {' '}&middot; {run.steps_completed}/{run.steps_total} steps
                    {run.duration_ms ? ` · ${(run.duration_ms / 1000).toFixed(1)}s` : ''}
                  </p>
                  <p className="text-[10px] text-rt-text-muted/50 mt-0.5 flex items-center gap-1">
                    <Clock className="w-2.5 h-2.5" />
                    {run.started_at ? timeAgo(run.started_at) : ''}
                  </p>
                </div>
                {!isRead && (
                  <div className="w-2 h-2 rounded-full bg-rt-primary-container flex-shrink-0 mt-2" />
                )}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Global Search
// ---------------------------------------------------------------------------
interface SearchResult {
  id: string
  title: string
  subtitle: string
  type: 'product' | 'automation' | 'documentation' | 'page' | 'setting'
  icon: LucideIcon
  href: string
}

const staticPages: SearchResult[] = [
  { id: 'page-dashboard', title: 'Dashboard', subtitle: 'Overview and stats', type: 'page', icon: LayoutDashboard, href: '/dashboard' },
  { id: 'page-products', title: 'Products', subtitle: 'Manage your products', type: 'page', icon: FolderTree, href: '/products' },
  { id: 'page-agent', title: 'Agent', subtitle: 'AI-powered task execution', type: 'page', icon: Bot, href: '/agent' },
  { id: 'page-automations', title: 'Automations', subtitle: 'Manage workflows', type: 'page', icon: ClipboardList, href: '/sops' },
  { id: 'page-docs', title: 'Documentation', subtitle: 'Knowledge articles', type: 'page', icon: FileText, href: '/docs' },
  { id: 'page-mcp-builder', title: 'MCP Builder', subtitle: 'Build MCP servers from APIs', type: 'page', icon: Blocks, href: '/mcp-builder' },
  { id: 'page-settings', title: 'Settings', subtitle: 'Platform configuration', type: 'page', icon: Settings, href: '/settings' },
  { id: 'setting-theme', title: 'Interface Identity', subtitle: 'Theme and appearance settings', type: 'setting', icon: SlidersHorizontal, href: '/settings' },
  { id: 'setting-llm', title: 'Intelligence Engine', subtitle: 'LLM provider configuration', type: 'setting', icon: Zap, href: '/settings' },
]

function GlobalSearch() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [selectedIdx, setSelectedIdx] = useState(0)
  const [dynamicItems, setDynamicItems] = useState<SearchResult[]>([])
  const [loaded, setLoaded] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  // Load dynamic items once on first focus
  const loadDynamicItems = useCallback(async () => {
    if (loaded) return
    setLoaded(true)
    try {
      const items: SearchResult[] = []
      // Products
      const products = await productApi.list()
      for (const p of products) {
        items.push({
          id: `product-${p.product_id}`,
          title: p.product_name,
          subtitle: `Product · ${p.folder_groups?.length || 0} folder groups`,
          type: 'product',
          icon: Package,
          href: `/products`,
        })
      }
      // SOPs
      const trainedProducts = products.filter((p: any) => p.folder_groups?.some((g: any) => g.training_status === 'completed'))
      const sopScopes = [AGENT_GENERAL_SCOPE, ...trainedProducts.map((p: any) => p.product_id)]
      for (const scope of sopScopes) {
        try {
          const sops = await agentApi.listSOPs(scope)
          if (Array.isArray(sops)) {
            for (const sop of sops) {
              items.push({
                id: `sop-${sop.sop_id}`,
                title: sop.title,
                subtitle: `Automation · ${sop.status || 'draft'}`,
                type: 'automation',
                icon: ClipboardList,
                href: '/sops',
              })
            }
          }
        } catch { /* skip */ }
      }
      // Docs
      for (const scope of sopScopes) {
        try {
          const docs = await agentApi.listDocs(scope)
          if (Array.isArray(docs)) {
            for (const doc of docs) {
              items.push({
                id: `doc-${doc.doc_id}`,
                title: doc.title,
                subtitle: `Documentation · ${doc.doc_type || 'article'}`,
                type: 'documentation',
                icon: BookOpen,
                href: '/docs',
              })
            }
          }
        } catch { /* skip */ }
      }
      setDynamicItems(items)
    } catch { /* ignore */ }
  }, [loaded])

  // Filter results
  useEffect(() => {
    if (!query.trim()) {
      setResults([])
      return
    }
    const q = query.toLowerCase()
    const all = [...staticPages, ...dynamicItems]
    const filtered = all.filter(r =>
      r.title.toLowerCase().includes(q) ||
      r.subtitle.toLowerCase().includes(q)
    ).slice(0, 8)
    setResults(filtered)
    setSelectedIdx(0)
  }, [query, dynamicItems])

  // Close on outside click
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  // Keyboard shortcut: Cmd+K
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault()
        inputRef.current?.focus()
        setOpen(true)
        loadDynamicItems()
      }
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [loadDynamicItems])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setSelectedIdx(i => Math.min(i + 1, results.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSelectedIdx(i => Math.max(i - 1, 0)) }
    else if (e.key === 'Enter' && results[selectedIdx]) {
      e.preventDefault()
      navigate(results[selectedIdx].href)
      setOpen(false)
      setQuery('')
      inputRef.current?.blur()
    }
  }

  const handleSelect = (r: SearchResult) => {
    navigate(r.href)
    setOpen(false)
    setQuery('')
    inputRef.current?.blur()
  }

  const typeColors: Record<string, string> = {
    page: 'bg-rt-primary-container/20 text-rt-primary-container border border-rt-primary-container/30',
    product: 'bg-blue-500/15 text-blue-400 border border-blue-500/30',
    automation: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30',
    documentation: 'bg-indigo-500/15 text-indigo-400 border border-indigo-500/30',
    setting: 'bg-rt-surface/80 text-rt-text border border-rt-border/50',
  }

  return (
    <div ref={dropdownRef} className="relative">
      <div className={`flex items-center gap-3 rounded-xl px-4 py-2.5 w-96 transition-all border ${
        open ? 'bg-rt-bg border-rt-primary-container shadow-lg shadow-rt-primary-container/15' : 'bg-rt-surface/50 border-rt-border hover:border-rt-primary-container/40'
      }`}>
        <Search className={`w-4 h-4 flex-shrink-0 ${open ? 'text-rt-primary-container' : 'text-rt-text-muted'}`} />
        <input
          ref={inputRef}
          className="bg-transparent border-none focus:ring-0 focus:outline-none text-sm w-full placeholder:text-rt-text-muted font-body"
          placeholder="Search anything..."
          type="text"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onFocus={() => { setOpen(true); loadDynamicItems() }}
          onKeyDown={handleKeyDown}
        />
        <kbd className="hidden sm:flex items-center gap-0.5 px-2 py-0.5 rounded-md bg-rt-surface border border-rt-border text-[10px] text-rt-text-muted font-mono flex-shrink-0">
          <Command className="w-2.5 h-2.5" />K
        </kbd>
      </div>

      {/* Dropdown */}
      {open && (query.trim() ? results.length > 0 : true) && (
        <div className="absolute top-full left-0 mt-2 w-[28rem] bg-rt-bg-light border border-rt-border/50 rounded-2xl shadow-2xl z-50 overflow-hidden">
          {query.trim() === '' ? (
            <div className="px-4 py-3">
              <p className="text-[10px] font-bold uppercase tracking-wider text-rt-text-muted mb-2">Quick Navigation</p>
              <div className="space-y-0.5">
                {staticPages.filter(p => p.type === 'page').map(page => (
                  <button
                    key={page.id}
                    onClick={() => handleSelect(page)}
                    className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left hover:bg-rt-surface/40 transition-colors"
                  >
                    <page.icon className="w-4 h-4 text-rt-text-muted" />
                    <span className="text-sm font-medium">{page.title}</span>
                    <ArrowRight className="w-3 h-3 text-rt-text-muted/30 ml-auto" />
                  </button>
                ))}
              </div>
              {dynamicItems.length > 0 && (
                <>
                  <p className="text-[10px] font-bold uppercase tracking-wider text-rt-text-muted mb-2 mt-3">Recent Items</p>
                  <div className="space-y-0.5">
                    {dynamicItems.slice(0, 4).map(item => (
                      <button
                        key={item.id}
                        onClick={() => handleSelect(item)}
                        className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left hover:bg-rt-surface/40 transition-colors"
                      >
                        <item.icon className="w-4 h-4 text-rt-text-muted" />
                        <div className="flex-1 min-w-0">
                          <span className="text-sm font-medium truncate block">{item.title}</span>
                          <span className="text-[10px] text-rt-text-muted truncate block">{item.subtitle}</span>
                        </div>
                        <span className={`px-1.5 py-0.5 rounded text-[9px] font-semibold ${typeColors[item.type]}`}>
                          {item.type}
                        </span>
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>
          ) : results.length > 0 ? (
            <div className="py-2">
              <p className="px-4 py-1 text-[10px] font-bold uppercase tracking-wider text-rt-text-muted">
                {results.length} result{results.length !== 1 ? 's' : ''}
              </p>
              {results.map((r, idx) => (
                <button
                  key={r.id}
                  onClick={() => handleSelect(r)}
                  onMouseEnter={() => setSelectedIdx(idx)}
                  className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                    idx === selectedIdx ? 'bg-rt-primary-container/10' : 'hover:bg-rt-surface/30'
                  }`}
                >
                  <div className={`w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0 ${typeColors[r.type]}`}>
                    <r.icon className="w-4 h-4" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-semibold truncate">{r.title}</p>
                    <p className="text-[10px] text-rt-text-muted truncate">{r.subtitle}</p>
                  </div>
                  <span className={`px-2 py-0.5 rounded-md text-[9px] font-semibold flex-shrink-0 ${typeColors[r.type]}`}>
                    {r.type}
                  </span>
                  {idx === selectedIdx && <ArrowRight className="w-3 h-3 text-rt-primary-container flex-shrink-0" />}
                </button>
              ))}
            </div>
          ) : (
            <div className="text-center py-8">
              <Search className="w-5 h-5 text-rt-text-muted/30 mx-auto mb-2" />
              <p className="text-xs text-rt-text-muted">No results for "{query}"</p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function timeAgo(dateStr: string): string {
  const now = Date.now()
  const then = new Date(dateStr).getTime()
  const diff = Math.max(0, now - then)
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}

export default function Layout() {
  const [profileOpen, setProfileOpen] = useState(false)
  const [notifOpen, setNotifOpen] = useState(false)
  return (
    <div className="flex h-screen bg-rt-bg">
      {/* Sidebar — warm editorial style */}
      <aside className="w-64 bg-rt-bg-light flex flex-col py-8 px-4 flex-shrink-0">
        {/* Brand */}
        <div className="mb-10 px-4 flex items-center gap-3">
          <RetraceLogo variant="md" />
        </div>

        {/* Navigation */}
        <nav className="flex-1 space-y-1">
          {navigation.map((item) => (
            <NavLink
              key={item.name}
              to={item.href}
              className={({ isActive }) =>
                clsx(
                  'flex items-center gap-4 px-4 py-3 text-sm font-medium transition-all duration-200 relative',
                  isActive
                    ? 'text-rt-primary font-bold bg-rt-primary-fixed/20 border-r-[3px] border-rt-primary-container'
                    : 'text-rt-text-muted hover:bg-rt-primary-fixed/10 hover:text-rt-text'
                )
              }
            >
              <item.icon className="w-5 h-5" />
              <span>{item.name}</span>
            </NavLink>
          ))}
        </nav>

        {/* Bottom: Settings + User */}
        <div className="space-y-1">
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              clsx(
                'flex items-center gap-4 px-4 py-3 text-sm font-medium transition-all duration-200',
                isActive
                  ? 'text-rt-primary font-bold bg-rt-primary-fixed/20 border-r-[3px] border-rt-primary-container'
                  : 'text-rt-text-muted hover:bg-rt-primary-fixed/10 hover:text-rt-text'
              )
            }
          >
            <Settings className="w-5 h-5" />
            <span>Settings</span>
          </NavLink>
          <UserMenu onOpenProfile={() => setProfileOpen(true)} />
        </div>
      </aside>
      <ProfilePanel openForSelf={profileOpen} onCloseForSelf={() => setProfileOpen(false)} />

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Top App Bar — glassmorphism */}
        <header className="h-20 flex items-center justify-between px-10 bg-rt-bg/70 backdrop-blur-md flex-shrink-0 z-30 relative">
          {/* Global Search */}
          <GlobalSearch />

          {/* Actions */}
          <div className="flex items-center gap-5">
            <button
              onClick={() => { setNotifOpen(o => !o); setProfileOpen(false) }}
              className="text-rt-text-muted hover:text-rt-primary transition-all relative"
              title="Notifications"
            >
              <Bell className="w-5 h-5" />
              <span className="absolute -top-0.5 -right-0.5 w-2 h-2 bg-rt-primary-container rounded-full border-2 border-rt-bg"></span>
            </button>
            <button
              onClick={() => { setProfileOpen(o => !o); setNotifOpen(false) }}
              className="text-rt-text-muted hover:text-rt-primary transition-all"
              title="Account"
            >
              <User className="w-5 h-5" />
            </button>
          </div>

          {/* Notification dropdown */}
          <NotificationDropdown open={notifOpen} onClose={() => setNotifOpen(false)} />
        </header>

        {/* Page Content */}
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
