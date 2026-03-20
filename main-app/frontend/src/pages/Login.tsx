import { useState, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { Loader2, ArrowRight, Mail, ArrowLeft, CheckCircle, Lock, Cloud } from 'lucide-react'
import toast from 'react-hot-toast'
import axios from 'axios'
import { supabase, isSupabaseConfigured } from '@/lib/supabase'

const api = axios.create({ baseURL: '/api/v1/auth' })

export default function Login() {
  const [identifier, setIdentifier] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [showForgot, setShowForgot] = useState(false)
  const [forgotEmail, setForgotEmail] = useState('')
  const [forgotLoading, setForgotLoading] = useState(false)
  const [forgotSent, setForgotSent] = useState(false)
  const [authMode, setAuthMode] = useState<'cloud' | 'local'>(isSupabaseConfigured() ? 'cloud' : 'local')
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  useEffect(() => {
    const err = searchParams.get('error')
    const msg = searchParams.get('message')
    if (msg === 'azure_invite') toast('Sign in with Microsoft to complete your invitation.')
    if (err === 'azure_failed') toast.error('Microsoft sign-in failed. Please try again.')
    else if (err === 'callback_missing') toast.error('Sign-in was interrupted. Please try again.')
    else if (err) toast.error('Sign-in failed. Please try again.')
  }, [searchParams])

  const handleCloudLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!identifier.trim() || !password) return
    setLoading(true)
    try {
      const { data: supaData, error: supaError } = await supabase.auth.signInWithPassword({
        email: identifier.trim(),
        password,
      })
      if (supaError) throw supaError
      if (!supaData.session) throw new Error('No session returned')

      const { data: localData } = await api.post('/remote-login', {
        supabase_token: supaData.session.access_token,
      })
      localStorage.setItem('rt_access_token', localData.access_token)
      localStorage.setItem('rt_refresh_token', localData.refresh_token)
      localStorage.setItem('rt_supabase_token', supaData.session.access_token)
      navigate('/')
    } catch (err: any) {
      const msg = err?.message || err?.response?.data?.detail || 'Sign-in failed'
      toast.error(msg)
    } finally {
      setLoading(false)
    }
  }

  const handleLocalContinue = async (e: React.FormEvent) => {
    e.preventDefault()
    const ident = identifier.trim()
    if (!ident) return
    setLoading(true)
    try {
      const { data } = await api.get('/options', { params: { identifier: ident } })
      const authMethod: string = data.auth_method || 'email'
      const domain: string = data.domain || ''

      if (authMethod === 'azure_ad') {
        sessionStorage.setItem('login_domain', domain)
        sessionStorage.setItem('login_identifier', ident)
        const authzResp = await api.get('/azure-ad/authorize', { params: { domain } })
        window.location.href = authzResp.data.authorize_url
        return
      }

      sessionStorage.setItem('login_identifier', ident)
      sessionStorage.setItem('login_auth_method', authMethod)
      navigate('/login/password')
    } catch (err: any) {
      const status = err.response?.status
      const isServerUnreachable =
        !err.response ||
        err.code === 'ECONNREFUSED' ||
        err.message?.includes('Network Error') ||
        (status >= 502 && status <= 504)
      const detail = err.response?.data?.detail
      if (isServerUnreachable) {
        toast.error(
          'Cannot reach server. Start the backend (e.g. run uvicorn in main-app/backend on port 8000) and try again.'
        )
      } else {
        toast.error(detail || 'Unable to determine sign-in method. Please try again.')
      }
    } finally {
      setLoading(false)
    }
  }

  const handleForgotPassword = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!forgotEmail) return
    setForgotLoading(true)
    try {
      if (authMode === 'cloud') {
        await supabase.auth.resetPasswordForEmail(forgotEmail)
      } else {
        await api.post('/forgot-password', { email: forgotEmail })
      }
      setForgotSent(true)
    } catch {
      setForgotSent(true)
    } finally {
      setForgotLoading(false)
    }
  }

  const showModeToggle = isSupabaseConfigured()

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

        {showModeToggle && (
          <div className="flex justify-center mb-4 gap-2">
            <button
              type="button"
              onClick={() => setAuthMode('cloud')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
                authMode === 'cloud'
                  ? 'bg-rt-primary/15 text-rt-primary border border-rt-primary/30'
                  : 'text-rt-text-muted hover:text-rt-text border border-transparent'
              }`}
            >
              <Cloud className="w-3.5 h-3.5" /> Cloud Account
            </button>
            <button
              type="button"
              onClick={() => setAuthMode('local')}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
                authMode === 'local'
                  ? 'bg-rt-primary/15 text-rt-primary border border-rt-primary/30'
                  : 'text-rt-text-muted hover:text-rt-text border border-transparent'
              }`}
            >
              <Lock className="w-3.5 h-3.5" /> Local / Enterprise
            </button>
          </div>
        )}

        <div className="card p-6">
          <AnimatePresence mode="wait">
            {!showForgot ? (
              <motion.div key={authMode} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                <h2 className="text-lg font-semibold text-rt-text mb-1">
                  {authMode === 'cloud' ? 'Sign in with your Lumena account' : 'Sign in to your account'}
                </h2>
                <p className="text-xs text-rt-text-muted mb-6">
                  {authMode === 'cloud'
                    ? 'Use the email and password from lumenatech.ai.'
                    : 'Enter your work email or username to continue.'}
                </p>

                {authMode === 'cloud' ? (
                  <form onSubmit={handleCloudLogin} className="space-y-4">
                    <div>
                      <label className="label">Email</label>
                      <div className="relative">
                        <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
                        <input
                          type="email"
                          className="input pl-10"
                          placeholder="you@company.com"
                          value={identifier}
                          onChange={e => setIdentifier(e.target.value)}
                          required
                          autoComplete="email"
                          autoFocus
                        />
                      </div>
                    </div>
                    <div>
                      <label className="label">Password</label>
                      <div className="relative">
                        <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
                        <input
                          type="password"
                          className="input pl-10"
                          placeholder="Your password"
                          value={password}
                          onChange={e => setPassword(e.target.value)}
                          required
                          autoComplete="current-password"
                        />
                      </div>
                    </div>
                    <button
                      type="submit"
                      disabled={loading}
                      className="btn-primary w-full flex items-center justify-center gap-2 py-2.5"
                    >
                      {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <ArrowRight className="w-4 h-4" />}
                      {loading ? 'Signing in...' : 'Sign in'}
                    </button>
                  </form>
                ) : (
                  <form onSubmit={handleLocalContinue} className="space-y-4">
                    <div>
                      <label className="label">Email or Username</label>
                      <div className="relative">
                        <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
                        <input
                          type="text"
                          className="input pl-10"
                          placeholder="you@company.com or username"
                          value={identifier}
                          onChange={e => setIdentifier(e.target.value)}
                          required
                          autoComplete="username"
                          autoFocus
                        />
                      </div>
                    </div>
                    <button
                      type="submit"
                      disabled={loading}
                      className="btn-primary w-full flex items-center justify-center gap-2 py-2.5"
                    >
                      {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <ArrowRight className="w-4 h-4" />}
                      {loading ? 'Checking...' : 'Continue'}
                    </button>
                  </form>
                )}

                <div className="mt-4 text-center">
                  <button
                    type="button"
                    onClick={() => { setShowForgot(true); setForgotSent(false); setForgotEmail(identifier.includes('@') ? identifier : '') }}
                    className="text-xs text-rt-primary hover:text-rt-primary/80 transition-colors"
                  >
                    Forgot password?
                  </button>
                </div>
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
