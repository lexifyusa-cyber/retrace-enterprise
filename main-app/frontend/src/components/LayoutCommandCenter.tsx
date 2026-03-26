import { useState, useEffect } from 'react'
import { Outlet, NavLink, useLocation, useNavigate } from 'react-router-dom'
import RetraceLogo from '@/components/RetraceLogo'
import {
  LayoutDashboard, FolderTree, Bot, Settings, Search,
  ChevronRight, Command, ClipboardList, FileText,
  Activity, Zap, PanelLeftClose, PanelLeft, User,
} from 'lucide-react'
import { clsx } from 'clsx'
import { motion } from 'framer-motion'
import CommandPalette from './CommandPalette'
import ProfilePanel from './ProfilePanel'

const navigation = [
  { name: 'Dashboard', href: '/dashboard', icon: LayoutDashboard, shortcut: '1' },
  { name: 'Products', href: '/products', icon: FolderTree, shortcut: '2' },
  { name: 'Agent', href: '/agent', icon: Bot, shortcut: '3' },
  { name: 'Automations', href: '/sops', icon: ClipboardList, shortcut: '4' },
  { name: 'Documentation', href: '/docs', icon: FileText, shortcut: '5' },
]

const breadcrumbMap: Record<string, string> = {
  '/dashboard': 'Dashboard',
  '/products': 'Products',
  '/agent': 'Agent',
  '/sops': 'Automations',
  '/docs': 'Documentation',
  '/settings': 'Settings',
}

export default function LayoutCommandCenter() {
  const [sidebarExpanded, setSidebarExpanded] = useState(false)
  const [sidebarPinned, setSidebarPinned] = useState(false)
  const [showCommandPalette, setShowCommandPalette] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()
  const visibleNav = navigation

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault()
        setShowCommandPalette((prev) => !prev)
      }
      if (e.altKey && !e.metaKey && !e.ctrlKey) {
        const idx = parseInt(e.key, 10) - 1
        if (idx >= 0 && idx < visibleNav.length) {
          e.preventDefault()
          navigate(visibleNav[idx].href)
        }
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [navigate, visibleNav])

  const currentPage = breadcrumbMap[location.pathname] || 'Page'
  const isExpanded = sidebarExpanded || sidebarPinned

  return (
    <div className="h-screen flex flex-col bg-rt-bg overflow-hidden">
      <div className="h-[2px] w-full bg-gradient-to-r from-rt-primary-container via-rt-primary to-rt-primary-container flex-shrink-0" />

      <header className="h-12 flex-shrink-0 bg-rt-bg-light/80 backdrop-blur-xl flex items-center px-4 gap-4 z-40">
        <div className="flex items-center gap-2.5 min-w-[40px]">
          <span className="hidden sm:block"><RetraceLogo variant="sm" /></span>
          <span className="sm:hidden"><RetraceLogo variant="icon" /></span>
        </div>

        <div className="w-px h-5 bg-rt-border/50" />

        <div className="flex items-center gap-1.5 text-xs text-rt-text-muted">
          <span className="opacity-60">Home</span>
          <ChevronRight className="w-3 h-3 opacity-40" />
          <span className="text-rt-text font-medium">{currentPage}</span>
        </div>

        <div className="flex-1" />

        <button
          type="button"
          onClick={() => setShowCommandPalette(true)}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-rt-surface/50 border border-rt-border/50 hover:border-rt-text-muted/30 hover:bg-rt-surface transition-all text-xs text-rt-text-muted group"
        >
          <Search className="w-3.5 h-3.5 group-hover:text-rt-primary transition-colors" />
          <span className="hidden sm:inline">Search or jump to...</span>
          <kbd className="hidden sm:inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-rt-bg/80 border border-rt-border/50 text-[10px] font-mono">
            <Command className="w-2.5 h-2.5" />K
          </kbd>
        </button>

        <div className="flex items-center gap-1.5 text-[10px] text-rt-text-muted">
          <div className="w-1.5 h-1.5 rounded-full bg-rt-success animate-pulse" />
          <span className="hidden sm:inline">System Online</span>

        <NavLink
          to="/settings"
          className={({ isActive }) =>
            clsx(
              'p-1.5 rounded-lg transition-all',
              isActive ? 'text-rt-primary bg-rt-primary/10' : 'text-rt-text-muted hover:text-rt-text hover:bg-rt-surface'
            )
          }
        >
          <Settings className="w-4 h-4" />
        </NavLink>
        </div>
      </header>

      <div className="flex-1 flex overflow-hidden">
        <aside
          onMouseEnter={() => {
            if (!sidebarPinned) setSidebarExpanded(true)
          }}
          onMouseLeave={() => {
            if (!sidebarPinned) setSidebarExpanded(false)
          }}
          className={clsx(
            'flex-shrink-0 bg-rt-bg-light/60 backdrop-blur-xl border-r border-rt-border/40 flex flex-col transition-all duration-300 ease-out z-30 relative',
            isExpanded ? 'w-52' : 'w-14'
          )}
        >
          <div className={clsx('flex items-center px-2 h-10', isExpanded ? 'justify-end' : 'justify-center')}>
            <button
              type="button"
              onClick={() => setSidebarPinned(!sidebarPinned)}
              className="p-1 rounded text-rt-text-muted hover:text-rt-text hover:bg-rt-surface transition-colors"
              title={sidebarPinned ? 'Unpin sidebar' : 'Pin sidebar'}
            >
              {sidebarPinned ? <PanelLeftClose className="w-4 h-4" /> : <PanelLeft className="w-4 h-4" />}
            </button>
          </div>

          <nav className="flex-1 px-2 space-y-0.5">
            {visibleNav.map((item) => (
              <NavLink
                key={item.name}
                to={item.href}
                className={({ isActive }) =>
                  clsx(
                    'flex items-center gap-3 rounded-lg text-sm font-medium transition-all duration-200 relative group',
                    isExpanded ? 'px-3 py-2' : 'px-0 py-2 justify-center',
                    isActive ? 'bg-rt-primary/10 text-rt-primary' : 'text-rt-text-muted hover:text-rt-text hover:bg-rt-surface/60'
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && (
                      <motion.div
                        layoutId="sidebar-active"
                        className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 rounded-r-full bg-rt-primary"
                        transition={{ type: 'spring', stiffness: 500, damping: 30 }}
                      />
                    )}
                    <item.icon className="w-[18px] h-[18px] flex-shrink-0" />
                    {isExpanded && (
                      <motion.span initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="whitespace-nowrap text-[13px]">
                        {item.name}
                      </motion.span>
                    )}
                    {isExpanded && (
                      <kbd className="ml-auto text-[10px] font-mono text-rt-text-muted/40 opacity-0 group-hover:opacity-100 transition-opacity">
                        ⌥{item.shortcut}
                      </kbd>
                    )}
                    {!isExpanded && (
                      <div className="absolute left-full ml-2 px-2 py-1 rounded bg-rt-surface border border-rt-border text-xs font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-50 shadow-xl">
                        {item.name}
                      </div>
                    )}
                  </>
                )}
              </NavLink>
            ))}
          </nav>

            <NavLink
              to="/settings"
              className={({ isActive }) =>
                clsx(
                  'flex items-center gap-3 rounded-lg text-sm font-medium transition-all duration-200',
                  isExpanded ? 'px-3 py-2' : 'px-0 py-2 justify-center',
                  isActive
                    ? 'bg-rt-primary/10 text-rt-primary'
                    : 'text-rt-text-muted hover:text-rt-text hover:bg-rt-surface/60'
                )
              }
            >
              <Settings className="w-[18px] h-[18px] flex-shrink-0" />
              {isExpanded && <span className="text-[13px]">Settings</span>}
            </NavLink>
            <div className={isExpanded ? 'mt-2' : 'mt-2 flex justify-center'}>
            <div className={isExpanded ? '' : 'flex justify-center'}>
              <button
                type="button"
                onClick={() => setProfileOpen(true)}
                className={clsx(
                  'rounded-lg text-rt-text-muted hover:text-rt-text hover:bg-rt-surface/60 transition-colors',
                  isExpanded ? 'flex items-center gap-3 w-full px-3 py-2' : 'p-2'
                )}
                title="Open profile"
              >
                <User className="w-[18px] h-[18px] flex-shrink-0" />
                {isExpanded && <span className="text-[13px]">Profile</span>}
              </button>
            </div>
          </div>
        </aside>
        <ProfilePanel openForSelf={profileOpen} onCloseForSelf={() => setProfileOpen(false)} />

        <main className="flex-1 overflow-auto bg-rt-bg relative">
          <div
            className="absolute inset-0 pointer-events-none opacity-[0.015]"
            style={{
              backgroundImage: 'radial-gradient(circle at 1px 1px, currentColor 1px, transparent 0)',
              backgroundSize: '24px 24px',
            }}
          />
          <div className="relative z-10 h-full">
            <Outlet />
          </div>
        </main>
      </div>

      <footer className="h-6 flex-shrink-0 bg-rt-bg-light/60 backdrop-blur-xl border-t border-rt-border/30 flex items-center px-4 gap-4 text-[10px] text-rt-text-muted z-40">
        <div className="flex items-center gap-1.5">
          <Activity className="w-3 h-3 text-rt-success" />
          <span>Operational</span>
        </div>
        <div className="w-px h-3 bg-rt-border/30" />
        <div className="flex items-center gap-1.5">
          <span>AI Connected</span>
          <span>Community</span>
          <span>Community</span>
          <span>Community</span>
          <span>Community</span>
          <span>Community</span>
        </div>
        <div className="flex-1" />
        <span className="opacity-40">A product of</span>
        <span className="opacity-60 font-medium">Lumena Technologies</span>
      </footer>

      <CommandPalette isOpen={showCommandPalette} onClose={() => setShowCommandPalette(false)} />
    </div>
  )
}
