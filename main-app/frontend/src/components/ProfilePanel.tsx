import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import {
  User, Lock, X, Loader2, Mail, Globe,
  Shield, ShieldCheck, Crown, Key, Clock,
} from 'lucide-react'
import toast from 'react-hot-toast'
import axios from 'axios'
import { useAuth } from '@/context/AuthContext'

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
  return `${days}d ago`
}

const roleLabel = (r: string) => {
  if (r === 'zero_admin') return 'Zero Admin'
  if (r === 'user_admin') return 'User Admin'
  if (r === 'user') return 'User'
  return 'Admin'
}

const RoleIcon = ({ role, className = 'w-5 h-5' }: { role: string; className?: string }) => {
  if (role === 'zero_admin') return <Crown className={`${className} text-red-400`} />
  if (role === 'admin') return <ShieldCheck className={`${className} text-purple-400`} />
  if (role === 'user_admin') return <Shield className={`${className} text-blue-400`} />
  return <User className={`${className} text-zinc-400`} />
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

function InfoRow({ icon: Icon, label, value }: { icon: React.ElementType; label: string; value: string }) {
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

interface ProfilePanelProps {
  /** When true, panel opens for "My Profile"; parent controls visibility (e.g. User icon click). */
  openForSelf?: boolean
  /** Called when panel is closed and it was opened via openForSelf. */
  onCloseForSelf?: () => void
}

export default function ProfilePanel({ openForSelf, onCloseForSelf }: ProfilePanelProps = {}) {
  const { changePassword } = useAuth()
  const queryClient = useQueryClient()
  const [selfDisplayName, setSelfDisplayName] = useState('')
  const [selfPhone, setSelfPhone] = useState('')
  const [selfDepartment, setSelfDepartment] = useState('')
  const [selfTimezone, setSelfTimezone] = useState('')
  const [selfCurrentPassword, setSelfCurrentPassword] = useState('')
  const [selfNewPassword, setSelfNewPassword] = useState('')

  const open = !!openForSelf

  const meQuery = useQuery({
    queryKey: ['auth', 'me'],
    queryFn: async () => (await api.get('/me')).data,
    enabled: open,
  })

  const closePanel = () => {
    setSelfCurrentPassword('')
    setSelfNewPassword('')
    onCloseForSelf?.()
  }

  const saveSelfProfileMutation = useMutation({
    mutationFn: async () =>
      (await api.patch('/me/profile', {
        display_name: selfDisplayName,
        phone: selfPhone,
        department: selfDepartment,
        timezone: selfTimezone,
      })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['auth', 'me'] })
      toast.success('Your profile is updated')
    },
    onError: (err: any) => toast.error(getApiErrorMessage(err, 'Failed to update profile')),
  })

  const selfChangePasswordMutation = useMutation({
    mutationFn: async () => {
      await changePassword(selfNewPassword, undefined, selfCurrentPassword)
    },
    onSuccess: () => {
      toast.success('Your password is updated')
      setSelfCurrentPassword('')
      setSelfNewPassword('')
    },
    onError: (err: any) => toast.error(getApiErrorMessage(err, 'Failed to change password')),
  })

  const hydrateSelf = () => {
    const me = meQuery.data?.user
    if (!me) return
    setSelfDisplayName(me.display_name || '')
    setSelfPhone(me.phone || '')
    setSelfDepartment(me.department || '')
    setSelfTimezone(me.timezone || '')
  }

  useEffect(() => {
    if (openForSelf && meQuery.data) {
      hydrateSelf()
    }
  }, [openForSelf, meQuery.data])

  const user = meQuery.data?.user
  const isLocalAuth = user?.auth_provider === 'email'

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm"
            onClick={closePanel}
          />
          <motion.div
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
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
                    <h2 className="text-base font-semibold text-rt-text">
                      {user?.display_name || user?.email || 'My Profile'}
                    </h2>
                    <p className="text-xs text-rt-text-muted">{user?.email || '—'}</p>
                  </div>
                </div>
                <button
                  onClick={closePanel}
                  className="p-2 rounded-lg hover:bg-rt-surface text-rt-text-muted hover:text-rt-text transition-colors"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
            </div>

            <div className="px-6 py-4">
              {!user ? (
                <div className="flex justify-center py-12">
                  <Loader2 className="w-5 h-5 animate-spin text-rt-text-muted" />
                </div>
              ) : (
                <div className="space-y-4">
                  {/* Account Information */}
                  <InfoCard title="Account Information">
                    <InfoRow icon={Mail} label="Email" value={user.email || '—'} />
                    <InfoRow icon={User} label="Username" value={user.username || '—'} />
                    <InfoRow icon={Shield} label="Role" value={roleLabel(user.role)} />
                    <InfoRow
                      icon={Key}
                      label="Auth Method"
                      value={user.role === 'zero_admin' ? 'Local' : (user.auth_provider === 'ldap' ? 'LDAP' : 'Local (Email)')}
                    />
                    <InfoRow
                      icon={Clock}
                      label="Status"
                      value={user.status || (user.is_active ? 'Active' : 'Disabled')}
                    />
                    <InfoRow icon={Clock} label="Last Login" value={timeAgo(user.last_login_at)} />
                    <InfoRow icon={Globe} label="Last IP" value={user.last_login_ip || '—'} />
                    <InfoRow
                      icon={Clock}
                      label="Member Since"
                      value={user.created_at ? new Date(user.created_at).toLocaleDateString() : '—'}
                    />
                    {user.mfa_enabled && (
                      <InfoRow icon={Lock} label="MFA" value="Enabled" />
                    )}
                  </InfoCard>

                  {user.force_password_change && (
                    <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/20">
                      <p className="text-xs text-amber-400 font-medium">Password change required on next login</p>
                    </div>
                  )}

                  {/* Personal Details */}
                  <InfoCard title="Personal Details">
                    <div className="space-y-3">
                      <div>
                        <label className="text-xs text-rt-text-muted block mb-1">Display Name</label>
                        <input
                          className="input w-full"
                          value={selfDisplayName}
                          onChange={(e) => setSelfDisplayName(e.target.value)}
                        />
                      </div>
                      <div>
                        <label className="text-xs text-rt-text-muted block mb-1">Phone</label>
                        <input
                          className="input w-full"
                          value={selfPhone}
                          onChange={(e) => setSelfPhone(e.target.value)}
                        />
                      </div>
                      <div>
                        <label className="text-xs text-rt-text-muted block mb-1">Department</label>
                        <input
                          className="input w-full"
                          value={selfDepartment}
                          onChange={(e) => setSelfDepartment(e.target.value)}
                        />
                      </div>
                      <div>
                        <label className="text-xs text-rt-text-muted block mb-1">Timezone</label>
                        <input
                          className="input w-full"
                          value={selfTimezone}
                          onChange={(e) => setSelfTimezone(e.target.value)}
                        />
                      </div>
                      <button
                        onClick={() => saveSelfProfileMutation.mutate()}
                        disabled={saveSelfProfileMutation.isPending}
                        className="flex items-center gap-2 px-3 py-2 rounded-lg bg-indigo-500/10 text-indigo-400 text-sm hover:bg-indigo-500/20 transition-colors disabled:opacity-50"
                      >
                        {saveSelfProfileMutation.isPending ? (
                          <Loader2 className="w-4 h-4 animate-spin" />
                        ) : null}
                        {saveSelfProfileMutation.isPending ? 'Saving...' : 'Save Changes'}
                      </button>
                    </div>
                  </InfoCard>

                  {/* Security */}
                  <InfoCard title="Security">
                    {isLocalAuth ? (
                      <div className="space-y-3">
                        {user.password_changed_at && (
                          <InfoRow
                            icon={Clock}
                            label="Password last changed"
                            value={new Date(user.password_changed_at).toLocaleDateString()}
                          />
                        )}
                        <p className="text-xs text-rt-text-muted">
                          Use a strong password with at least 8 characters, including uppercase, lowercase, and number.
                        </p>
                        <div>
                          <label className="text-xs text-rt-text-muted block mb-1">Current password</label>
                          <input
                            className="input w-full"
                            type="password"
                            placeholder="Current password"
                            value={selfCurrentPassword}
                            onChange={(e) => setSelfCurrentPassword(e.target.value)}
                          />
                        </div>
                        <div>
                          <label className="text-xs text-rt-text-muted block mb-1">New password</label>
                          <input
                            className="input w-full"
                            type="password"
                            placeholder="New password"
                            value={selfNewPassword}
                            onChange={(e) => setSelfNewPassword(e.target.value)}
                          />
                        </div>
                        <button
                          onClick={() => selfChangePasswordMutation.mutate()}
                          disabled={selfChangePasswordMutation.isPending || !selfNewPassword}
                          className="flex items-center gap-2 px-3 py-2 rounded-lg bg-indigo-500/10 text-indigo-400 text-sm hover:bg-indigo-500/20 transition-colors disabled:opacity-50"
                        >
                          {selfChangePasswordMutation.isPending ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                          ) : (
                            <Lock className="w-4 h-4" />
                          )}
                          {selfChangePasswordMutation.isPending ? 'Updating...' : 'Update Password'}
                        </button>
                      </div>
                    ) : (
                      <div className="p-3 rounded-lg bg-rt-surface/50 border border-rt-border">
                        <p className="text-xs text-rt-text-muted">
                          Password is managed by your organization's LDAP/Active Directory. Contact your organization administrator to change your password.
                        </p>
                      </div>
                    )}
                  </InfoCard>
                </div>
              )}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}
