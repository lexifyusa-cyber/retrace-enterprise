import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  X, User, Shield, ShieldCheck, Crown, Mail, Phone, Building2, Clock,
  Globe, Hash, Key, Lock, Unlock, LogOut, RefreshCw, Loader2,
  Activity, Eye, ChevronRight,
} from 'lucide-react'
import toast from 'react-hot-toast'
import axios from 'axios'
import { isAdmin as checkIsAdmin } from '@/utils/roles'

const api = axios.create({ baseURL: '/api/v1/auth' })
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('rt_access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

type TabId = 'overview' | 'security' | 'activity'

function timeAgo(dateStr: string | null | undefined): string {
  if (!dateStr) return 'Never'
  const diff = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'Just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}

const roleLabel = (r: string) => {
  if (r === 'zero_admin') return 'Zero Admin'
  if (r === 'user_admin') return 'User Admin'
  if (r === 'user') return 'User'
  return 'Admin'
}

const authMethodLabel = (user: any) => {
  if (user?.role === 'zero_admin') return 'Local'
  if (user?.auth_provider === 'ldap') return 'LDAP'
  if (user?.auth_provider === 'azure_ad') return 'Azure AD'
  return 'Local (Email)'
}

const RoleIcon = ({ role, className = 'w-5 h-5' }: { role: string; className?: string }) => {
  if (role === 'zero_admin') return <Crown className={`${className} text-red-400`} />
  if (role === 'admin') return <ShieldCheck className={`${className} text-purple-400`} />
  if (role === 'user_admin') return <Shield className={`${className} text-blue-400`} />
  return <User className={`${className} text-zinc-400`} />
}

interface Props {
  userId: string | null
  onClose: () => void
  currentUser: any
}

export default function UserDetailDrawer({ userId, onClose, currentUser }: Props) {
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState<TabId>('overview')

  const { data: userData } = useQuery({
    queryKey: ['auth', 'user-detail', userId],
    queryFn: async () => {
      const [usersRes] = await Promise.all([api.get('/users')])
      const users = usersRes.data?.users ?? (Array.isArray(usersRes.data) ? usersRes.data : [])
      const found = users.find((u: any) => u.user_id === userId) || null
      return { user: found, allUsers: users }
    },
    enabled: !!userId,
  })

  const { data: auditLogs = [] } = useQuery({
    queryKey: ['auth', 'user-audit', userId],
    queryFn: async () => (await api.get(`/users/${userId}/audit`, { params: { limit: 30 } })).data,
    enabled: !!userId && activeTab === 'activity',
  })

  const { data: sessions = [] } = useQuery({
    queryKey: ['auth', 'user-sessions', userId],
    queryFn: async () => (await api.get(`/users/${userId}/sessions`)).data,
    enabled: !!userId && activeTab === 'security',
  })

  const { data: tenantAuth } = useQuery({
    queryKey: ['auth', 'tenant'],
    queryFn: async () => (await api.get('/tenant/auth')).data,
    enabled: !!userId && checkIsAdmin(currentUser?.role),
  })

  const resetPasswordMutation = useMutation({
    mutationFn: async () => (await api.post(`/users/${userId}/reset-password`)).data,
    onSuccess: (data) => {
      const msg = data.email_sent ? 'Temporary password sent via email.' : 'Password reset. SMTP not configured — user will need to use forgot password.'
      toast.success(msg)
      queryClient.invalidateQueries({ queryKey: ['auth', 'users'] })
    },
    onError: (err: any) => toast.error(err?.response?.data?.detail || 'Failed to reset password'),
  })

  const forceLogoutMutation = useMutation({
    mutationFn: async () => (await api.post(`/users/${userId}/force-logout`)).data,
    onSuccess: (data) => {
      toast.success(data.message)
      queryClient.invalidateQueries({ queryKey: ['auth', 'user-sessions', userId] })
    },
    onError: (err: any) => toast.error(err?.response?.data?.detail || 'Failed'),
  })

  const revokeSessionMutation = useMutation({
    mutationFn: async (sessionId: string) => (await api.delete(`/users/${userId}/sessions/${sessionId}`)).data,
    onSuccess: () => {
      toast.success('Session revoked')
      queryClient.invalidateQueries({ queryKey: ['auth', 'user-sessions', userId] })
    },
    onError: (err: any) => toast.error(err?.response?.data?.detail || 'Failed'),
  })

  const deactivateMutation = useMutation({
    mutationFn: async () => (await api.patch(`/users/${userId}/deactivate`)).data,
    onSuccess: () => {
      toast.success('User deactivated')
      queryClient.invalidateQueries({ queryKey: ['auth', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['auth', 'user-detail', userId] })
    },
    onError: (err: any) => toast.error(err?.response?.data?.detail || 'Failed'),
  })

  const activateMutation = useMutation({
    mutationFn: async () => (await api.patch(`/users/${userId}/activate`)).data,
    onSuccess: () => {
      toast.success('User activated')
      queryClient.invalidateQueries({ queryKey: ['auth', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['auth', 'user-detail', userId] })
    },
    onError: (err: any) => toast.error(err?.response?.data?.detail || 'Failed'),
  })

  const switchAuthMutation = useMutation({
    mutationFn: async (provider: string) => (await api.patch(`/users/${userId}/auth-method`, { auth_provider: provider })).data,
    onSuccess: (data) => {
      toast.success(data.message)
      queryClient.invalidateQueries({ queryKey: ['auth', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['auth', 'user-detail', userId] })
    },
    onError: (err: any) => toast.error(err?.response?.data?.detail || 'Failed'),
  })

  const user = userData?.user
  const allUsers: any[] = userData?.allUsers || []
  const isOpen = !!userId

  const isAdminRole = checkIsAdmin(currentUser?.role)
  const isSelf = userId === currentUser?.user_id
  const canManage = isAdminRole && !isSelf

  const tabs: { id: TabId; label: string; icon: any }[] = [
    { id: 'overview', label: 'Overview', icon: Eye },
    { id: 'security', label: 'Security', icon: Key },
    { id: 'activity', label: 'Activity', icon: Activity },
  ]

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          <motion.div
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm"
            onClick={onClose}
          />
          <motion.div
            initial={{ x: '100%' }} animate={{ x: 0 }} exit={{ x: '100%' }}
            transition={{ type: 'spring', damping: 30, stiffness: 300 }}
            className="fixed right-0 top-0 bottom-0 z-50 w-full max-w-lg bg-rt-bg-light border-l border-rt-border shadow-2xl overflow-y-auto"
          >
            {/* Header */}
            <div className="sticky top-0 z-10 bg-rt-bg-light/95 backdrop-blur border-b border-rt-border px-6 py-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-full bg-rt-surface flex items-center justify-center">
                    {user ? <RoleIcon role={user.role} /> : <User className="w-5 h-5 text-rt-text-muted" />}
                  </div>
                  <div>
                    <h2 className="text-base font-semibold text-rt-text">{user?.display_name || user?.email || 'Loading...'}</h2>
                    <p className="text-xs text-rt-text-muted">{user?.email}</p>
                  </div>
                </div>
                <button onClick={onClose} className="p-2 rounded-lg hover:bg-rt-surface text-rt-text-muted hover:text-rt-text transition-colors">
                  <X className="w-5 h-5" />
                </button>
              </div>

              {/* Tabs */}
              <div className="flex gap-1 mt-4">
                {tabs.map(t => (
                  <button
                    key={t.id}
                    onClick={() => setActiveTab(t.id)}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${activeTab === t.id ? 'bg-rt-primary/10 text-rt-primary' : 'text-rt-text-muted hover:text-rt-text hover:bg-rt-surface/50'}`}
                  >
                    <t.icon className="w-3.5 h-3.5" />
                    {t.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="px-6 py-4">
              {!user ? (
                <div className="flex justify-center py-12"><Loader2 className="w-5 h-5 animate-spin text-rt-text-muted" /></div>
              ) : (
                <>
                  {/* Overview Tab */}
                  {activeTab === 'overview' && (
                    <div className="space-y-4">
                      <InfoCard title="User Information">
                        <InfoRow icon={Mail} label="Email" value={user.email} />
                        <InfoRow icon={User} label="Username" value={user.username || '—'} />
                        <InfoRow icon={Phone} label="Phone" value={user.phone || '—'} />
                        <InfoRow icon={Building2} label="Department" value={user.department || '—'} />
                        <InfoRow icon={Globe} label="Timezone" value={user.timezone || '—'} />
                        <InfoRow icon={Hash} label="Employee ID" value={user.employee_id || '—'} />
                      </InfoCard>
                      <InfoCard title="Account">
                        <InfoRow icon={Shield} label="Role" value={roleLabel(user.role)} />
                        <InfoRow icon={Key} label="Auth Method" value={authMethodLabel(user)} />
                        <InfoRow icon={Clock} label="Status" value={user.status || (user.is_active ? 'Active' : 'Disabled')} />
                        <InfoRow icon={Clock} label="Last Login" value={timeAgo(user.last_login_at)} />
                        <InfoRow icon={Globe} label="Last IP" value={user.last_login_ip || '—'} />
                        <InfoRow icon={Clock} label="Created" value={user.created_at ? new Date(user.created_at).toLocaleDateString() : '—'} />
                        {user.role === 'user_admin' && (
                          <>
                            <InfoRow icon={Hash} label="Max Products" value={user.max_products !== null && user.max_products !== undefined ? String(user.max_products) : 'Org default'} />
                            <InfoRow icon={Hash} label="Max Users (can create)" value={user.max_managed_users !== null && user.max_managed_users !== undefined ? String(user.max_managed_users) : 'Org default'} />
                          </>
                        )}
                        {user.manager_id && (
                          <InfoRow icon={ChevronRight} label="Manager" value={allUsers.find((u: any) => u.user_id === user.manager_id)?.display_name || allUsers.find((u: any) => u.user_id === user.manager_id)?.username || user.manager_id} />
                        )}
                      </InfoCard>
                      {user.force_password_change && (
                        <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/20">
                          <p className="text-xs text-amber-400 font-medium">Password change required on next login</p>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Security Tab */}
                  {activeTab === 'security' && (
                    <div className="space-y-4">
                      {canManage && user.auth_provider === 'email' && (
                        <InfoCard title="Password Management">
                          <div className="space-y-3">
                            <p className="text-xs text-rt-text-muted">Reset this user's password. A temporary password will be generated and emailed to them.</p>
                            <button
                              onClick={() => { if (confirm('Send a password reset email to this user?')) resetPasswordMutation.mutate() }}
                              disabled={resetPasswordMutation.isPending}
                              className="flex items-center gap-2 px-3 py-2 rounded-lg bg-indigo-500/10 text-indigo-400 text-sm hover:bg-indigo-500/20 transition-colors disabled:opacity-50"
                            >
                              {resetPasswordMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                              Send Password Reset
                            </button>
                          </div>
                        </InfoCard>
                      )}

                      {canManage && (
                        <InfoCard title="Authentication Method">
                          <div className="space-y-3">
                            <p className="text-xs text-rt-text-muted">
                              Current method: <strong className="text-rt-text">{authMethodLabel(user)}</strong>
                            </p>
                            {user.auth_provider === 'email' && user.role !== 'zero_admin' && (
                              <>
                                <button
                                  onClick={() => { if (confirm('Switch this user to LDAP? Their local password will be removed.')) switchAuthMutation.mutate('ldap') }}
                                  disabled={switchAuthMutation.isPending}
                                  className="text-sm px-3 py-2 rounded-lg bg-amber-500/10 text-amber-400 hover:bg-amber-500/20 transition-colors disabled:opacity-50 mr-2"
                                >Switch to LDAP</button>
                                {tenantAuth?.azure_ad_config && (
                                  <button
                                    onClick={() => { if (confirm('Switch this user to Azure AD (Microsoft)? Their local password will be removed.')) switchAuthMutation.mutate('azure_ad') }}
                                    disabled={switchAuthMutation.isPending}
                                    className="text-sm px-3 py-2 rounded-lg bg-sky-500/10 text-sky-400 hover:bg-sky-500/20 transition-colors disabled:opacity-50"
                                  >Switch to Azure AD</button>
                                )}
                              </>
                            )}
                            {user.auth_provider === 'ldap' && (
                              <>
                                <button
                                  onClick={() => { if (confirm('Switch to email authentication? A temp password will be generated.')) switchAuthMutation.mutate('email') }}
                                  disabled={switchAuthMutation.isPending}
                                  className="text-sm px-3 py-2 rounded-lg bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors disabled:opacity-50 mr-2"
                                >Switch to Email</button>
                                {tenantAuth?.azure_ad_config && (
                                  <button
                                    onClick={() => { if (confirm('Switch this user to Azure AD (Microsoft)?')) switchAuthMutation.mutate('azure_ad') }}
                                    disabled={switchAuthMutation.isPending}
                                    className="text-sm px-3 py-2 rounded-lg bg-sky-500/10 text-sky-400 hover:bg-sky-500/20 transition-colors disabled:opacity-50"
                                  >Switch to Azure AD</button>
                                )}
                              </>
                            )}
                            {user.auth_provider === 'azure_ad' && (
                              <button
                                onClick={() => { if (confirm('Switch to email authentication? A temp password will be generated.')) switchAuthMutation.mutate('email') }}
                                disabled={switchAuthMutation.isPending}
                                className="text-sm px-3 py-2 rounded-lg bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors disabled:opacity-50"
                              >Switch to Email</button>
                            )}
                          </div>
                        </InfoCard>
                      )}

                      {canManage && (
                        <InfoCard title="Account Status">
                          <div className="flex items-center gap-3">
                            {user.is_active ? (
                              <button
                                onClick={() => { if (confirm(`Deactivate ${user.display_name || user.email}?`)) deactivateMutation.mutate() }}
                                disabled={deactivateMutation.isPending}
                                className="flex items-center gap-2 px-3 py-2 rounded-lg bg-red-500/10 text-red-400 text-sm hover:bg-red-500/20 transition-colors disabled:opacity-50"
                              ><Lock className="w-4 h-4" /> Deactivate</button>
                            ) : (
                              <button
                                onClick={() => activateMutation.mutate()}
                                disabled={activateMutation.isPending}
                                className="flex items-center gap-2 px-3 py-2 rounded-lg bg-emerald-500/10 text-emerald-400 text-sm hover:bg-emerald-500/20 transition-colors disabled:opacity-50"
                              ><Unlock className="w-4 h-4" /> Activate</button>
                            )}
                          </div>
                        </InfoCard>
                      )}

                      <InfoCard title="Active Sessions">
                        {sessions.length === 0 ? (
                          <p className="text-xs text-rt-text-muted">No active sessions</p>
                        ) : (
                          <div className="space-y-2">
                            {sessions.map((s: any) => (
                              <div key={s.session_id} className="flex items-center justify-between p-2 rounded-lg bg-rt-surface/30 border border-rt-border">
                                <div>
                                  <p className="text-xs text-rt-text">{s.ip_address || 'Unknown IP'}</p>
                                  <p className="text-[10px] text-rt-text-muted truncate max-w-[200px]">{s.user_agent?.substring(0, 60) || 'Unknown'}</p>
                                  <p className="text-[10px] text-rt-text-muted">{timeAgo(s.created_at)}</p>
                                </div>
                                <button
                                  onClick={() => revokeSessionMutation.mutate(s.session_id)}
                                  className="text-xs text-red-400 hover:text-red-300 px-2 py-1 rounded hover:bg-red-500/10 transition-colors"
                                >Revoke</button>
                              </div>
                            ))}
                            {canManage && sessions.length > 0 && (
                              <button
                                onClick={() => { if (confirm('Force logout from all sessions?')) forceLogoutMutation.mutate() }}
                                disabled={forceLogoutMutation.isPending}
                                className="flex items-center gap-2 w-full justify-center py-2 rounded-lg bg-red-500/10 text-red-400 text-xs hover:bg-red-500/20 transition-colors"
                              >
                                <LogOut className="w-3.5 h-3.5" />
                                Force Logout All Sessions
                              </button>
                            )}
                          </div>
                        )}
                      </InfoCard>
                    </div>
                  )}

                  {/* Activity Tab */}
                  {activeTab === 'activity' && (
                    <div className="space-y-2">
                      {auditLogs.length === 0 ? (
                        <div className="text-center py-12">
                          <Activity className="w-8 h-8 text-rt-text-muted mx-auto mb-2" />
                          <p className="text-sm text-rt-text-muted">No activity recorded yet</p>
                        </div>
                      ) : (
                        auditLogs.map((log: any, i: number) => (
                          <div key={log.id || i} className="flex items-start gap-3 p-3 rounded-lg bg-rt-surface/20 border border-rt-border/50">
                            <div className="w-6 h-6 rounded-full bg-rt-surface flex items-center justify-center flex-shrink-0 mt-0.5">
                              <Activity className="w-3 h-3 text-rt-text-muted" />
                            </div>
                            <div className="flex-1 min-w-0">
                              <p className="text-xs text-rt-text font-medium">{(log.action || '').replace(/_/g, ' ')}</p>
                              <p className="text-[10px] text-rt-text-muted">{log.ip_address} · {log.timestamp ? new Date(log.timestamp).toLocaleString() : ''}</p>
                              {log.details && (
                                <p className="text-[10px] text-rt-text-muted mt-0.5 truncate">{JSON.stringify(log.details)}</p>
                              )}
                            </div>
                          </div>
                        ))
                      )}
                    </div>
                  )}

                </>
              )}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}

function InfoCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-rt-border bg-rt-surface/30 overflow-hidden">
      <div className="px-4 py-2.5 border-b border-rt-border bg-rt-surface/50">
        <h3 className="text-xs font-semibold text-rt-text-muted uppercase tracking-wider">{title}</h3>
      </div>
      <div className="p-4 space-y-3">{children}</div>
    </div>
  )
}

function InfoRow({ icon: Icon, label, value }: { icon: any; label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <div className="flex items-center gap-2">
        <Icon className="w-3.5 h-3.5 text-rt-text-muted" />
        <span className="text-xs text-rt-text-muted">{label}</span>
      </div>
      <span className="text-xs text-rt-text font-medium">{value}</span>
    </div>
  )
}
