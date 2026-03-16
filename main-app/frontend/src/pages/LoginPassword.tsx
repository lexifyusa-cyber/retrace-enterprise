import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { Loader2, LogIn, Lock, Eye, EyeOff, ArrowLeft, Mail, CheckCircle } from 'lucide-react'
import toast from 'react-hot-toast'
import { useAuth } from '@/context/AuthContext'
import axios from 'axios'

const api = axios.create({ baseURL: '/api/v1/auth' })

export default function LoginPassword() {
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [showForgot, setShowForgot] = useState(false)
  const [forgotEmail, setForgotEmail] = useState('')
  const [forgotLoading, setForgotLoading] = useState(false)
  const [forgotSent, setForgotSent] = useState(false)
  const { login } = useAuth()
  const navigate = useNavigate()

  const identifier = sessionStorage.getItem('login_identifier') || ''
  const authMethod = sessionStorage.getItem('login_auth_method') || 'email'

  useEffect(() => {
    if (!identifier) navigate('/login', { replace: true })
  }, [identifier, navigate])

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!password) return
    setLoading(true)
    try {
      await login(identifier, password)
      sessionStorage.removeItem('login_identifier')
      sessionStorage.removeItem('login_auth_method')
      toast.success('Welcome back!')
      navigate('/dashboard')
    } catch (err: any) {
      const detail = err.response?.data?.detail
      if (err.response?.status === 403) {
        toast.error(detail || 'Account is deactivated. Contact your administrator.')
      } else {
        toast.error(detail || 'Invalid username or password')
      }
    } finally {
      setLoading(false)
    }
  }

  const handleBack = () => {
    sessionStorage.removeItem('login_identifier')
    sessionStorage.removeItem('login_auth_method')
    navigate('/login')
  }

  const handleForgotPassword = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!forgotEmail) return
    setForgotLoading(true)
    try {
      await api.post('/forgot-password', { email: forgotEmail })
      setForgotSent(true)
    } catch {
      setForgotSent(true)
    } finally {
      setForgotLoading(false)
    }
  }

  if (!identifier) return null

  return (
    <div className="min-h-screen flex items-center justify-center bg-rt-bg p-4">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="w-full max-w-md"
      >
        <div className="text-center mb-8">
          <h1 className="text-3xl font-display font-bold text-rt-text">ReTrace</h1>
          <p className="text-sm text-rt-text-muted mt-1">Enterprise Knowledge Management</p>
        </div>

        <div className="card p-6">
          <AnimatePresence mode="wait">
            {!showForgot ? (
              <motion.div key="password" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                <button
                  type="button"
                  onClick={handleBack}
                  className="flex items-center gap-1 text-xs text-rt-text-muted hover:text-rt-text mb-4 transition-colors"
                >
                  <ArrowLeft className="w-3.5 h-3.5" /> Use a different account
                </button>

                <h2 className="text-lg font-semibold text-rt-text mb-1">Enter your password</h2>
                <div className="flex items-center gap-2 mb-6">
                  <div className="w-6 h-6 rounded-full bg-rt-primary/20 flex items-center justify-center">
                    <Mail className="w-3 h-3 text-rt-primary" />
                  </div>
                  <span className="text-sm text-rt-text-muted truncate">{identifier}</span>
                </div>

                <form onSubmit={handleSignIn} className="space-y-4">
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <label className="label mb-0">Password</label>
                      {authMethod === 'email' && (
                        <button
                          type="button"
                          onClick={() => { setShowForgot(true); setForgotSent(false); setForgotEmail(identifier.includes('@') ? identifier : '') }}
                          className="text-xs text-rt-primary hover:text-rt-primary/80 transition-colors"
                        >
                          Forgot password?
                        </button>
                      )}
                    </div>
                    <div className="relative">
                      <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
                      <input
                        type={showPassword ? 'text' : 'password'}
                        className="input pl-10 pr-10"
                        placeholder="••••••••"
                        value={password}
                        onChange={e => setPassword(e.target.value)}
                        required
                        autoComplete="current-password"
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
                    {authMethod === 'ldap' && (
                      <p className="text-[10px] text-rt-text-muted mt-1">
                        Signing in with your network (LDAP) credentials.
                      </p>
                    )}
                  </div>

                  <button
                    type="submit"
                    disabled={loading}
                    className="btn-primary w-full flex items-center justify-center gap-2 py-2.5"
                  >
                    {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <LogIn className="w-4 h-4" />}
                    {loading ? 'Signing in...' : 'Sign In'}
                  </button>
                </form>
              </motion.div>
            ) : (
              <motion.div key="forgot" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                <button
                  onClick={() => { setShowForgot(false); setForgotSent(false) }}
                  className="flex items-center gap-1 text-xs text-rt-text-muted hover:text-rt-text mb-4 transition-colors"
                >
                  <ArrowLeft className="w-3.5 h-3.5" /> Back to sign in
                </button>

                {forgotSent ? (
                  <div className="text-center py-4">
                    <CheckCircle className="w-12 h-12 text-emerald-400 mx-auto mb-3" />
                    <h3 className="text-base font-semibold text-rt-text mb-2">Check your email</h3>
                    <p className="text-sm text-rt-text-muted">
                      If an account exists with <strong>{forgotEmail}</strong>, we've sent a password reset link. Check your inbox.
                    </p>
                    <button
                      onClick={() => { setShowForgot(false); setForgotSent(false) }}
                      className="btn-secondary mt-4 text-sm"
                    >
                      Return to Sign In
                    </button>
                  </div>
                ) : (
                  <>
                    <h2 className="text-lg font-semibold text-rt-text mb-1">Reset your password</h2>
                    <p className="text-xs text-rt-text-muted mb-6">
                      Enter your email address and we'll send you a link to reset your password.
                    </p>
                    <form onSubmit={handleForgotPassword} className="space-y-4">
                      <div>
                        <label className="label">Email Address</label>
                        <div className="relative">
                          <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
                          <input
                            type="email"
                            className="input pl-10"
                            placeholder="you@company.com"
                            value={forgotEmail}
                            onChange={e => setForgotEmail(e.target.value)}
                            required
                            autoFocus
                          />
                        </div>
                      </div>
                      <button
                        type="submit"
                        disabled={forgotLoading}
                        className="btn-primary w-full flex items-center justify-center gap-2 py-2.5"
                      >
                        {forgotLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Mail className="w-4 h-4" />}
                        {forgotLoading ? 'Sending...' : 'Send Reset Link'}
                      </button>
                    </form>
                  </>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </motion.div>
    </div>
  )
}
