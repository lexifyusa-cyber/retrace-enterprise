import { useState, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { Loader2, Lock, Eye, EyeOff, CheckCircle, AlertTriangle } from 'lucide-react'
import toast from 'react-hot-toast'
import axios from 'axios'

const api = axios.create({ baseURL: '/api/v1/auth' })

export default function ResetPassword() {
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token') || ''
  const navigate = useNavigate()

  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [success, setSuccess] = useState(false)
  const [error, setError] = useState('')

  const isInvite = window.location.pathname === '/setup' && !!token

  useEffect(() => {
    if (!token) setError('Missing or invalid reset token. Please request a new reset link.')
  }, [token])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (password !== confirmPassword) {
      toast.error('Passwords do not match')
      return
    }
    if (password.length < 8) {
      toast.error('Password must be at least 8 characters')
      return
    }
    setLoading(true)
    setError('')
    try {
      const endpoint = isInvite ? '/accept-invite' : '/reset-password'
      await api.post(endpoint, { token, new_password: password })
      setSuccess(true)
      toast.success(isInvite ? 'Account set up successfully!' : 'Password reset successfully!')
    } catch (err: any) {
      const detail = err.response?.data?.detail || 'Failed to reset password. The link may have expired.'
      setError(detail)
      toast.error(detail)
    } finally {
      setLoading(false)
    }
  }

  if (success) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-rt-bg p-4">
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} className="w-full max-w-md text-center">
          <CheckCircle className="w-16 h-16 text-emerald-400 mx-auto mb-4" />
          <h2 className="text-xl font-semibold text-rt-text mb-2">
            {isInvite ? 'Account Ready!' : 'Password Reset!'}
          </h2>
          <p className="text-sm text-rt-text-muted mb-6">
            {isInvite ? 'Your account has been set up. You can now sign in.' : 'Your password has been changed. You can now sign in with your new password.'}
          </p>
          <button onClick={() => navigate('/login')} className="btn-primary">
            Go to Sign In
          </button>
        </motion.div>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-rt-bg p-4">
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} className="w-full max-w-md">
        <div className="text-center mb-8">
          <h1 className="text-3xl font-display font-bold text-rt-text">ReTrace</h1>
          <p className="text-sm text-rt-text-muted mt-1">Enterprise Knowledge Management</p>
        </div>

        <div className="card p-6">
          <h2 className="text-lg font-semibold text-rt-text mb-1">
            {isInvite ? 'Set Up Your Account' : 'Set New Password'}
          </h2>
          <p className="text-xs text-rt-text-muted mb-6">
            {isInvite ? 'Create a password to complete your account setup.' : 'Enter your new password below.'}
          </p>

          {error && (
            <div className="flex items-center gap-2 p-3 rounded-lg bg-red-500/10 border border-red-500/20 mb-4">
              <AlertTriangle className="w-4 h-4 text-red-400 flex-shrink-0" />
              <p className="text-xs text-red-400">{error}</p>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="label">New Password</label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
                <input
                  type={showPassword ? 'text' : 'password'}
                  className="input pl-10 pr-10"
                  placeholder="••••••••"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  required
                  minLength={8}
                  autoFocus
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-rt-text-muted hover:text-rt-text"
                >
                  {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
              <p className="text-[10px] text-rt-text-muted mt-1">Min 8 characters, 1 uppercase, 1 digit</p>
            </div>
            <div>
              <label className="label">Confirm Password</label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
                <input
                  type={showPassword ? 'text' : 'password'}
                  className="input pl-10"
                  placeholder="••••••••"
                  value={confirmPassword}
                  onChange={e => setConfirmPassword(e.target.value)}
                  required
                  minLength={8}
                />
              </div>
            </div>
            <button
              type="submit"
              disabled={loading || !token}
              className="btn-primary w-full flex items-center justify-center gap-2 py-2.5"
            >
              {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Lock className="w-4 h-4" />}
              {loading ? 'Setting password...' : (isInvite ? 'Set Up Account' : 'Reset Password')}
            </button>
          </form>
        </div>
      </motion.div>
    </div>
  )
}
