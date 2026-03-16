import { useEffect, useRef } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { useAuth } from '@/context/AuthContext'
import axios from 'axios'

const api = axios.create({ baseURL: '/api/v1/auth' })

export default function AzureADCallback() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const { refreshAuth } = useAuth()
  const exchanged = useRef(false)

  useEffect(() => {
    if (exchanged.current) return
    exchanged.current = true

    const code = searchParams.get('code')
    const tenantDomain = sessionStorage.getItem('login_domain') || ''

    if (!code) {
      navigate('/login?error=callback_missing', { replace: true })
      return
    }

    ;(async () => {
      try {
        const resp = await api.post('/azure-ad', {
          code,
          tenant_domain: tenantDomain,
        })

        localStorage.setItem('rt_access_token', resp.data.access_token)
        localStorage.setItem('rt_refresh_token', resp.data.refresh_token)

        sessionStorage.removeItem('login_domain')
        sessionStorage.removeItem('login_identifier')

        await refreshAuth()
        navigate('/dashboard', { replace: true })
      } catch {
        sessionStorage.removeItem('login_domain')
        sessionStorage.removeItem('login_identifier')
        navigate('/login?error=azure_failed', { replace: true })
      }
    })()
  }, [searchParams, navigate, refreshAuth])

  return (
    <div className="min-h-screen flex items-center justify-center bg-rt-bg p-4">
      <div className="text-center">
        <Loader2 className="w-8 h-8 animate-spin text-rt-primary mx-auto mb-4" />
        <h2 className="text-lg font-semibold text-rt-text mb-1">Signing you in...</h2>
        <p className="text-sm text-rt-text-muted">Completing Microsoft authentication</p>
      </div>
    </div>
  )
}
