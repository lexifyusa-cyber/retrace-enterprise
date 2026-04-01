import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import axios from 'axios'

interface AuthUser {
  user_id: string
  email: string
  display_name?: string
  role: string
  tenant_id?: string
  auth_provider: string
  force_password_change?: boolean
}

interface Tenant {
  tenant_id: string
  name: string
  domain?: string
  auth_method: string
}

interface AuthState {
  user: AuthUser | null
  tenant: Tenant | null
  isLoading: boolean
  isAuthenticated: boolean
  login: (identifier: string, password: string) => Promise<void>
  register: (data: RegisterData) => Promise<void>
  logout: () => Promise<void>
  refreshAuth: () => Promise<boolean>
  changePassword: (newPassword: string, newEmail?: string, currentPassword?: string) => Promise<void>
}

interface RegisterData {
  tenant_name: string
  admin_email: string
  admin_password: string
  admin_display_name?: string
  domain?: string
}

const AuthContext = createContext<AuthState | null>(null)

const api = axios.create({ baseURL: '/api/v1/auth' })

function getStoredTokens() {
  return {
    access: localStorage.getItem('rt_access_token'),
    refresh: localStorage.getItem('rt_refresh_token'),
  }
}

function storeTokens(access: string, refresh: string) {
  localStorage.setItem('rt_access_token', access)
  localStorage.setItem('rt_refresh_token', refresh)
}

function clearTokens() {
  localStorage.removeItem('rt_access_token')
  localStorage.removeItem('rt_refresh_token')
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [user, setUser] = useState<AuthUser | null>(null)
  const [tenant, setTenant] = useState<Tenant | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  const fetchMe = useCallback(async (token: string) => {
    try {
      const resp = await api.get('/me', {
        headers: { Authorization: `Bearer ${token}` },
      })
      setUser(resp.data.user)
      setTenant(resp.data.tenant)
      return true
    } catch {
      return false
    }
  }, [])

  const refreshAuth = useCallback(async (): Promise<boolean> => {
    const { refresh } = getStoredTokens()
    if (!refresh) return false
    try {
      const resp = await api.post('/refresh', { refresh_token: refresh })
      storeTokens(resp.data.access_token, resp.data.refresh_token)
      setUser(resp.data.user)
      return true
    } catch {
      clearTokens()
      setUser(null)
      setTenant(null)
      return false
    }
  }, [])

  useEffect(() => {
    const init = async () => {
      const { access } = getStoredTokens()
      if (access) {
        const ok = await fetchMe(access)
        if (!ok) {
          await refreshAuth()
        }
      }
      setIsLoading(false)
    }
    init()
  }, [fetchMe, refreshAuth])

  const login = async (identifier: string, password: string) => {
    const resp = await api.post('/login', { identifier, password })
    storeTokens(resp.data.access_token, resp.data.refresh_token)
    setUser(resp.data.user)
    const meResp = await api.get('/me', {
      headers: { Authorization: `Bearer ${resp.data.access_token}` },
    })
    setTenant(meResp.data.tenant)
  }

  const register = async (data: RegisterData) => {
    const resp = await api.post('/register', data)
    storeTokens(resp.data.access_token, resp.data.refresh_token)
    setUser(resp.data.user)
    const meResp = await api.get('/me', {
      headers: { Authorization: `Bearer ${resp.data.access_token}` },
    })
    setTenant(meResp.data.tenant)
  }

  const changePassword = async (newPassword: string, newEmail?: string, currentPassword?: string) => {
    const { access } = getStoredTokens()
    const resp = await api.post('/change-password', {
      new_password: newPassword,
      new_email: newEmail || undefined,
      current_password: currentPassword || '',
    }, {
      headers: { Authorization: `Bearer ${access}` },
    })
    storeTokens(resp.data.access_token, resp.data.refresh_token)
    setUser(resp.data.user)
  }

  const logout = async () => {
    const { access } = getStoredTokens()
    try {
      if (access) {
        await api.post('/logout', {}, {
          headers: { Authorization: `Bearer ${access}` },
        })
      }
    } catch { /* ignore */ }
    clearTokens()
    setUser(null)
    setTenant(null)
    queryClient.clear()
  }

  return (
    <AuthContext.Provider
      value={{
        user,
        tenant,
        isLoading,
        isAuthenticated: !!user,
        login,
        register,
        logout,
        refreshAuth,
        changePassword,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
