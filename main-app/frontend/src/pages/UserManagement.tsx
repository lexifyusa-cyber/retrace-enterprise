import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Users, UserPlus, Shield, ShieldCheck, User, X, Loader2, Mail,
  Crown, Search, ChevronDown, ChevronUp, ChevronLeft, ChevronRight,
  Filter, MoreHorizontal, Check, AlertCircle, Clock, Lock, RefreshCw,
  Building2, Globe, Hash,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { useAuth } from '@/context/AuthContext'
import axios from 'axios'
import UserDetailDrawer from '@/components/UserDetailDrawer'

const api = axios.create({ baseURL: '/api/v1/auth' })
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('rt_access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

function getApiErrorMessage(err: any, fallback = 'Request failed'): string {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length > 0) {
    const msgs = detail.map((d: any) => (d && typeof d.msg === 'string' ? d.msg : String(d))).filter(Boolean)
    return msgs.length ? msgs.join('. ') : fallback
  }
  return fallback
}

function timeAgo(dateStr: string | null | undefined): string {
  if (!dateStr) return 'Never'
  const diff = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'Just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 30) return `${days}d ago`
  return `${Math.floor(days / 30)}mo ago`
}

const ROLE_OPTIONS = [
  { value: 'admin', label: 'Admin', desc: 'Full access to everything', icon: ShieldCheck },
  { value: 'user_admin', label: 'User Admin', desc: 'Manage users + assigned products', icon: Shield },
  { value: 'user', label: 'User', desc: 'Access assigned products only', icon: User },
]

const roleLabel = (r: string) => {
  if (r === 'zero_admin') return 'Zero Admin'
  if (r === 'user_admin') return 'User Admin'
  if (r === 'user') return 'User'
  return 'Admin'
}

const roleBadgeClass = (r: string) => {
  if (r === 'zero_admin') return 'bg-red-500/10 text-red-400 border border-red-500/20'
  if (r === 'admin' || r === 'super_admin' || r === 'tenant_admin') return 'bg-purple-500/10 text-purple-400 border border-purple-500/20'
  if (r === 'user_admin') return 'bg-blue-500/10 text-blue-400 border border-blue-500/20'
  return 'bg-rt-surface/20 text-rt-text-muted border border-rt-border'
}

const statusConfig: Record<string, { label: string; color: string; icon: any; rowClass: string }> = {
  active: { label: 'Active', color: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20', icon: Check, rowClass: '' },
  invited: { label: 'Invited', color: 'bg-amber-500/10 text-amber-400 border-amber-500/20', icon: Clock, rowClass: 'border-l-2 border-l-amber-500/40' },
  disabled: { label: 'Disabled', color: 'bg-rt-surface/20 text-rt-text-muted border-rt-border', icon: AlertCircle, rowClass: 'opacity-50 grayscale' },
  locked: { label: 'Locked', color: 'bg-red-500/10 text-red-400 border-red-500/20', icon: Lock, rowClass: 'border-l-2 border-l-red-500/40' },
}

const RoleIcon = ({ role, className = 'w-4 h-4' }: { role: string; className?: string }) => {
  if (role === 'zero_admin') return <Crown className={`${className} text-red-400`} />
  if (role === 'admin' || role === 'super_admin' || role === 'tenant_admin') return <ShieldCheck className={`${className} text-purple-400`} />
  if (role === 'user_admin') return <Shield className={`${className} text-blue-400`} />
  return <User className={`${className} text-rt-text-muted`} />
}

export default function UserManagement() {
  const { user: currentUser } = useAuth()
  const queryClient = useQueryClient()

  const [search, setSearch] = useState('')
  const [roleFilter, setRoleFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [authFilter, setAuthFilter] = useState('')
  const [sortBy, setSortBy] = useState('created_at')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [page, setPage] = useState(1)
  const [perPage, setPerPage] = useState(25)

  const [selectedUsers, setSelectedUsers] = useState<Set<string>>(new Set())
  const [drawerUserId, setDrawerUserId] = useState<string | null>(null)
  const [showModal, setShowModal] = useState(false)
  const [showFilters, setShowFilters] = useState(false)

  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteName, setInviteName] = useState('')
  const [inviteRole, setInviteRole] = useState('user')
  const [inviteAuthProvider, setInviteAuthProvider] = useState<'email' | 'ldap' | 'azure_ad'>('email')
  const [inviteMaxProducts, setInviteMaxProducts] = useState<string>('')
  const [inviteMaxManagedUsers, setInviteMaxManagedUsers] = useState<string>('')
  const [inviteManagerId, setInviteManagerId] = useState<string>('')
  const [invitePhone, setInvitePhone] = useState('')
  const [inviteDepartment, setInviteDepartment] = useState('')
  const [inviteTimezone, setInviteTimezone] = useState('')
  const [inviteEmployeeId, setInviteEmployeeId] = useState('')

  const isAdmin = currentUser?.role === 'zero_admin' || currentUser?.role === 'admin' || currentUser?.role === 'super_admin' || currentUser?.role === 'tenant_admin'
  const isZeroAdmin = currentUser?.role === 'zero_admin'

  const { data: usersResponse, isLoading, refetch, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ['auth', 'users', search, roleFilter, statusFilter, authFilter, sortBy, sortDir, page, perPage],
    queryFn: async () => {
      const params: any = { page, per_page: perPage, sort_by: sortBy, sort_dir: sortDir }
      if (search) params.search = search
      if (roleFilter) params.role = roleFilter
      if (statusFilter) params.status_filter = statusFilter
      if (authFilter) params.auth_provider = authFilter
      return (await api.get('/users', { params })).data
    },
    refetchOnWindowFocus: true,
  })

  const users = usersResponse?.users ?? (Array.isArray(usersResponse) ? usersResponse : [])
  const totalUsers = usersResponse?.total ?? users.length
  const totalPages = usersResponse?.total_pages ?? 1

  const { data: userAdminData } = useQuery({
    queryKey: ['auth', 'user-admins-all'],
    queryFn: async () => (await api.get('/users', { params: { role: 'user_admin', per_page: 500 } })).data,
    enabled: isAdmin,
  })
  const userAdminOptions = useMemo(() => {
    const raw = userAdminData?.users ?? (Array.isArray(userAdminData) ? userAdminData : [])
    return raw.filter((u: any) => u.is_active)
  }, [userAdminData])

  const addUserMutation = useMutation({
    mutationFn: async (data: any) => {
      const payload: any = { email: data.email, display_name: data.display_name, role: data.role }
      if (data.auth_provider) payload.auth_provider = data.auth_provider
      if (data.max_products !== undefined && data.max_products !== null) payload.max_products = data.max_products
      if (data.max_managed_users !== undefined && data.max_managed_users !== null) payload.max_managed_users = data.max_managed_users
      if (data.manager_id) payload.manager_id = data.manager_id
      if (data.phone) payload.phone = data.phone
      if (data.department) payload.department = data.department
      if (data.timezone) payload.timezone = data.timezone
      if (data.employee_id) payload.employee_id = data.employee_id
      return (await api.post('/users/add', payload)).data
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['auth', 'users'] })
      const emailNote = data.email_sent ? 'Invite email sent.' : (data.email_error ? `Email failed: ${data.email_error}` : 'SMTP not configured — share the invite link manually.')
      toast.success(`User added: ${data.user.email}. ${emailNote}`)
      resetModal()
    },
    onError: (err: any) => toast.error(getApiErrorMessage(err, 'Failed to add user')),
  })

  const bulkMutation = useMutation({
    mutationFn: async (data: { action: string; user_ids: string[] }) =>
      (await api.post('/users/bulk-action', data)).data,
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['auth', 'users'] })
      toast.success(`Bulk action: ${data.success} succeeded, ${data.failed} failed`)
      setSelectedUsers(new Set())
    },
    onError: (err: any) => toast.error(getApiErrorMessage(err, 'Bulk action failed')),
  })

  const resetModal = () => {
    setShowModal(false)
    setInviteEmail('')
    setInviteName('')
    setInviteRole('user')
    setInviteAuthProvider('email')
    setInviteMaxProducts('')
    setInviteMaxManagedUsers('')
    setInviteManagerId('')
    setInvitePhone('')
    setInviteDepartment('')
    setInviteTimezone('')
    setInviteEmployeeId('')
  }

  const toggleSort = (col: string) => {
    if (sortBy === col) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortBy(col)
      setSortDir('asc')
    }
    setPage(1)
  }

  const toggleSelectAll = () => {
    if (selectedUsers.size === users.length) {
      setSelectedUsers(new Set())
    } else {
      setSelectedUsers(new Set(users.map((u: any) => u.user_id)))
    }
  }

  const toggleSelect = (uid: string) => {
    setSelectedUsers(prev => {
      const next = new Set(prev)
      if (next.has(uid)) next.delete(uid)
      else next.add(uid)
      return next
    })
  }

  const SortHeader = ({ col, label }: { col: string; label: string }) => (
    <th
      className="px-3 py-3 text-left text-xs font-medium text-rt-text-muted uppercase tracking-wider cursor-pointer select-none hover:text-rt-text transition-colors"
      onClick={() => toggleSort(col)}
    >
      <span className="flex items-center gap-1">
        {label}
        {sortBy === col && (sortDir === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />)}
      </span>
    </th>
  )

  const activeFilterCount = [roleFilter, statusFilter, authFilter].filter(Boolean).length

  const getUserStatus = (u: any) => {
    if (u.locked_until && new Date(u.locked_until) > new Date()) return 'locked'
    return u.status || (u.is_active ? 'active' : 'disabled')
  }

  return (
    <div className="p-6 max-w-[1400px] mx-auto">
      {/* Header - sticky so Refresh stays visible when scrolling */}
      <div className="sticky top-0 z-10 -mx-6 px-6 py-4 mb-6 bg-rt-bg/95 backdrop-blur-sm border-b border-rt-border/50">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-500/10 flex items-center justify-center">
              <Users className="w-5 h-5 text-indigo-400" />
            </div>
            <div>
              <h1 className="text-xl font-display font-bold text-rt-text">User Management</h1>
              <p className="text-sm text-rt-text-muted">{totalUsers} user{totalUsers !== 1 ? 's' : ''}</p>
            </div>
          </div>
          <div className="flex items-center gap-4 flex-wrap">
            <div className="flex items-center gap-3 px-3 py-2 rounded-lg bg-rt-surface/50 border border-rt-border/50">
              <button
                onClick={() => refetch().then(() => toast.success('List updated'))}
                disabled={isFetching}
                className="btn-secondary flex items-center gap-2 text-sm font-medium"
                title="Refresh user list to see latest last login times"
              >
                <RefreshCw className={`w-4 h-4 ${isFetching ? 'animate-spin' : ''}`} />
                Refresh
              </button>
              <span className="text-sm text-rt-text font-medium whitespace-nowrap">
                Last updated: {dataUpdatedAt ? timeAgo(new Date(dataUpdatedAt).toISOString()) : '—'}
              </span>
            </div>
            <button onClick={() => { setInviteAuthProvider('email'); setShowModal(true) }} className="btn-primary flex items-center gap-2 text-sm">
              <UserPlus className="w-4 h-4" /> Add User
            </button>
          </div>
        </div>
      </div>

      {/* Search & Filters bar */}
      <div className="flex items-center gap-3 mb-4">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
          <input
            type="text"
            placeholder="Search by name, email, department..."
            className="input pl-10 w-full"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(1) }}
          />
        </div>
        <button
          onClick={() => setShowFilters(!showFilters)}
          className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-sm transition-all ${showFilters ? 'border-indigo-500/50 bg-indigo-500/10 text-indigo-400' : 'border-rt-border text-rt-text-muted hover:border-rt-surface'}`}
        >
          <Filter className="w-4 h-4" />
          Filters
          {activeFilterCount > 0 && (
            <span className="w-5 h-5 rounded-full bg-indigo-500 text-white text-xs flex items-center justify-center">{activeFilterCount}</span>
          )}
        </button>
        {activeFilterCount > 0 && (
          <button onClick={() => { setRoleFilter(''); setStatusFilter(''); setAuthFilter(''); setPage(1) }} className="text-xs text-rt-text-muted hover:text-rt-text">
            Clear filters
          </button>
        )}
      </div>

      {/* Filter dropdowns */}
      <AnimatePresence>
        {showFilters && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden mb-4"
          >
            <div className="flex items-center gap-3 p-3 rounded-lg border border-rt-border bg-rt-bg-lighter">
              <div>
                <label className="text-xs text-rt-text-muted block mb-1">Role</label>
                <select className="input text-sm py-1.5" value={roleFilter} onChange={(e) => { setRoleFilter(e.target.value); setPage(1) }}>
                  <option value="">All Roles</option>
                  <option value="zero_admin">Zero Admin</option>
                  <option value="admin">Admin</option>
                  <option value="user_admin">User Admin</option>
                  <option value="user">User</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-rt-text-muted block mb-1">Status</label>
                <select className="input text-sm py-1.5" value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1) }}>
                  <option value="">All Statuses</option>
                  <option value="active">Active</option>
                  <option value="invited">Invited</option>
                  <option value="disabled">Disabled</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-rt-text-muted block mb-1">Auth Method</label>
                <select className="input text-sm py-1.5" value={authFilter} onChange={(e) => { setAuthFilter(e.target.value); setPage(1) }}>
                  <option value="">All Methods</option>
                  <option value="email">Email</option>
                  <option value="ldap">LDAP</option>
                  <option value="azure_ad">Azure AD</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-rt-text-muted block mb-1">Per Page</label>
                <select className="input text-sm py-1.5" value={perPage} onChange={(e) => { setPerPage(Number(e.target.value)); setPage(1) }}>
                  <option value={25}>25</option>
                  <option value={50}>50</option>
                  <option value={100}>100</option>
                </select>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Bulk actions bar */}
      <AnimatePresence>
        {selectedUsers.size > 0 && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden mb-3"
          >
            <div className="flex items-center gap-3 p-3 rounded-lg bg-indigo-500/10 border border-indigo-500/20">
              <span className="text-sm text-indigo-300 font-medium">{selectedUsers.size} selected</span>
              <div className="flex items-center gap-2 ml-auto">
                <button
                  onClick={() => { if (confirm(`Deactivate ${selectedUsers.size} user(s)?`)) bulkMutation.mutate({ action: 'deactivate', user_ids: [...selectedUsers] }) }}
                  className="px-3 py-1.5 rounded-md bg-rt-surface/50 text-rt-text text-xs hover:bg-rt-surface transition-colors"
                >Deactivate</button>
                <button
                  onClick={() => bulkMutation.mutate({ action: 'activate', user_ids: [...selectedUsers] })}
                  className="px-3 py-1.5 rounded-md bg-emerald-500/20 text-emerald-400 text-xs hover:bg-emerald-500/30 transition-colors"
                >Activate</button>
                <button
                  onClick={() => bulkMutation.mutate({ action: 'resend_invite', user_ids: [...selectedUsers] })}
                  className="px-3 py-1.5 rounded-md bg-amber-500/20 text-amber-400 text-xs hover:bg-amber-500/30 transition-colors"
                >
                  <RefreshCw className="w-3 h-3 inline mr-1" />Resend Invites
                </button>
                <button
                  onClick={() => { if (confirm(`Permanently delete ${selectedUsers.size} user(s)? This cannot be undone.`)) bulkMutation.mutate({ action: 'delete', user_ids: [...selectedUsers] }) }}
                  className="px-3 py-1.5 rounded-md bg-red-500/20 text-red-400 text-xs hover:bg-red-500/30 transition-colors"
                >Delete</button>
                <button onClick={() => setSelectedUsers(new Set())} className="text-xs text-rt-text-muted hover:text-rt-text ml-2">Cancel</button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* User Table */}
      {isLoading ? (
        <div className="flex justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-rt-text-muted" /></div>
      ) : users.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 rounded-full bg-rt-surface flex items-center justify-center mb-4">
            <Users className="w-8 h-8 text-rt-text-muted/60" />
          </div>
          <h3 className="text-lg font-medium text-rt-text mb-1">No users found</h3>
          <p className="text-sm text-rt-text-muted max-w-sm">
            {search || activeFilterCount > 0 ? 'Try adjusting your search or filters.' : 'Get started by adding your first user.'}
          </p>
        </div>
      ) : (
        <div className="rounded-xl border border-rt-border overflow-x-auto overflow-y-visible bg-rt-bg-light">
          <table className="w-full min-w-[900px]">
            <thead className="bg-rt-bg-lighter border-b border-rt-border">
              <tr>
                <th className="w-10 px-3 py-3">
                  <input
                    type="checkbox"
                    className="rounded border-rt-border"
                    checked={selectedUsers.size === users.length && users.length > 0}
                    onChange={toggleSelectAll}
                  />
                </th>
                <SortHeader col="display_name" label="User" />
                <SortHeader col="role" label="Role" />
                <th className="px-3 py-3 text-left text-xs font-medium text-rt-text-muted uppercase tracking-wider">Auth</th>
                <SortHeader col="status" label="Status" />
                <SortHeader col="last_login_at" label="Last Login" />
                <th className="px-3 py-3 text-left text-xs font-medium text-rt-text-muted uppercase tracking-wider">Department</th>
                <th className="w-10 px-3 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-rt-border/50">
              {users.map((u: any) => {
                const st = getUserStatus(u)
                const cfg = statusConfig[st] || statusConfig.active
                return (
                  <tr
                    key={u.user_id}
                    className={`hover:bg-rt-surface/30 transition-colors cursor-pointer ${cfg.rowClass}`}
                    onClick={() => setDrawerUserId(u.user_id)}
                  >
                    <td className="px-3 py-3" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        className="rounded border-rt-border"
                        checked={selectedUsers.has(u.user_id)}
                        onChange={() => toggleSelect(u.user_id)}
                      />
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex items-center gap-3">
                        <div className="w-8 h-8 rounded-full bg-rt-surface/50 flex items-center justify-center flex-shrink-0">
                          <RoleIcon role={u.role} className="w-4 h-4" />
                        </div>
                        <div className="min-w-0">
                          <p className={`text-sm font-medium truncate ${st === 'disabled' ? 'text-rt-text-muted line-through' : 'text-rt-text'}`}>
                            {u.display_name || u.email}
                            {u.user_id === currentUser?.user_id && <span className="text-[10px] text-rt-text-muted ml-1.5">(you)</span>}
                          </p>
                          <p className={`text-xs truncate ${st === 'disabled' ? 'text-rt-text-muted/60 line-through' : 'text-rt-text-muted'}`}>{u.email}</p>
                        </div>
                      </div>
                    </td>
                    <td className="px-3 py-3">
                      <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${roleBadgeClass(u.role)}`}>
                        {roleLabel(u.role)}
                      </span>
                    </td>
                    <td className="px-3 py-3">
                      <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${u.role === 'zero_admin' ? 'bg-zinc-500/10 text-zinc-400' : u.auth_provider === 'ldap' ? 'bg-amber-500/10 text-amber-400' : u.auth_provider === 'azure_ad' ? 'bg-sky-500/10 text-sky-400' : 'bg-emerald-500/10 text-emerald-400'}`}>
                        {u.role === 'zero_admin' ? 'Local' : (u.auth_provider === 'ldap' ? 'LDAP' : u.auth_provider === 'azure_ad' ? 'Azure AD' : 'Email')}
                      </span>
                    </td>
                    <td className="px-3 py-3">
                      <span className={`inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full font-medium border ${cfg.color}`}>
                        <cfg.icon className="w-3 h-3" />
                        {cfg.label}
                      </span>
                    </td>
                    <td className="px-3 py-3">
                      <span className="text-xs text-rt-text-muted">{timeAgo(u.last_login_at)}</span>
                    </td>
                    <td className="px-3 py-3">
                      <span className="text-xs text-rt-text-muted">{u.department || '—'}</span>
                    </td>
                    <td className="px-3 py-3" onClick={(e) => e.stopPropagation()}>
                      <button
                        className="p-1 rounded hover:bg-rt-surface text-rt-text-muted hover:text-rt-text transition-colors"
                        onClick={() => setDrawerUserId(u.user_id)}
                      >
                        <MoreHorizontal className="w-4 h-4" />
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-4 py-3 border-t border-rt-border bg-rt-bg-light">
              <p className="text-xs text-rt-text-muted">
                Showing {((page - 1) * perPage) + 1}–{Math.min(page * perPage, totalUsers)} of {totalUsers}
              </p>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="p-1.5 rounded hover:bg-rt-surface text-rt-text-muted disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
                {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
                  let p: number
                  if (totalPages <= 5) p = i + 1
                  else if (page <= 3) p = i + 1
                  else if (page >= totalPages - 2) p = totalPages - 4 + i
                  else p = page - 2 + i
                  return (
                    <button
                      key={p}
                      onClick={() => setPage(p)}
                      className={`w-8 h-8 rounded text-xs font-medium transition-colors ${page === p ? 'bg-indigo-500 text-white' : 'text-rt-text-muted hover:bg-rt-surface'}`}
                    >{p}</button>
                  )
                })}
                <button
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="p-1.5 rounded hover:bg-rt-surface text-rt-text-muted disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* User Detail Drawer */}
      <UserDetailDrawer
        userId={drawerUserId}
        onClose={() => setDrawerUserId(null)}
        currentUser={currentUser}
      />

      {/* Add User Drawer */}
      <AnimatePresence>
        {showModal && (
          <>
            <motion.div
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
              className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm"
              onClick={() => resetModal()}
            />
            <motion.div
              initial={{ x: '100%' }} animate={{ x: 0 }} exit={{ x: '100%' }}
              transition={{ type: 'spring', damping: 30, stiffness: 300 }}
              className="fixed right-0 top-0 bottom-0 z-50 w-full max-w-2xl bg-rt-bg-light border-l border-rt-border shadow-2xl flex flex-col"
            >
              {/* Sticky header */}
              <div className="flex-shrink-0 bg-rt-bg-light/95 backdrop-blur border-b border-rt-border px-6 py-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-full bg-indigo-500/10 flex items-center justify-center">
                      <UserPlus className="w-5 h-5 text-indigo-400" />
                    </div>
                    <div>
                      <h2 className="text-base font-semibold text-rt-text">Add New User</h2>
                      <p className="text-xs text-rt-text-muted">Fill in the details below to create an account and send an invite</p>
                    </div>
                  </div>
                  <button onClick={resetModal} className="p-2 rounded-lg hover:bg-rt-surface text-rt-text-muted hover:text-rt-text transition-colors">
                    <X className="w-5 h-5" />
                  </button>
                </div>
              </div>

              {/* Scrollable form body */}
              <form
                id="add-user-form"
                onSubmit={(e) => {
                  e.preventDefault()
                  if (!inviteEmail) return
                  if (inviteRole !== 'zero_admin' && !inviteEmail.trim().includes('@')) {
                    toast.error('A valid email address is required for this role')
                    return
                  }
                  if (isAdmin && inviteRole === 'user' && !inviteManagerId) {
                    toast.error('Select a User Admin (LOB owner)')
                    return
                  }
                  const maxProductsVal = inviteMaxProducts.trim() ? parseInt(inviteMaxProducts, 10) : undefined
                  const maxManagedUsersVal = inviteMaxManagedUsers.trim() ? parseInt(inviteMaxManagedUsers, 10) : undefined
                  addUserMutation.mutate({
                    email: inviteEmail, display_name: inviteName, role: inviteRole,
                    auth_provider: inviteAuthProvider,
                    max_products: maxProductsVal,
                    max_managed_users: maxManagedUsersVal,
                    manager_id: isAdmin && inviteRole === 'user' ? inviteManagerId : undefined,
                    phone: invitePhone || undefined,
                    department: inviteDepartment || undefined,
                    timezone: inviteTimezone || undefined,
                    employee_id: inviteEmployeeId || undefined,
                  })
                }}
                className="flex-1 overflow-y-auto px-6 py-5 space-y-5"
              >
                {/* IDENTITY section */}
                <div className="rounded-lg border border-rt-border bg-rt-surface/10 overflow-hidden">
                  <div className="px-4 py-2.5 border-b border-rt-border bg-rt-surface/20 flex items-center gap-2">
                    <User className="w-3.5 h-3.5 text-rt-text-muted" />
                    <h3 className="text-xs font-semibold text-rt-text-muted uppercase tracking-wider">Identity</h3>
                  </div>
                  <div className="p-4 space-y-4">
                    <div>
                      <label className="text-xs font-medium text-rt-text-muted mb-1.5 block">
                        Email <span className="text-red-400">*</span>
                      </label>
                      <div className="relative">
                        <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted/60" />
                        <input type="text" className="input pl-10 w-full" placeholder="user@company.com" value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)} required />
                      </div>
                    </div>
                    <div>
                      <label className="text-xs font-medium text-rt-text-muted mb-1.5 block">
                        Display Name <span className="text-rt-text-muted/60 font-normal">(optional)</span>
                      </label>
                      <input type="text" className="input w-full" placeholder="John Doe" value={inviteName} onChange={(e) => setInviteName(e.target.value)} />
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div>
                        <label className="text-xs font-medium text-rt-text-muted mb-1.5 block">
                          Phone <span className="text-rt-text-muted/60 font-normal">(optional)</span>
                        </label>
                        <input type="text" className="input w-full" placeholder="+1 (555) 000-0000" value={invitePhone} onChange={(e) => setInvitePhone(e.target.value)} />
                      </div>
                      <div>
                        <label className="text-xs font-medium text-rt-text-muted mb-1.5 block">
                          Employee ID <span className="text-rt-text-muted/60 font-normal">(optional)</span>
                        </label>
                        <div className="relative">
                          <Hash className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted/60" />
                          <input type="text" className="input pl-10 w-full" placeholder="EMP-001" value={inviteEmployeeId} onChange={(e) => setInviteEmployeeId(e.target.value)} />
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                {/* ORGANIZATION section */}
                <div className="rounded-lg border border-rt-border bg-rt-surface/10 overflow-hidden">
                  <div className="px-4 py-2.5 border-b border-rt-border bg-rt-surface/20 flex items-center gap-2">
                    <Building2 className="w-3.5 h-3.5 text-rt-text-muted" />
                    <h3 className="text-xs font-semibold text-rt-text-muted uppercase tracking-wider">Organization</h3>
                  </div>
                  <div className="p-4 space-y-4">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div>
                        <label className="text-xs font-medium text-rt-text-muted mb-1.5 block">
                          Department <span className="text-rt-text-muted/60 font-normal">(optional)</span>
                        </label>
                        <div className="relative">
                          <Building2 className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted/60" />
                          <input type="text" className="input pl-10 w-full" placeholder="Engineering, Finance, HR..." value={inviteDepartment} onChange={(e) => setInviteDepartment(e.target.value)} />
                        </div>
                      </div>
                      <div>
                        <label className="text-xs font-medium text-rt-text-muted mb-1.5 block">
                          Timezone <span className="text-rt-text-muted/60 font-normal">(optional)</span>
                        </label>
                        <div className="relative">
                          <Globe className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted/60" />
                          <input type="text" className="input pl-10 w-full" placeholder="America/New_York" value={inviteTimezone} onChange={(e) => setInviteTimezone(e.target.value)} />
                        </div>
                      </div>
                    </div>
                    {isAdmin && inviteRole === 'user' && (
                      <div>
                        <label className="text-xs font-medium text-rt-text-muted mb-1.5 block">
                          Assigned User Admin (LOB) <span className="text-red-400">*</span>
                        </label>
                        <select className="input w-full" value={inviteManagerId} onChange={(e) => setInviteManagerId(e.target.value)} required>
                          <option value="">Select User Admin</option>
                          {userAdminOptions.map((ua: any) => (
                            <option key={ua.user_id} value={ua.user_id}>{ua.display_name || ua.email}</option>
                          ))}
                        </select>
                        <p className="text-[10px] text-rt-text-muted/60 mt-1.5">This user will be managed by the selected User Admin</p>
                      </div>
                    )}
                  </div>
                </div>

                {/* ACCESS & ROLE section */}
                <div className="rounded-lg border border-rt-border bg-rt-surface/10 overflow-hidden">
                  <div className="px-4 py-2.5 border-b border-rt-border bg-rt-surface/20 flex items-center gap-2">
                    <Shield className="w-3.5 h-3.5 text-rt-text-muted" />
                    <h3 className="text-xs font-semibold text-rt-text-muted uppercase tracking-wider">Access & Role</h3>
                  </div>
                  <div className="p-4 space-y-4">
                    <div>
                      <label className="text-xs font-medium text-rt-text-muted mb-2 block">
                        Role <span className="text-red-400">*</span>
                      </label>
                      <div className="grid grid-cols-3 gap-2">
                        {ROLE_OPTIONS.filter(r => isAdmin || r.value !== 'admin').map((r) => (
                          <label
                            key={r.value}
                            className={`flex flex-col items-center gap-1.5 p-3 rounded-lg border cursor-pointer transition-all text-center ${
                              inviteRole === r.value
                                ? 'border-indigo-500/50 bg-indigo-500/10'
                                : 'border-rt-border hover:border-rt-border/80 bg-rt-surface/20'
                            }`}
                          >
                            <input type="radio" name="role" value={r.value} checked={inviteRole === r.value} onChange={() => setInviteRole(r.value)} className="sr-only" />
                            <r.icon className={`w-5 h-5 ${inviteRole === r.value ? 'text-indigo-400' : 'text-rt-text-muted'}`} />
                            <span className={`text-xs font-medium ${inviteRole === r.value ? 'text-indigo-300' : 'text-rt-text'}`}>{r.label}</span>
                            <span className="text-[10px] text-rt-text-muted/60 leading-tight">{r.desc}</span>
                          </label>
                        ))}
                      </div>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div>
                        <label className="text-xs font-medium text-rt-text-muted mb-1.5 block">Authentication Method</label>
                        <select className="input w-full" value={inviteAuthProvider} onChange={(e) => setInviteAuthProvider(e.target.value as 'email' | 'ldap' | 'azure_ad')}>
                          <option value="email">Email / Password</option>
                          <option value="ldap">LDAP</option>
                          <option value="azure_ad">Azure AD (Microsoft)</option>
                        </select>
                      </div>
                      {inviteRole === 'user_admin' && isAdmin && (
                        <>
                          <div>
                            <label className="text-xs font-medium text-rt-text-muted mb-1.5 block">
                              Max Products <span className="text-rt-text-muted/60 font-normal">(optional)</span>
                            </label>
                            <input type="number" min={0} max={1000} placeholder="Use org default" className="input w-full"
                              value={inviteMaxProducts} onChange={(e) => setInviteMaxProducts(e.target.value)} />
                          </div>
                          <div>
                            <label className="text-xs font-medium text-rt-text-muted mb-1.5 block">
                              Max Users (can create) <span className="text-rt-text-muted/60 font-normal">(optional)</span>
                            </label>
                            <input type="number" min={0} max={10000} placeholder="Use org default" className="input w-full"
                              value={inviteMaxManagedUsers} onChange={(e) => setInviteMaxManagedUsers(e.target.value)} />
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              </form>

              {/* Sticky footer */}
              <div className="flex-shrink-0 border-t border-rt-border bg-rt-bg-light/95 backdrop-blur px-6 py-4 space-y-3">
                <div className="flex items-start gap-2.5 p-3 rounded-lg bg-indigo-500/5 border border-indigo-500/10">
                  <Mail className="w-4 h-4 text-indigo-400 mt-0.5 flex-shrink-0" />
                  <p className="text-xs text-rt-text-muted leading-relaxed">
                    {inviteAuthProvider === 'ldap'
                      ? 'LDAP users authenticate via your organization\'s directory. No local password is needed.'
                      : inviteAuthProvider === 'azure_ad'
                      ? 'User will sign in with their Microsoft account. No password is set by the admin.'
                      : 'A secure invite link will be emailed to the user so they can set their own password. No password is exposed to admins.'}
                  </p>
                </div>
                <button
                  type="submit"
                  form="add-user-form"
                  disabled={addUserMutation.isPending}
                  className="btn-primary w-full flex items-center justify-center gap-2"
                >
                  {addUserMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <UserPlus className="w-4 h-4" />}
                  Add User
                </button>
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  )
}
