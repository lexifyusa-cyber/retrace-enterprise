import { useState } from 'react'
import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard,
  FolderTree,
  Bot,
  ClipboardList,
  FileText,
  Settings,
  ChevronRight,
  LogOut,
  User,
  Users,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { clsx } from 'clsx'
import { useAuth } from '@/context/AuthContext'
import ProfilePanel from './ProfilePanel'

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

function UserMenu({ onOpenProfile }: { onOpenProfile?: () => void }) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  if (!user) return null
  const label =
    user.role === 'zero_admin'
      ? 'Zero Admin'
      : user.role === 'user_admin'
        ? 'User Admin'
        : user.role === 'user'
          ? 'User'
          : 'Admin'
  return (
    <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-rt-surface/30">
      <button
        type="button"
        onClick={onOpenProfile}
        className="flex items-center gap-2 flex-1 min-w-0 text-left rounded-md hover:bg-rt-surface/50 transition-colors"
        title="Open profile"
      >
        <div className="w-7 h-7 rounded-full bg-rt-primary/20 flex items-center justify-center flex-shrink-0">
          <User className="w-3.5 h-3.5 text-rt-primary" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-xs font-medium truncate">{user.display_name || user.email}</p>
          <p className="text-[10px] text-rt-text-muted truncate">{label}</p>
        </div>
      </button>
      <button
        onClick={async () => { await logout(); navigate('/login') }}
        className="text-rt-text-muted hover:text-rt-accent transition-colors"
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
  minRole?: string
}

const navigation: NavItem[] = [
  { name: 'Dashboard', href: '/dashboard', icon: LayoutDashboard },
  { name: 'Products', href: '/products', icon: FolderTree },
  { name: 'Agent', href: '/agent', icon: Bot },
  { name: 'Automations', href: '/sops', icon: ClipboardList, minRole: 'admin' },
  { name: 'Documentation', href: '/docs', icon: FileText, minRole: 'admin' },
  { name: 'User Management', href: '/users', icon: Users, minRole: 'user_admin' },
]

export default function Layout() {
  const { user } = useAuth()
  const [profileOpen, setProfileOpen] = useState(false)
  const role = user?.role || 'user'
  const isAdmin = hasAccess(role, 'admin')

  return (
    <div className="flex h-screen bg-rt-bg">
      <aside className="w-64 bg-rt-bg-light border-r border-rt-border flex flex-col">
        <div className="h-16 flex items-center px-6 border-b border-rt-border">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-gradient-to-br from-indigo-500 to-purple-600 rounded-lg flex items-center justify-center shadow-lg shadow-indigo-500/25">
              <svg className="w-5 h-5 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 4v16" /><path d="M4 4h8q5 0 5 5t-5 5H4" /><path d="M11 14l7 6" />
                <path d="M18 20c2-2 2-5 0-7" strokeWidth="1.5" opacity="0.7" />
              </svg>
            </div>
            <div className="flex flex-col">
              <span className="font-display font-extrabold text-lg tracking-tight bg-gradient-to-r from-indigo-400 via-purple-400 to-violet-400 bg-clip-text text-transparent leading-tight">ReTrace</span>
              <span className="text-[9px] text-rt-text-muted font-medium -mt-0.5">by Lumena</span>
            </div>
          </div>
        </div>

        <nav className="flex-1 px-3 py-4 space-y-1">
          {navigation.filter(item => hasAccess(role, item.minRole)).map((item) => (
            <NavLink
              key={item.name}
              to={item.href}
              className={({ isActive }) =>
                clsx(
                  'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-200',
                  isActive
                    ? 'bg-rt-primary/10 text-rt-primary'
                    : 'text-rt-text-muted hover:text-rt-text hover:bg-rt-surface'
                )
              }
            >
              {({ isActive }) => (
                <>
                  <item.icon className="w-5 h-5" />
                  <span>{item.name}</span>
                  {isActive && <ChevronRight className="w-4 h-4 ml-auto" />}
                </>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="px-3 py-4 border-t border-rt-border space-y-1">
          {isAdmin && (
            <NavLink
              to="/settings"
              className={({ isActive }) =>
                clsx(
                  'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-200',
                  isActive
                    ? 'bg-rt-primary/10 text-rt-primary'
                    : 'text-rt-text-muted hover:text-rt-text hover:bg-rt-surface'
                )
              }
            >
              <Settings className="w-5 h-5" />
              <span>Settings</span>
            </NavLink>
          )}
          <UserMenu onOpenProfile={() => setProfileOpen(true)} />
        </div>
      </aside>
      <ProfilePanel openForSelf={profileOpen} onCloseForSelf={() => setProfileOpen(false)} />

      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}
