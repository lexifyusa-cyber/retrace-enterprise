import { useEffect } from 'react'
import { useSearchParams, useNavigate, Navigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '@/context/AuthContext'
import { Loader2 } from 'lucide-react'
import toast from 'react-hot-toast'
import axios from 'axios'
import FirstLoginSetup from './FirstLoginSetup'
import ResetPassword from './ResetPassword'

const api = axios.create({ baseURL: '/api/v1/auth' })

/**
 * Handles /setup route: invite acceptance (with token) or first-login password change.
 * When token is present and user is not authenticated, validates the invite token;
 * if auth_provider is azure_ad, redirects to login with message to sign in with Microsoft.
 */
export default function SetupPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const { isAuthenticated, user } = useAuth()
  const token = searchParams.get('token') || ''

  const { data: validateData, isLoading: validateLoading } = useQuery({
    queryKey: ['invite-validate', token],
    queryFn: async () => (await api.get('/invite/validate', { params: { token } })).data,
    enabled: !!token && !isAuthenticated,
    retry: false,
  })

  useEffect(() => {
    if (!token || !validateData || validateData.valid !== true || validateData.auth_provider !== 'azure_ad') return
    navigate('/login?message=azure_invite', { replace: true })
    toast('Sign in with Microsoft to complete your invitation.')
  }, [token, validateData, navigate])

  if (token && !isAuthenticated) {
    if (validateLoading) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-rt-bg p-4">
          <Loader2 className="w-8 h-8 animate-spin text-rt-primary" />
        </div>
      )
    }
    if (validateData?.valid === true && validateData?.auth_provider === 'azure_ad') {
      return null
    }
    if (validateData?.valid === true && validateData?.auth_provider !== 'azure_ad') {
      return <ResetPassword />
    }
    if (validateData?.valid === false) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-rt-bg p-4">
          <div className="card p-6 max-w-md text-center">
            <p className="text-rt-text mb-4">This invite link is invalid or has expired.</p>
            <button onClick={() => navigate('/login')} className="btn-primary">Go to Sign In</button>
          </div>
        </div>
      )
    }
  }

  if (!token) {
    if (!isAuthenticated) return <Navigate to="/login" replace />
    if (user && !user.force_password_change) return <Navigate to="/dashboard" replace />
    return <FirstLoginSetup />
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-rt-bg p-4">
      <Loader2 className="w-8 h-8 animate-spin text-rt-primary" />
    </div>
  )
}
