import { useState } from 'react'
import { useNavigate, Navigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { Loader2, ShieldCheck, Mail, Lock, Eye, EyeOff, User } from 'lucide-react'
import toast from 'react-hot-toast'
import { useAuth } from '@/context/AuthContext'

export default function FirstLoginSetup() {
  const { user, isAuthenticated, changePassword } = useAuth()
  const navigate = useNavigate()
  const [newEmail, setNewEmail] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)

  if (!isAuthenticated) return <Navigate to="/login" replace />
  if (!user?.force_password_change) return <Navigate to="/dashboard" replace />

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newPassword) return
    if (newPassword !== confirmPassword) {
      toast.error('Passwords do not match')
      return
    }
    if (newPassword.length < 8) {
      toast.error('Password must be at least 8 characters')
      return
    }
    setLoading(true)
    try {
      await changePassword(newPassword, newEmail || undefined)
      toast.success('Account updated! Welcome to ReTrace.')
      navigate('/dashboard')
    } catch (err: any) {
      toast.error(err.response?.data?.detail || 'Failed to update account')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-rt-bg p-4">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="w-full max-w-md"
      >
        <div className="text-center mb-8">
          <div className="w-14 h-14 rounded-full bg-amber-500/10 flex items-center justify-center mx-auto mb-4">
            <ShieldCheck className="w-7 h-7 text-amber-400" />
          </div>
          <h1 className="text-2xl font-display font-bold text-rt-text">First-Time Setup</h1>
          <p className="text-sm text-rt-text-muted mt-1">
            Please change the default administrator credentials before continuing.
          </p>
        </div>

        <div className="card p-6">
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="label">New Email / Username</label>
              <div className="relative">
                <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
                <input
                  type="text"
                  className="input pl-10"
                  placeholder={user.email}
                  value={newEmail}
                  onChange={e => setNewEmail(e.target.value)}
                  autoComplete="email"
                />
              </div>
              <p className="text-[10px] text-rt-text-muted mt-1">
                Leave blank to keep "{user.email}" as your login
              </p>
            </div>

            <div>
              <label className="label">New Password <span className="text-rt-accent">*</span></label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
                <input
                  type={showPassword ? 'text' : 'password'}
                  className="input pl-10 pr-10"
                  placeholder="New secure password"
                  value={newPassword}
                  onChange={e => setNewPassword(e.target.value)}
                  required
                  autoComplete="new-password"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-rt-text-muted hover:text-rt-text"
                >
                  {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
              <p className="text-[10px] text-rt-text-muted mt-1">
                Min 8 characters, 1 uppercase, 1 digit
              </p>
            </div>

            <div>
              <label className="label">Confirm Password <span className="text-rt-accent">*</span></label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
                <input
                  type={showPassword ? 'text' : 'password'}
                  className="input pl-10"
                  placeholder="Confirm new password"
                  value={confirmPassword}
                  onChange={e => setConfirmPassword(e.target.value)}
                  required
                  autoComplete="new-password"
                />
              </div>
              {newPassword && confirmPassword && newPassword !== confirmPassword && (
                <p className="text-[10px] text-rt-accent mt-1">Passwords do not match</p>
              )}
            </div>

            <button
              type="submit"
              disabled={loading || !newPassword || newPassword !== confirmPassword}
              className="btn-primary w-full flex items-center justify-center gap-2 py-2.5 mt-2"
            >
              {loading ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <ShieldCheck className="w-4 h-4" />
              )}
              {loading ? 'Updating...' : 'Set Credentials & Continue'}
            </button>
          </form>
        </div>
      </motion.div>
    </div>
  )
}
