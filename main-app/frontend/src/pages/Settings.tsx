import React, { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Settings as SettingsIcon, Zap, CheckCircle, XCircle, Loader2, Info, Monitor, LayoutPanelLeft, Command, Palette, Sun, Moon, Sparkles, ChevronDown, ChevronRight, Mail, Trash2, Plus, MessageSquare, RefreshCw } from 'lucide-react'
import { motion } from 'framer-motion'
import toast from 'react-hot-toast'
import axios from 'axios'
import { useLayout } from '@/context/LayoutContext'
import { useAuth } from '@/context/AuthContext'

interface LLMSettings {
  api_url: string | null
  api_key: string
  model_name: string
  provider: 'openai' | 'anthropic' | 'custom'
  screenops_api_url?: string | null
  screenops_api_key?: string | null
  screenops_model?: string | null
  screenops_mouse_timeout?: number | null
  debug_logging?: boolean | null
  max_parallel_files?: number | null
}

interface LLMSettingsResponse {
  api_url: string | null
  model_name: string
  provider: string
  api_key_set: boolean
  screenops_api_key_set: boolean
  screenops_api_url?: string | null
  screenops_model?: string | null
  screenops_mouse_timeout?: number
  debug_logging?: boolean
  max_parallel_files?: number
}

const api = axios.create({ baseURL: '/api/v1' })
const authApi = axios.create({ baseURL: '/api/v1/auth' })
const addAuth = (c: any) => {
  const t = localStorage.getItem('rt_access_token')
  if (t) c.headers.Authorization = `Bearer ${t}`
  return c
}
api.interceptors.request.use(addAuth)
authApi.interceptors.request.use(addAuth)

export default function Settings() {
  const { user } = useAuth()
  const [apiUrl, setApiUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [modelName, setModelName] = useState('gpt-4o')
  const [provider, setProvider] = useState<'openai' | 'anthropic' | 'custom'>('openai')
  const [screenopsUrl, setScreenopsUrl] = useState('')
  const [screenopsKey, setScreenopsKey] = useState('')
  const [screenopsModel, setScreenopsModel] = useState('gpt-4o-mini')
  const [screenopsMouseTimeout, setScreenopsMouseTimeout] = useState(30)
  const [debugLogging, setDebugLogging] = useState(false)
  const [maxParallelFiles, setMaxParallelFiles] = useState(1)
  const [testResult, setTestResult] = useState<{ success: boolean; message: string; latency_ms?: number } | null>(null)
  const [layoutThemeExpanded, setLayoutThemeExpanded] = useState(false)
  const [llmExpanded, setLlmExpanded] = useState(false)
  const [emailExpanded, setEmailExpanded] = useState(false)
  const [orgExpanded, setOrgExpanded] = useState(false)
  const [maxProductsPerUserAdmin, setMaxProductsPerUserAdmin] = useState(10)
  const [emailProvider, setEmailProvider] = useState<'microsoft' | 'google' | 'zoho'>('microsoft')
  const [emailTenantName, setEmailTenantName] = useState('')
  const [emailClientId, setEmailClientId] = useState('')
  const [emailClientSecret, setEmailClientSecret] = useState('')
  const [emailTenantId, setEmailTenantId] = useState('common')
  const [emailOauthCode, setEmailOauthCode] = useState('')
  const [emailRedirectUri, setEmailRedirectUri] = useState('')
  const [globalSlackWebhook, setGlobalSlackWebhook] = useState('')
  const [globalTeamsWebhook, setGlobalTeamsWebhook] = useState('')
  const [channelsExpanded, setChannelsExpanded] = useState(false)
  const [channelPlatform, setChannelPlatform] = useState<'slack' | 'teams'>('slack')
  const [channelBotToken, setChannelBotToken] = useState('')
  const [channelName, setChannelName] = useState('')
  const [channelId, setChannelId] = useState('')
  const [channelTeamId, setChannelTeamId] = useState('')
  const [channelProductId, setChannelProductId] = useState('')
  const [channelAutoRespond, setChannelAutoRespond] = useState(false)
  const [channelIngestHistory, setChannelIngestHistory] = useState(true)
  const [authMethod, setAuthMethod] = useState<'email' | 'ldap' | 'azure_ad' | 'google'>('email')
  const [tenantDomain, setTenantDomain] = useState('')
  const [ldapHost, setLdapHost] = useState('')
  const [ldapPort, setLdapPort] = useState(389)
  const [ldapUseSsl, setLdapUseSsl] = useState(false)
  const [ldapBaseDn, setLdapBaseDn] = useState('')
  const [ldapBindDn, setLdapBindDn] = useState('')
  const [ldapBindPassword, setLdapBindPassword] = useState('')
  const [ldapUserLookup, setLdapUserLookup] = useState<'uid' | 'sAMAccountName'>('uid')
  const [authSavePending, setAuthSavePending] = useState(false)
  const [selectedTenantId, setSelectedTenantId] = useState<string | null>(null)
  
  const queryClient = useQueryClient()
  const isGlobalAdmin = !user?.tenant_id
  
  // Load current settings
  const { data: currentSettings } = useQuery<LLMSettingsResponse>({
    queryKey: ['settings', 'llm'],
    queryFn: async () => {
      const response = await api.get('/settings/llm')
      return response.data
    }
  })

  // List tenants (for global admin to pick which org to configure)
  const { data: tenants = [] } = useQuery({
    queryKey: ['auth', 'tenants'],
    queryFn: async () => {
      const r = await authApi.get('/tenants')
      return r.data
    },
    enabled: !!user && (!!user.tenant_id || isGlobalAdmin),
  })

  // Load tenant auth config
  const { data: tenantAuth } = useQuery({
    queryKey: ['auth', 'tenant', selectedTenantId || user?.tenant_id],
    queryFn: async () => {
      const tid = user?.tenant_id || selectedTenantId
      const r = await authApi.get(`/tenant/auth${tid ? `?tenant_id=${tid}` : ''}`)
      return r.data
    },
    retry: false,
    enabled: !!(user?.tenant_id || selectedTenantId),
  })
  React.useEffect(() => {
    if (tenantAuth) {
      setAuthMethod(tenantAuth.auth_method || 'email')
      setTenantDomain(tenantAuth.domain || '')
      setMaxProductsPerUserAdmin(tenantAuth.max_products_per_user_admin ?? 10)
      const lc = tenantAuth.ldap_config
      if (lc) {
        setLdapHost(lc.host || '')
        setLdapPort(lc.port ?? 389)
        setLdapUseSsl(!!lc.use_ssl)
        setLdapBaseDn(lc.base_dn || '')
        setLdapBindDn(lc.bind_dn || '')
        setLdapBindPassword(lc.bind_password === '********' ? '' : (lc.bind_password || ''))
        const f = lc.user_search_filter || '(uid={username})'
        setLdapUserLookup(f.includes('sAMAccountName') ? 'sAMAccountName' : 'uid')
      }
    }
  }, [tenantAuth, isGlobalAdmin])
  React.useEffect(() => {
    if (!isGlobalAdmin || tenants.length === 0) return
    setSelectedTenantId(tenants[0].tenant_id)
  }, [isGlobalAdmin, tenants])

  // Update form when settings load or change
  React.useEffect(() => {
    if (currentSettings) {
      console.log('Updating form with settings:', currentSettings)
      setApiUrl(currentSettings.api_url || '')
      setModelName(currentSettings.model_name || 'gpt-4o')
      setProvider((currentSettings.provider || 'openai') as any)
      setScreenopsUrl(currentSettings.screenops_api_url || '')
      setScreenopsModel(currentSettings.screenops_model || 'gpt-4o-mini')
      setScreenopsMouseTimeout(Math.max(5, Math.min(120, currentSettings.screenops_mouse_timeout ?? 30)))
      setDebugLogging(currentSettings.debug_logging ?? false)
      setMaxParallelFiles(Math.max(1, Math.min(32, currentSettings.max_parallel_files ?? 1)))
      // Note: API keys are not loaded from server for security
    }
  }, [currentSettings])
  
  // Save settings
  const saveMutation = useMutation({
    mutationFn: async (data: LLMSettings) => {
      console.log('Sending save request...', { ...data, api_key: '***' })
      try {
        const response = await api.post('/settings/llm', data, {
          timeout: 10000 // 10 second timeout
        })
        console.log('Save response received:', response.data)
        return response.data
      } catch (error: any) {
        console.error('Save request failed:', error)
        throw error
      }
    },
    onSuccess: async (data) => {
      console.log('Save successful, refetching settings')
      // Clear password fields
      setApiKey('')
      setScreenopsKey('')
      
      // Invalidate and wait for refetch to complete
      await queryClient.invalidateQueries({ queryKey: ['settings', 'llm'] })
      await queryClient.refetchQueries({ queryKey: ['settings', 'llm'] })
      
      toast.success('Settings saved successfully')
    },
    onError: (error: any) => {
      console.error('Save settings error:', error)
      const message = error.response?.data?.detail || error.response?.data?.message || error.message || 'Failed to save settings'
      toast.error(`Failed to save: ${message}`)
    }
  })
  
  // Test connection
  const testMutation = useMutation({
    mutationFn: async (data: LLMSettings) => {
      console.log('Testing connection with:', { ...data, api_key: '***' })
      const response = await api.post('/settings/llm/test', data)
      console.log('Test response:', response.data)
      return response.data
    },
    onSuccess: (data) => {
      console.log('Test successful:', data)
      setTestResult(data)
      if (data.success) {
        toast.success('Connection test completed!')
      } else {
        toast.error('Connection test failed')
      }
    },
    onError: (error: any) => {
      console.error('Test connection error:', error)
      const message = error.response?.data?.detail || error.message || 'Connection test failed'
      setTestResult({ success: false, message })
      toast.error(message)
    }
  })
  
  // Email connections
  const { data: emailConnections = [] } = useQuery<any[]>({
    queryKey: ['email', 'connections'],
    queryFn: async () => {
      const response = await api.get('/email/connections')
      return response.data
    }
  })

  const createEmailConnectionMutation = useMutation({
    mutationFn: async () => {
      const payload: any = {
        provider: emailProvider,
        tenant_name: emailTenantName.trim(),
        oauth_code: emailOauthCode.trim(),
        client_id: emailClientId.trim(),
        client_secret: emailClientSecret.trim(),
        redirect_uri: emailRedirectUri.trim() || '',
        slack_webhook_url: globalSlackWebhook.trim() || null,
        teams_webhook_url: globalTeamsWebhook.trim() || null,
      }
      if (emailProvider === 'microsoft') {
        payload.tenant_id = emailTenantId.trim() || 'common'
      }
      const response = await api.post('/email/connections', payload)
      return response.data
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['email', 'connections'] })
      setEmailOauthCode('')
      setEmailClientId('')
      setEmailClientSecret('')
      setEmailTenantName('')
      setEmailRedirectUri('')
      toast.success('Email provider connected!')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.detail || 'Failed to connect email provider')
    },
  })

  const deleteEmailConnectionMutation = useMutation({
    mutationFn: async (connectionId: string) => {
      await api.delete(`/email/connections/${connectionId}`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['email', 'connections'] })
      toast.success('Email connection removed')
    },
    onError: () => {
      toast.error('Failed to remove connection')
    },
  })

  const testEmailConnectionMutation = useMutation({
    mutationFn: async (connectionId: string) => {
      const response = await api.post(`/email/connections/${connectionId}/test`)
      return response.data
    },
    onSuccess: (data) => {
      if (data.success) {
        toast.success(data.message || 'Connection test passed!')
      } else {
        toast.error(data.message || 'Connection test failed')
      }
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.detail || 'Test failed')
    },
  })

  const syncEmailConnectionMutation = useMutation({
    mutationFn: async (connectionId: string) => {
      const response = await api.post(`/email/connections/${connectionId}/sync`)
      return response.data
    },
    onSuccess: (data) => {
      toast.success(data.message || `Synced ${data.synced ?? 0} message(s)`)
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.detail || 'Sync failed')
    },
  })

  const reconnectMutation = useMutation({
    mutationFn: async ({ connectionId, oauthCode, redirectUri }: { connectionId: string; oauthCode: string; redirectUri: string }) => {
      return emailApi.reconnectConnection(connectionId, oauthCode, redirectUri)
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['email', 'connections'] })
      toast.success(data.message || 'Reconnected successfully')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.detail || 'Reconnect failed')
    },
  })

  // Channel connections (Slack / Teams)
  const { data: channelConnections = [] } = useQuery<any[]>({
    queryKey: ['channels', 'connections'],
    queryFn: async () => {
      const response = await api.get('/channels/connections')
      return response.data
    },
  })

  const { data: products = [] } = useQuery<any[]>({
    queryKey: ['products'],
    queryFn: async () => {
      const response = await api.get('/products')
      return response.data
    },
  })

  const createChannelMutation = useMutation({
    mutationFn: async () => {
      const response = await api.post('/channels/connections', {
        product_id: channelProductId,
        platform: channelPlatform,
        channel_name: channelName.trim(),
        channel_id: channelId.trim(),
        bot_token: channelBotToken.trim(),
        team_id: channelTeamId.trim() || undefined,
        auto_respond: channelAutoRespond,
        ingest_history: channelIngestHistory,
      })
      return response.data
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['channels', 'connections'] })
      setChannelBotToken('')
      setChannelName('')
      setChannelId('')
      setChannelTeamId('')
      toast.success('Channel connected!')
    },
    onError: (error: any) => {
      toast.error(error.response?.data?.detail || 'Failed to connect channel')
    },
  })

  const deleteChannelMutation = useMutation({
    mutationFn: async (id: string) => {
      await api.delete(`/channels/${id}`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['channels', 'connections'] })
      toast.success('Channel removed')
    },
    onError: () => toast.error('Failed to remove channel'),
  })

  const testChannelMutation = useMutation({
    mutationFn: async (id: string) => {
      const response = await api.post(`/channels/${id}/test`)
      return response.data
    },
    onSuccess: (data) => {
      if (data.success) toast.success(data.message || 'Test passed!')
      else toast.error(data.message || 'Test failed')
    },
    onError: (error: any) => toast.error(error.response?.data?.detail || 'Test failed'),
  })

  const syncChannelMutation = useMutation({
    mutationFn: async (id: string) => {
      const response = await api.post(`/channels/${id}/sync`)
      return response.data
    },
    onSuccess: (data) => {
      toast.success(data.message || `Synced ${data.synced} messages`)
      queryClient.invalidateQueries({ queryKey: ['channels', 'connections'] })
    },
    onError: (error: any) => toast.error(error.response?.data?.detail || 'Sync failed'),
  })

  const handleSave = () => {
    if (!modelName.trim()) {
      toast.error('Model name is required')
      return
    }
    if (provider === 'custom' && !apiUrl.trim()) {
      toast.error('API URL is required for custom provider')
      return
    }
    // Allow save when user entered at least one key, or when at least one key is already configured (partial save)
    const hasKeyInput = apiKey.trim() || screenopsKey.trim()
    const hasKeyConfigured = currentSettings?.api_key_set || currentSettings?.screenops_api_key_set
    if (!hasKeyInput && !hasKeyConfigured) {
      toast.error('Enter at least one API key (Chat or ScreenOps) to save')
      return
    }

    // Build payload - only include key fields when user entered a value (backend preserves others)
    const payload: any = {
      model_name: modelName.trim(),
      provider: provider,
    }
    if (apiKey.trim()) {
      payload.api_key = apiKey.trim()
    }
    if (apiUrl.trim()) {
      payload.api_url = apiUrl.trim()
    } else {
      payload.api_url = null
    }
    if (screenopsUrl.trim()) {
      payload.screenops_api_url = screenopsUrl.trim()
    } else {
      payload.screenops_api_url = null
    }
    if (screenopsKey.trim()) {
      payload.screenops_api_key = screenopsKey.trim()
    }
    if (screenopsModel.trim()) {
      payload.screenops_model = screenopsModel.trim()
    } else {
      payload.screenops_model = null
    }
    payload.screenops_mouse_timeout = Math.max(5, Math.min(120, screenopsMouseTimeout))
    payload.debug_logging = debugLogging
    payload.max_parallel_files = Math.max(1, Math.min(32, maxParallelFiles))

    console.log('Saving settings with payload:', {
      ...payload,
      api_key: payload.api_key ? '***' + payload.api_key.slice(-4) : 'not sent',
      screenops_api_url: payload.screenops_api_url || '(not set)',
      screenops_api_key: payload.screenops_api_key ? '***' : 'not sent',
    })
    saveMutation.mutate(payload)
  }
  
  const handleTest = () => {
    console.log('[TEST] Button clicked')
    console.log('[TEST] Current state:', { 
      hasApiKey: !!apiKey, 
      hasModelName: !!modelName,
      apiKeySet: currentSettings?.api_key_set,
      provider 
    })
    
    // Check if we have API key either in form or saved
    const hasApiKey = apiKey.trim() || currentSettings?.api_key_set
    
    if (!hasApiKey) {
      console.log('[TEST] No API key - aborting')
      toast.error('Please save your API key first, then test')
      return
    }
    if (!modelName.trim()) {
      console.log('[TEST] No model name - aborting')
      toast.error('Model name is required')
      return
    }
    
    // Use placeholder if no key in form but key is saved
    // Backend will use the saved key
    const testApiKey = apiKey.trim() || 'saved-key-placeholder'
    const testScreenopsKey = screenopsKey.trim() || (currentSettings?.screenops_api_key_set ? 'saved-key-placeholder' : null)

    if (!apiKey.trim() && currentSettings?.api_key_set) {
      console.log('[TEST] Using saved credentials')
      toast('Testing with saved credentials...', { icon: 'ℹ️' })
    }

    console.log('[TEST] Starting test mutation')
    setTestResult(null)
    testMutation.mutate({
      api_url: apiUrl || null,
      api_key: testApiKey,
      model_name: modelName,
      provider,
      screenops_api_url: screenopsUrl.trim() || null,
      screenops_api_key: testScreenopsKey,
      screenops_model: screenopsModel.trim() || null,
      screenops_mouse_timeout: screenopsMouseTimeout,
    })
  }

  const { layoutMode, setLayoutMode, themeMode, setThemeMode } = useLayout()

  return (
    <div className="p-8">
      {/* Header */}
      <motion.div
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        className="mb-8"
      >
        <h1 className="text-3xl font-display font-bold mb-2">Settings</h1>
        <p className="text-rt-text-muted">
          Configure your <span className="font-extrabold bg-gradient-to-r from-indigo-400 via-purple-400 to-violet-400 bg-clip-text text-transparent">ReTrace</span> system
        </p>
      </motion.div>

      {/* Layout & Theme: expandable */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="max-w-4xl mb-6"
      >
        <div className="card overflow-hidden">
          <button
            type="button"
            onClick={() => setLayoutThemeExpanded((e) => !e)}
            className="w-full flex items-center justify-between gap-3 p-3 text-left hover:bg-rt-bg-light/50 transition-colors"
          >
            <div className="flex items-center gap-2">
              {layoutThemeExpanded ? (
                <ChevronDown className="w-4 h-4 text-rt-text-muted" />
              ) : (
                <ChevronRight className="w-4 h-4 text-rt-text-muted" />
              )}
              <LayoutPanelLeft className="w-4 h-4 text-purple-400" />
              <Palette className="w-4 h-4 text-pink-400" />
              <span className="text-sm font-display font-semibold">Layout & theme</span>
            </div>
            <span className="text-xs text-rt-text-muted">
              {layoutMode === 'classic' ? 'Classic' : 'Command Center'} · {themeMode === 'dark' ? 'Dark' : themeMode === 'light' ? 'Light' : 'Colorful'}
            </span>
          </button>
          {layoutThemeExpanded && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 p-4 pt-0 border-t border-rt-border/50">
              {/* Layout */}
              <div>
                <div className="flex items-center gap-2 mb-3">
                  <LayoutPanelLeft className="w-4 h-4 text-purple-400" />
                  <h2 className="text-sm font-display font-semibold">Interface Layout</h2>
                </div>
                <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => setLayoutMode('classic')}
              className={`relative rounded-lg border-2 p-2.5 text-left transition-all ${
                layoutMode === 'classic'
                  ? 'border-rt-primary bg-rt-primary/5 ring-1 ring-rt-primary/20'
                  : 'border-rt-border hover:border-rt-text-muted/30'
              }`}
            >
              {layoutMode === 'classic' && (
                <div className="absolute top-1.5 right-1.5">
                  <CheckCircle className="w-4 h-4 text-rt-primary" />
                </div>
              )}
              <div className="mb-2 rounded border border-rt-border/50 bg-rt-bg overflow-hidden h-14">
                <div className="flex h-full">
                  <div className="w-1/4 bg-rt-bg-light border-r border-rt-border/30 p-1">
                    <div className="w-full h-1 rounded bg-rt-primary/30 mb-0.5" />
                    <div className="w-3/4 h-0.5 rounded bg-rt-surface mb-0.5" />
                    <div className="w-3/4 h-0.5 rounded bg-rt-surface" />
                  </div>
                  <div className="flex-1 p-1">
                    <div className="w-1/2 h-1 rounded bg-rt-text/20 mb-0.5" />
                    <div className="w-full h-0.5 rounded bg-rt-surface/50 mb-0.5" />
                    <div className="w-4/5 h-0.5 rounded bg-rt-surface/50" />
                  </div>
                </div>
              </div>
              <h3 className="font-semibold text-xs">Classic</h3>
              <p className="text-[10px] text-rt-text-muted leading-tight">Sidebar + full nav</p>
            </button>
            <button
              type="button"
              onClick={() => setLayoutMode('command-center')}
              className={`relative rounded-lg border-2 p-2.5 text-left transition-all ${
                layoutMode === 'command-center'
                  ? 'border-rt-primary bg-rt-primary/5 ring-1 ring-rt-primary/20'
                  : 'border-rt-border hover:border-rt-text-muted/30'
              }`}
            >
              {layoutMode === 'command-center' && (
                <div className="absolute top-1.5 right-1.5">
                  <CheckCircle className="w-4 h-4 text-rt-primary" />
                </div>
              )}
              <div className="mb-2 rounded border border-rt-border/50 bg-rt-bg overflow-hidden h-14">
                <div className="flex flex-col h-full">
                  <div className="h-[1px] bg-gradient-to-r from-rt-primary via-purple-500 to-pink-500" />
                  <div className="h-2 bg-rt-bg-light border-b border-rt-border/30 flex items-center px-1">
                    <div className="w-1 h-1 rounded bg-rt-primary/40" />
                    <div className="ml-0.5 w-6 h-0.5 rounded bg-rt-text/10" />
                  </div>
                  <div className="flex flex-1">
                    <div className="w-2 bg-rt-bg-light/60 border-r border-rt-border/20 flex flex-col items-center pt-0.5 gap-0.5">
                      <div className="w-1 h-1 rounded bg-rt-primary/40" />
                      <div className="w-1 h-1 rounded bg-rt-surface" />
                    </div>
                    <div className="flex-1 p-1">
                      <div className="w-1/3 h-0.5 rounded bg-rt-text/20 mb-0.5" />
                      <div className="w-full h-0.5 rounded bg-rt-surface/40" />
                    </div>
                  </div>
                </div>
              </div>
              <h3 className="font-semibold text-xs flex items-center gap-1">
                Command Center
                <span className="px-1 py-0.5 rounded bg-gradient-to-r from-rt-primary/20 to-purple-500/20 text-[8px] font-bold text-rt-primary uppercase">Pro</span>
              </h3>
              <p className="text-[10px] text-rt-text-muted leading-tight">Top bar, <Command className="w-2 h-2 inline" />K</p>
            </button>
                </div>
              </div>

              {/* Theme */}
              <div>
                <div className="flex items-center gap-2 mb-3">
                  <Palette className="w-4 h-4 text-pink-400" />
                  <h2 className="text-sm font-display font-semibold">Color Theme</h2>
                </div>
                <div className="grid grid-cols-3 gap-2">
            <button
              type="button"
              onClick={() => setThemeMode('dark')}
              className={`relative rounded-lg border-2 p-2.5 text-left transition-all ${
                themeMode === 'dark'
                  ? 'border-rt-primary ring-1 ring-rt-primary/20'
                  : 'border-rt-border hover:border-rt-text-muted/30'
              }`}
            >
              {themeMode === 'dark' && (
                <div className="absolute top-1.5 right-1.5">
                  <CheckCircle className="w-4 h-4 text-rt-primary" />
                </div>
              )}
              <div className="mb-2 rounded overflow-hidden h-10 border border-rt-border/50">
                <div className="h-full bg-[#1e2128] flex">
                  <div className="w-1/4 bg-[#252932] border-r border-[#434c5e]" />
                  <div className="flex-1 p-1">
                    <div className="w-2/3 h-0.5 rounded bg-[#eceff4]/20 mb-0.5" />
                    <div className="w-full h-0.5 rounded bg-[#3b4252]" />
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <Moon className="w-3 h-3 text-rt-text-muted" />
                <h3 className="font-semibold text-xs">Dark</h3>
              </div>
            </button>
            <button
              type="button"
              onClick={() => setThemeMode('light')}
              className={`relative rounded-lg border-2 p-2.5 text-left transition-all ${
                themeMode === 'light'
                  ? 'border-rt-primary ring-1 ring-rt-primary/20'
                  : 'border-rt-border hover:border-rt-text-muted/30'
              }`}
            >
              {themeMode === 'light' && (
                <div className="absolute top-1.5 right-1.5">
                  <CheckCircle className="w-4 h-4 text-rt-primary" />
                </div>
              )}
              <div className="mb-2 rounded overflow-hidden h-10 border border-[#d1d9e6]">
                <div className="h-full bg-[#f8f9fc] flex">
                  <div className="w-1/4 bg-white border-r border-[#d1d9e6]" />
                  <div className="flex-1 p-1">
                    <div className="w-2/3 h-0.5 rounded bg-[#1a1d23]/20 mb-0.5" />
                    <div className="w-full h-0.5 rounded bg-[#e8ecf4]" />
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <Sun className="w-3 h-3 text-amber-500" />
                <h3 className="font-semibold text-xs">Light</h3>
              </div>
            </button>
            <button
              type="button"
              onClick={() => setThemeMode('colorful')}
              className={`relative rounded-lg border-2 p-2.5 text-left transition-all ${
                themeMode === 'colorful'
                  ? 'border-rt-primary ring-1 ring-rt-primary/20'
                  : 'border-rt-border hover:border-rt-text-muted/30'
              }`}
            >
              {themeMode === 'colorful' && (
                <div className="absolute top-1.5 right-1.5">
                  <CheckCircle className="w-4 h-4 text-rt-primary" />
                </div>
              )}
              <div className="mb-2 rounded overflow-hidden h-10 border border-[#3d2f66]">
                <div className="h-full bg-[#0f0a1a] flex flex-col">
                  <div className="h-[1px] bg-gradient-to-r from-violet-500 via-pink-500 to-orange-400" />
                  <div className="flex-1 flex">
                    <div className="w-1/4 bg-[#16102b] border-r border-[#3d2f66]" />
                    <div className="flex-1 p-1">
                      <div className="w-2/3 h-0.5 rounded bg-[#a78bfa]/30 mb-0.5" />
                      <div className="w-full h-0.5 rounded bg-[#2a2045]" />
                    </div>
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <Sparkles className="w-3 h-3 text-violet-400" />
                <h3 className="font-semibold text-xs">Colorful</h3>
              </div>
            </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </motion.div>

      {/* LLM Configuration — collapsible */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.15 }}
        className="max-w-4xl"
      >
        <div className="card overflow-hidden">
          <button
            type="button"
            onClick={() => setLlmExpanded((e) => !e)}
            className="w-full flex items-center justify-between gap-3 p-3 text-left hover:bg-rt-bg-light/50 transition-colors"
          >
            <div className="flex items-center gap-2">
              {llmExpanded ? (
                <ChevronDown className="w-4 h-4 text-rt-text-muted" />
              ) : (
                <ChevronRight className="w-4 h-4 text-rt-text-muted" />
              )}
              <Zap className="w-4 h-4 text-rt-primary" />
              <span className="text-sm font-display font-semibold">LLM API Configuration</span>
            </div>
            <span className="text-xs text-rt-text-muted">
              {provider === 'openai' ? 'OpenAI' : provider === 'anthropic' ? 'Anthropic' : 'Custom'} · {modelName || 'not set'}
              {currentSettings?.api_key_set ? ' · key set' : ''}
            </span>
          </button>
          {llmExpanded && (
          <div className="p-4 pt-0 border-t border-rt-border/50">

          <div className="space-y-4 mt-4">
            {/* Provider */}
            <div>
              <label className="label">Provider</label>
              <div className="grid grid-cols-3 gap-3">
                {[
                  { value: 'openai', label: 'OpenAI' },
                  { value: 'anthropic', label: 'Anthropic' },
                  { value: 'custom', label: 'Custom' }
                ].map((p) => (
                  <button
                    key={p.value}
                    type="button"
                    onClick={() => setProvider(p.value as any)}
                    className={`p-3 rounded-lg border text-sm font-medium transition-all ${
                      provider === p.value
                        ? 'border-rt-primary bg-rt-primary/10 text-rt-primary'
                        : 'border-rt-border hover:border-rt-text-muted'
                    }`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>

            {/* API URL (for custom provider) */}
            {provider === 'custom' && (
              <div>
                <label className="label">API URL</label>
                <input
                  type="text"
                  className="input"
                  placeholder="https://api.your-llm.com/v1"
                  value={apiUrl}
                  onChange={(e) => setApiUrl(e.target.value)}
                />
                <p className="text-xs text-rt-text-muted mt-1">
                  Base URL for your LLM API (OpenAI-compatible)
                </p>
              </div>
            )}

            {/* Model Name */}
            <div>
              <label className="label">Model Name</label>
              <input
                type="text"
                className="input"
                placeholder={
                  provider === 'openai' ? 'gpt-4o' :
                  provider === 'anthropic' ? 'claude-3-5-sonnet-20241022' :
                  'model-name'
                }
                value={modelName}
                onChange={(e) => setModelName(e.target.value)}
              />
              <p className="text-xs text-rt-text-muted mt-1">
                {provider === 'openai' && 'Examples: gpt-4o, gpt-4-turbo, gpt-3.5-turbo'}
                {provider === 'anthropic' && 'Examples: claude-3-5-sonnet-20241022, claude-3-opus-20240229'}
                {provider === 'custom' && 'Enter your model identifier'}
              </p>
            </div>

            {/* API Key */}
            <div>
              <label className="label">API Key</label>
              <input
                type="password"
                className="input font-mono"
                placeholder={
                  provider === 'openai' ? 'sk-...' :
                  provider === 'anthropic' ? 'sk-ant-...' :
                  'Your API key'
                }
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
              {currentSettings?.api_key_set && !apiKey && (
                <p className="text-xs text-rt-success mt-1 flex items-center gap-1">
                  <CheckCircle className="w-3 h-3" />
                  API key is configured (enter new key to update)
                </p>
              )}
            </div>

            {/* Training — Debug logging + Max parallel files */}
            <div className="border-t border-rt-border pt-4 mt-4">
              <h4 className="text-xs font-semibold text-rt-text-muted uppercase tracking-wide mb-3">Training</h4>
              <div className="flex items-start gap-3 mb-4">
                <input
                  type="checkbox"
                  id="debug-logging"
                  checked={debugLogging}
                  onChange={(e) => setDebugLogging(e.target.checked)}
                  className="mt-1 rounded border-rt-border bg-rt-bg text-rt-primary focus:ring-rt-primary"
                />
                <div>
                  <label htmlFor="debug-logging" className="label cursor-pointer">Enable debug logging</label>
                  <p className="text-xs text-rt-text-muted">
                    When enabled, training logs list which folders and files are included or excluded by the LLM.
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <label htmlFor="max-parallel-files" className="label whitespace-nowrap">Max parallel files</label>
                <select
                  id="max-parallel-files"
                  value={maxParallelFiles}
                  onChange={(e) => setMaxParallelFiles(Number(e.target.value))}
                  className="input w-24"
                >
                  {[1, 2, 4, 8, 16, 32].map((n) => (
                    <option key={n} value={n}>{n}</option>
                  ))}
                </select>
                <p className="text-xs text-rt-text-muted">
                  Number of files to extract at once in Phase 3 (higher = faster, more CPU).
                </p>
              </div>
            </div>

            {/* ScreenOps Configuration */}
            <div className="border-t border-rt-border pt-4 mt-4">
              <div className="flex items-center gap-2 mb-3">
                <Monitor className="w-4 h-4 text-purple-400" />
                <label className="label mb-0">ScreenOps — Computer Use (Optional)</label>
              </div>
              
              <div className="space-y-3 bg-rt-surface/30 rounded-lg p-3">
                <div>
                  <label className="label text-xs mb-1 block">API URL</label>
                  <input
                    type="text"
                    className="input font-mono text-sm"
                    placeholder="https://api.openai.com/v1"
                    value={screenopsUrl}
                    onChange={(e) => setScreenopsUrl(e.target.value)}
                  />
                </div>
                <div>
                  <label className="label text-xs mb-1 block">Model name</label>
                  <input
                    type="text"
                    className="input"
                    placeholder="gpt-4o-mini"
                    value={screenopsModel}
                    onChange={(e) => setScreenopsModel(e.target.value)}
                  />
                </div>
                <div>
                  <label className="label text-xs mb-1 block">API Key</label>
                  <input
                    type="password"
                    className="input font-mono"
                    placeholder="API key for coordinate finder"
                    value={screenopsKey}
                    onChange={(e) => setScreenopsKey(e.target.value)}
                  />
                </div>
                {currentSettings?.screenops_api_key_set && !screenopsKey && (
                  <p className="text-xs text-rt-success flex items-center gap-1">
                    <CheckCircle className="w-3 h-3" />
                    ScreenOps API key is configured (enter new key to update)
                  </p>
                )}
                <div className="border-t border-rt-border/50 pt-3 mt-3">
                  <div className="flex items-center gap-3">
                    <label htmlFor="screenops-mouse-timeout" className="label text-xs whitespace-nowrap mb-0">Mouse wait timeout</label>
                    <input
                      id="screenops-mouse-timeout"
                      type="number"
                      className="input w-20 text-center"
                      min={5}
                      max={120}
                      value={screenopsMouseTimeout}
                      onChange={(e) => setScreenopsMouseTimeout(Math.max(5, Math.min(120, Number(e.target.value) || 30)))}
                    />
                    <span className="text-xs text-rt-text-muted">seconds</span>
                  </div>
                  <p className="text-xs text-rt-text-muted mt-1">
                    When running in keyboard-only mode (no API configured above), if a mouse click
                    is unavoidable the agent waits this long for you to click manually, then stops.
                  </p>
                </div>
                <p className="text-xs text-rt-text-muted mt-3">
                  ScreenOps works in keyboard-only mode without the above API. Configure URL + model + key
                  to enable mouse coordinate finding for fully automated screen operations.
                </p>
              </div>
            </div>

            {/* Info box */}
            <div className="bg-rt-primary/10 border border-rt-primary/20 rounded-lg p-4 flex gap-3">
              <Info className="w-5 h-5 text-rt-primary flex-shrink-0 mt-0.5" />
              <div className="text-sm text-rt-text-muted">
                <p className="mb-2">
                  Chat model is used for:
                </p>
                <ul className="list-disc list-inside space-y-1 text-xs">
                  <li>Query understanding and reasoning</li>
                  <li>Answer generation with citations</li>
                  <li>Vision analysis for diagrams (if supported)</li>
                  <li>Agent tool planning and execution</li>
                  <li>ScreenOps vision analysis (coordinate finder uses your configured URL + model + key)</li>
                </ul>
              </div>
            </div>
          </div>

          {/* Test Result */}
          {testResult && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className={`mt-6 p-4 rounded-lg border flex items-start gap-3 ${
                testResult.success
                  ? 'bg-rt-success/10 border-rt-success/20'
                  : 'bg-rt-accent/10 border-rt-accent/20'
              }`}
            >
              {testResult.success ? (
                <CheckCircle className="w-5 h-5 text-rt-success flex-shrink-0 mt-0.5" />
              ) : (
                <XCircle className="w-5 h-5 text-rt-accent flex-shrink-0 mt-0.5" />
              )}
              <div className="flex-1">
                <p className={`font-medium mb-1 whitespace-pre-line ${
                  testResult.success ? 'text-rt-success' : 'text-rt-accent'
                }`}>
                  {testResult.message}
                </p>
                {testResult.latency_ms && (
                  <p className="text-sm text-rt-text-muted">
                    Response time: {testResult.latency_ms}ms
                  </p>
                )}
                {testResult.model_info && (
                  <div className="text-xs text-rt-text-muted mt-2 font-mono bg-rt-surface/50 p-2 rounded">
                    Model: {testResult.model_info.model}
                  </div>
                )}
              </div>
            </motion.div>
          )}

          {/* Actions */}
          <div className="flex gap-3 justify-end mt-6 pt-6 border-t border-rt-border">
            <button
              onClick={handleTest}
              disabled={testMutation.isPending}
              className="btn-secondary flex items-center gap-2"
            >
              {testMutation.isPending ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Testing...
                </>
              ) : (
                <>
                  <Zap className="w-4 h-4" />
                  Test Connection
                </>
              )}
            </button>
            <button
              onClick={handleSave}
              disabled={saveMutation.isPending}
              className="btn-primary flex items-center gap-2"
            >
              {saveMutation.isPending ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Saving...
                </>
              ) : (
                <>
                  <SettingsIcon className="w-4 h-4" />
                  Save Settings
                </>
              )}
            </button>
          </div>
          </div>
          )}
        </div>
      </motion.div>

      {/* Organization Settings — collapsible */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
        className="max-w-4xl mt-6"
      >
        <div className="card overflow-hidden">
          <button
            type="button"
            onClick={() => setOrgExpanded((e) => !e)}
            className="w-full flex items-center justify-between gap-3 p-3 text-left hover:bg-rt-bg-light/50 transition-colors"
          >
            <div className="flex items-center gap-2">
              {orgExpanded ? (
                <ChevronDown className="w-4 h-4 text-rt-text-muted" />
              ) : (
                <ChevronRight className="w-4 h-4 text-rt-text-muted" />
              )}
              <SettingsIcon className="w-4 h-4 text-purple-400" />
              <span className="text-sm font-display font-semibold">Organization Settings</span>
            </div>
          </button>
          {orgExpanded && (
            <div className="p-4 pt-0 border-t border-rt-border/50 space-y-4 mt-2">
              {isGlobalAdmin && tenants.length === 0 && (
                <p className="text-sm text-rt-text-muted">No organization yet. Restart the backend to create the default organization.</p>
              )}
              {((!isGlobalAdmin && user?.tenant_id) || (isGlobalAdmin && selectedTenantId)) && (
              <>
              <div>
                <label className="label">Authentication Method</label>
                <select
                  className="input w-48"
                  value={authMethod}
                  onChange={(e) => setAuthMethod(e.target.value as any)}
                >
                  <option value="email">Email / Password</option>
                  <option value="ldap">LDAP / Active Directory</option>
                  <option value="azure_ad">Azure AD</option>
                  <option value="google">Google</option>
                </select>
              </div>
              {authMethod === 'ldap' && (
                <div className="space-y-3 p-3 rounded-lg bg-rt-surface/30 border border-rt-border">
                  <p className="text-xs font-medium text-rt-text-muted">LDAP — minimal setup</p>
                  <div className="space-y-3">
                    <div>
                      <label className="label text-[10px]">Organization Domain</label>
                      <input type="text" className="input text-sm w-full" placeholder="e.g. planetexpress" value={tenantDomain} onChange={(e) => setTenantDomain(e.target.value)} />
                      <p className="text-[10px] text-rt-text-muted mt-0.5">Users enter this when signing in with LDAP.</p>
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className="label text-[10px]">Host</label>
                        <input type="text" className="input text-sm" placeholder="localhost" value={ldapHost} onChange={(e) => setLdapHost(e.target.value)} />
                      </div>
                      <div>
                        <label className="label text-[10px]">Port</label>
                        <input type="number" className="input text-sm" value={ldapPort} onChange={(e) => setLdapPort(parseInt(e.target.value) || 389)} />
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <input type="checkbox" id="ldap-ssl" checked={ldapUseSsl} onChange={(e) => setLdapUseSsl(e.target.checked)} />
                      <label htmlFor="ldap-ssl" className="text-xs">Use SSL (ldaps://)</label>
                    </div>
                    <div>
                      <label className="label text-[10px]">Base DN</label>
                      <input type="text" className="input text-sm w-full" placeholder="ou=people,dc=planetexpress,dc=com" value={ldapBaseDn} onChange={(e) => setLdapBaseDn(e.target.value)} />
                    </div>
                    <div>
                      <label className="label text-[10px]">Bind DN</label>
                      <input type="text" className="input text-sm w-full" placeholder="cn=admin,dc=planetexpress,dc=com" value={ldapBindDn} onChange={(e) => setLdapBindDn(e.target.value)} />
                    </div>
                    <div>
                      <label className="label text-[10px]">Bind Password</label>
                      <input type="password" className="input text-sm w-full" placeholder={tenantAuth?.ldap_config?.bind_password ? '••••••••' : 'Leave empty to keep existing'} value={ldapBindPassword} onChange={(e) => setLdapBindPassword(e.target.value)} />
                    </div>
                    <div>
                      <label className="label text-[10px]">User lookup</label>
                      <select className="input text-sm w-full" value={ldapUserLookup} onChange={(e) => setLdapUserLookup(e.target.value as 'uid' | 'sAMAccountName')}>
                        <option value="uid">uid (OpenLDAP)</option>
                        <option value="sAMAccountName">sAMAccountName (Active Directory)</option>
                      </select>
                    </div>
                  </div>
                </div>
              )}
              <div>
                <label className="label">Max Products per User Admin</label>
                <input
                  type="number"
                  min={1}
                  max={1000}
                  className="input w-32"
                  value={maxProductsPerUserAdmin}
                  onChange={(e) => setMaxProductsPerUserAdmin(parseInt(e.target.value) || 1)}
                />
                <p className="text-[10px] text-rt-text-muted mt-1">
                  Limits how many products a User Admin can create. Admins are unlimited.
                </p>
              </div>
              <button
                disabled={authSavePending || (isGlobalAdmin && !selectedTenantId)}
                onClick={async () => {
                  setAuthSavePending(true)
                  try {
                    const payload: any = {
                      auth_method: authMethod,
                      max_products_per_user_admin: maxProductsPerUserAdmin,
                    }
                    if (authMethod === 'ldap') payload.domain = tenantDomain || undefined
                    if (isGlobalAdmin && selectedTenantId) payload.tenant_id = selectedTenantId
                    if (authMethod === 'ldap') {
                      payload.ldap_config = {
                        host: ldapHost,
                        port: ldapPort,
                        use_ssl: ldapUseSsl,
                        base_dn: ldapBaseDn,
                        bind_dn: ldapBindDn,
                        bind_password: ldapBindPassword || (tenantAuth?.ldap_config?.bind_password === '********' ? '********' : ''),
                        user_search_filter: ldapUserLookup === 'sAMAccountName' ? '(sAMAccountName={username})' : '(uid={username})',
                        email_attribute: 'mail',
                        display_name_attribute: 'cn',
                      }
                    }
                    await authApi.patch('/tenant/auth', payload)
                    toast.success('Organization settings saved')
                    queryClient.invalidateQueries({ queryKey: ['auth', 'tenant'] })
                    queryClient.invalidateQueries({ queryKey: ['auth', 'tenants'] })
                  } catch (e: any) {
                    toast.error(e.response?.data?.detail || 'Failed to save')
                  } finally {
                    setAuthSavePending(false)
                  }
                }}
                className="btn-primary text-xs"
              >
                {authSavePending ? 'Saving...' : 'Save Organization Settings'}
              </button>
              </>
              )}
            </div>
          )}
        </div>
      </motion.div>

      {/* Email Provider — collapsible */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.25 }}
        className="max-w-4xl mt-6"
      >
        <div className="card overflow-hidden">
          <button
            type="button"
            onClick={() => setEmailExpanded((e) => !e)}
            className="w-full flex items-center justify-between gap-3 p-3 text-left hover:bg-rt-bg-light/50 transition-colors"
          >
            <div className="flex items-center gap-2">
              {emailExpanded ? (
                <ChevronDown className="w-4 h-4 text-rt-text-muted" />
              ) : (
                <ChevronRight className="w-4 h-4 text-rt-text-muted" />
              )}
              <Mail className="w-4 h-4 text-blue-400" />
              <span className="text-sm font-display font-semibold">Email Provider</span>
            </div>
            <span className="text-xs text-rt-text-muted">
              {emailConnections.length > 0
                ? `${emailConnections.length} connected`
                : 'Not configured'}
            </span>
          </button>
          {emailExpanded && (
            <div className="p-4 pt-0 border-t border-rt-border/50">
              {/* Existing connections */}
              {emailConnections.length > 0 && (
                <div className="space-y-2 mt-4 mb-4">
                  <label className="label">Active Connections</label>
                  {emailConnections.map((conn: any) => (
                    <div
                      key={conn.connection_id}
                      className="flex items-center justify-between p-3 rounded-lg border border-rt-border bg-rt-surface/30"
                    >
                      <div className="flex items-center gap-2">
                        <Mail className="w-4 h-4 text-blue-400" />
                        <span className="text-sm font-medium">{conn.tenant_name}</span>
                        <span className="text-xs px-2 py-0.5 rounded bg-rt-primary/10 text-rt-primary capitalize">
                          {conn.provider}
                        </span>
                        <span className={`text-xs px-2 py-0.5 rounded ${
                          conn.status === 'active'
                            ? 'bg-rt-success/10 text-rt-success'
                            : 'bg-rt-accent/10 text-rt-accent'
                        }`}>
                          {conn.status}
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => testEmailConnectionMutation.mutate(conn.connection_id)}
                          disabled={testEmailConnectionMutation.isPending}
                          className="text-xs px-2 py-1 rounded border border-rt-border hover:border-rt-primary text-rt-text-muted hover:text-rt-primary transition-all flex items-center gap-1"
                        >
                          {testEmailConnectionMutation.isPending ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <Zap className="w-3 h-3" />
                          )}
                          Test
                        </button>
                        <button
                          onClick={() => syncEmailConnectionMutation.mutate(conn.connection_id)}
                          disabled={syncEmailConnectionMutation.isPending}
                          className="text-xs px-2 py-1 rounded border border-rt-border hover:border-purple-500 text-rt-text-muted hover:text-purple-400 transition-all flex items-center gap-1"
                        >
                          {syncEmailConnectionMutation.isPending ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <RefreshCw className="w-3 h-3" />
                          )}
                          Sync
                        </button>
                        <button
                          onClick={() => {
                            const code = prompt('Paste a new OAuth authorization code to reconnect.\n\nUse Option B in the instructions below (with access_type=offline) to get one.')
                            if (code && code.trim()) {
                              reconnectMutation.mutate({
                                connectionId: conn.connection_id,
                                oauthCode: code.trim(),
                                redirectUri: emailRedirectUri.trim(),
                              })
                            }
                          }}
                          disabled={reconnectMutation.isPending}
                          className="text-xs px-2 py-1 rounded border border-rt-border hover:border-amber-500 text-rt-text-muted hover:text-amber-400 transition-all flex items-center gap-1"
                        >
                          {reconnectMutation.isPending ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <RefreshCw className="w-3 h-3" />
                          )}
                          Reconnect
                        </button>
                        <button
                          onClick={() => {
                            if (confirm('Remove this email connection?')) {
                              deleteEmailConnectionMutation.mutate(conn.connection_id)
                            }
                          }}
                          className="text-rt-text-muted hover:text-rt-accent transition-colors"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Add new connection */}
              <div className="space-y-4 mt-4">
                <div>
                  <label className="label">Provider</label>
                  <div className="grid grid-cols-3 gap-3">
                    {[
                      { value: 'microsoft', label: 'Microsoft 365' },
                      { value: 'google', label: 'Google Workspace' },
                      { value: 'zoho', label: 'Zoho Mail' },
                    ].map((p) => (
                      <button
                        key={p.value}
                        type="button"
                        onClick={() => setEmailProvider(p.value as any)}
                        className={`p-3 rounded-lg border text-sm font-medium transition-all ${
                          emailProvider === p.value
                            ? 'border-blue-500 bg-blue-500/10 text-blue-400'
                            : 'border-rt-border hover:border-rt-text-muted'
                        }`}
                      >
                        {p.label}
                      </button>
                    ))}
                  </div>
                </div>

                <div>
                  <label className="label">Connection Name</label>
                  <input
                    type="text"
                    className="input"
                    placeholder="e.g. Acme Corp Google"
                    value={emailTenantName}
                    onChange={(e) => setEmailTenantName(e.target.value)}
                  />
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="label">OAuth Client ID</label>
                    <input
                      type="text"
                      className="input font-mono text-xs"
                      placeholder="Client ID from app registration"
                      value={emailClientId}
                      onChange={(e) => setEmailClientId(e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="label">OAuth Client Secret</label>
                    <input
                      type="password"
                      className="input font-mono text-xs"
                      placeholder="Client secret"
                      value={emailClientSecret}
                      onChange={(e) => setEmailClientSecret(e.target.value)}
                    />
                  </div>
                </div>

                {emailProvider === 'microsoft' && (
                  <div>
                    <label className="label">Tenant ID</label>
                    <input
                      type="text"
                      className="input font-mono text-xs"
                      placeholder="common (or your Azure AD tenant ID)"
                      value={emailTenantId}
                      onChange={(e) => setEmailTenantId(e.target.value)}
                    />
                  </div>
                )}

                <div>
                  <label className="label">Redirect URI</label>
                  <input
                    type="text"
                    className="input font-mono text-xs"
                    placeholder={
                      emailProvider === 'microsoft'
                        ? 'https://login.microsoftonline.com/common/oauth2/nativeclient'
                        : emailProvider === 'google'
                        ? 'http://localhost or urn:ietf:wg:oauth:2.0:oob'
                        : 'https://your-domain.com/callback'
                    }
                    value={emailRedirectUri}
                    onChange={(e) => setEmailRedirectUri(e.target.value)}
                  />
                  <p className="text-xs text-rt-text-muted mt-1">
                    Must match the redirect URI configured in your{' '}
                    {emailProvider === 'microsoft' ? 'Azure app registration'
                      : emailProvider === 'google' ? 'Google Cloud Console'
                      : 'Zoho API Console'}.
                  </p>
                </div>

                <div>
                  <label className="label">OAuth Authorization Code</label>
                  <input
                    type="text"
                    className="input font-mono text-xs"
                    placeholder="Paste the authorization code from OAuth flow"
                    value={emailOauthCode}
                    onChange={(e) => setEmailOauthCode(e.target.value)}
                  />
                  {emailProvider === 'zoho' && emailClientId.trim() && (
                    <div className="mt-2 p-3 rounded-lg bg-blue-500/5 border border-blue-500/20">
                      <p className="text-xs text-rt-text-muted mb-2">
                        <strong className="text-blue-400">How to get the code:</strong>
                      </p>
                      <p className="text-xs text-rt-text-muted mb-1">
                        <strong>Option A — Self Client (easiest):</strong> In{' '}
                        <a href="https://api-console.zoho.com" target="_blank" rel="noopener noreferrer"
                          className="text-blue-400 hover:underline">api-console.zoho.com</a>
                        , go to your client → <em>Generate Code</em> tab. Enter scope:{' '}
                        <code className="text-[10px] bg-rt-surface px-1 py-0.5 rounded">
                          ZohoMail.accounts.READ,ZohoMail.messages.ALL
                        </code>
                        , select a time duration, and click <em>Generate</em>.
                        <br />
                        <strong className="text-amber-400">Important:</strong> Self-client codes may not return a refresh token.
                        Use <strong>Option B</strong> (with <code className="text-[10px] bg-rt-surface px-1 py-0.5 rounded">access_type=offline</code>) to get a refresh token so your connection survives beyond 1 hour.
                      </p>
                      <p className="text-xs text-rt-text-muted">
                        <strong>Option B — Server-based app:</strong> Open this URL in your browser, approve, and copy the code from the redirect:
                      </p>
                      <code className="text-[10px] block bg-rt-surface p-2 rounded mt-1 break-all text-rt-text-muted leading-relaxed select-all">
                        https://accounts.zoho.com/oauth/v2/auth?scope=ZohoMail.accounts.READ,ZohoMail.messages.ALL&client_id={emailClientId.trim()}&response_type=code&access_type=offline&redirect_uri={emailRedirectUri.trim() || '{YOUR_REDIRECT_URI}'}
                      </code>
                    </div>
                  )}
                  {emailProvider === 'microsoft' && (
                    <p className="text-xs text-rt-text-muted mt-1">
                      Visit your Azure app's OAuth authorize URL with scope <code className="text-[10px] bg-rt-surface px-1 py-0.5 rounded">Mail.Read Mail.Send offline_access</code>, sign in, and paste the code from the redirect.
                    </p>
                  )}
                  {emailProvider === 'google' && (
                    <p className="text-xs text-rt-text-muted mt-1">
                      Visit your Google OAuth consent URL with scope <code className="text-[10px] bg-rt-surface px-1 py-0.5 rounded">https://www.googleapis.com/auth/gmail.modify</code>, sign in, and paste the code.
                    </p>
                  )}
                  {emailProvider === 'zoho' && !emailClientId.trim() && (
                    <p className="text-xs text-rt-text-muted mt-1">
                      Enter your Client ID above to see instructions for generating the authorization code.
                    </p>
                  )}
                </div>

                {/* Global notification webhooks */}
                <div className="border-t border-rt-border pt-4 mt-4">
                  <label className="label">Notification Webhooks (Optional)</label>
                  <p className="text-xs text-rt-text-muted mb-3">
                    Global webhook URLs used for escalations, summaries, and routing. Can be overridden per product.
                  </p>
                  <div className="space-y-3 bg-rt-surface/30 rounded-lg p-3">
                    <div>
                      <label className="label text-xs mb-1 block">Slack Incoming Webhook</label>
                      <input
                        type="text"
                        className="input font-mono text-xs"
                        placeholder="https://hooks.slack.com/services/..."
                        value={globalSlackWebhook}
                        onChange={(e) => setGlobalSlackWebhook(e.target.value)}
                      />
                    </div>
                    <div>
                      <label className="label text-xs mb-1 block">Teams Incoming Webhook</label>
                      <input
                        type="text"
                        className="input font-mono text-xs"
                        placeholder="https://outlook.office.com/webhook/..."
                        value={globalTeamsWebhook}
                        onChange={(e) => setGlobalTeamsWebhook(e.target.value)}
                      />
                    </div>
                  </div>
                </div>

                <div className="flex justify-end pt-2">
                  <button
                    onClick={() => createEmailConnectionMutation.mutate()}
                    disabled={
                      createEmailConnectionMutation.isPending ||
                      !emailTenantName.trim() ||
                      !emailOauthCode.trim() ||
                      !emailClientId.trim()
                    }
                    className="btn-primary flex items-center gap-2"
                  >
                    {createEmailConnectionMutation.isPending ? (
                      <>
                        <Loader2 className="w-4 h-4 animate-spin" />
                        Connecting...
                      </>
                    ) : (
                      <>
                        <Plus className="w-4 h-4" />
                        Connect Provider
                      </>
                    )}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </motion.div>

      {/* Slack & Teams Channels — collapsible */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.35 }}
        className="max-w-4xl mt-6"
      >
        <div className="card overflow-hidden">
          <button
            type="button"
            onClick={() => setChannelsExpanded((e) => !e)}
            className="w-full flex items-center justify-between gap-3 p-3 text-left hover:bg-rt-bg-light/50 transition-colors"
          >
            <div className="flex items-center gap-2">
              {channelsExpanded ? (
                <ChevronDown className="w-4 h-4 text-rt-text-muted" />
              ) : (
                <ChevronRight className="w-4 h-4 text-rt-text-muted" />
              )}
              <MessageSquare className="w-4 h-4 text-green-400" />
              <span className="text-sm font-display font-semibold">Slack & Teams Channels</span>
            </div>
            <span className="text-xs text-rt-text-muted">
              {channelConnections.length > 0
                ? `${channelConnections.length} channel${channelConnections.length > 1 ? 's' : ''}`
                : 'Not configured'}
            </span>
          </button>
          {channelsExpanded && (
            <div className="p-4 pt-0 border-t border-rt-border/50">
              {/* Existing channels */}
              {channelConnections.length > 0 && (
                <div className="space-y-2 mt-4 mb-4">
                  <label className="label">Connected Channels</label>
                  {channelConnections.map((ch: any) => (
                    <div
                      key={ch.connection_id}
                      className="flex items-center justify-between p-3 rounded-lg border border-rt-border bg-rt-surface/30"
                    >
                      <div className="flex items-center gap-2 flex-1 min-w-0">
                        <MessageSquare className="w-4 h-4 text-green-400 flex-shrink-0" />
                        <span className="text-sm font-medium truncate">{ch.channel_name}</span>
                        <span className="text-xs px-2 py-0.5 rounded bg-green-500/10 text-green-400 capitalize flex-shrink-0">
                          {ch.platform}
                        </span>
                        {ch.auto_respond && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-400 flex-shrink-0">
                            Auto-respond
                          </span>
                        )}
                        {ch.ingest_history && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400 flex-shrink-0">
                            Ingest
                          </span>
                        )}
                        {ch.last_synced_at && (
                          <span className="text-[10px] text-rt-text-muted flex-shrink-0">
                            Synced {new Date(ch.last_synced_at).toLocaleDateString()}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        <button
                          onClick={() => testChannelMutation.mutate(ch.connection_id)}
                          disabled={testChannelMutation.isPending}
                          className="text-xs px-2 py-1 rounded border border-rt-border hover:border-rt-primary text-rt-text-muted hover:text-rt-primary transition-all flex items-center gap-1"
                        >
                          {testChannelMutation.isPending ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <Zap className="w-3 h-3" />
                          )}
                          Test
                        </button>
                        <button
                          onClick={() => syncChannelMutation.mutate(ch.connection_id)}
                          disabled={syncChannelMutation.isPending}
                          className="text-xs px-2 py-1 rounded border border-rt-border hover:border-purple-500 text-rt-text-muted hover:text-purple-400 transition-all flex items-center gap-1"
                        >
                          {syncChannelMutation.isPending ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <RefreshCw className="w-3 h-3" />
                          )}
                          Sync
                        </button>
                        <button
                          onClick={() => {
                            if (confirm('Remove this channel connection?')) {
                              deleteChannelMutation.mutate(ch.connection_id)
                            }
                          }}
                          className="text-rt-text-muted hover:text-rt-accent transition-colors"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Add new channel */}
              <div className="space-y-4 mt-4">
                <div>
                  <label className="label">Platform</label>
                  <div className="grid grid-cols-2 gap-3">
                    {[
                      { value: 'slack', label: 'Slack' },
                      { value: 'teams', label: 'Microsoft Teams' },
                    ].map((p) => (
                      <button
                        key={p.value}
                        type="button"
                        onClick={() => setChannelPlatform(p.value as any)}
                        className={`p-3 rounded-lg border text-sm font-medium transition-all ${
                          channelPlatform === p.value
                            ? 'border-green-500 bg-green-500/10 text-green-400'
                            : 'border-rt-border hover:border-rt-text-muted'
                        }`}
                      >
                        {p.label}
                      </button>
                    ))}
                  </div>
                </div>

                <div>
                  <label className="label">Product</label>
                  <select
                    className="input"
                    value={channelProductId}
                    onChange={(e) => setChannelProductId(e.target.value)}
                  >
                    <option value="">Select a product...</option>
                    {products.map((p: any) => (
                      <option key={p.product_id} value={p.product_id}>
                        {p.product_name}
                      </option>
                    ))}
                  </select>
                  <p className="text-xs text-rt-text-muted mt-1">
                    Channel messages will be linked to this product's knowledge base.
                  </p>
                </div>

                <div>
                  <label className="label">
                    {channelPlatform === 'slack' ? 'Bot Token' : 'App Credentials (JSON)'}
                  </label>
                  <input
                    type="password"
                    className="input font-mono text-xs"
                    placeholder={
                      channelPlatform === 'slack'
                        ? 'xoxb-...'
                        : '{"client_id":"...","client_secret":"...","tenant_id":"..."}'
                    }
                    value={channelBotToken}
                    onChange={(e) => setChannelBotToken(e.target.value)}
                  />
                  <p className="text-xs text-rt-text-muted mt-1">
                    {channelPlatform === 'slack'
                      ? 'Slack Bot Token with channels:history, channels:read, chat:write scopes.'
                      : 'JSON with client_id, client_secret, tenant_id from Azure AD app registration.'}
                  </p>
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="label">Channel Name</label>
                    <input
                      type="text"
                      className="input"
                      placeholder="e.g. #general"
                      value={channelName}
                      onChange={(e) => setChannelName(e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="label">Channel ID</label>
                    <input
                      type="text"
                      className="input font-mono text-xs"
                      placeholder={channelPlatform === 'slack' ? 'C01ABC23DEF' : 'channel-id'}
                      value={channelId}
                      onChange={(e) => setChannelId(e.target.value)}
                    />
                  </div>
                </div>

                {channelPlatform === 'teams' && (
                  <div>
                    <label className="label">Team ID</label>
                    <input
                      type="text"
                      className="input font-mono text-xs"
                      placeholder="Team ID from Microsoft Teams"
                      value={channelTeamId}
                      onChange={(e) => setChannelTeamId(e.target.value)}
                    />
                  </div>
                )}

                <div className="flex gap-4">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={channelIngestHistory}
                      onChange={(e) => setChannelIngestHistory(e.target.checked)}
                      className="accent-rt-primary"
                    />
                    <span className="text-xs">Ingest history into knowledge base</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={channelAutoRespond}
                      onChange={(e) => setChannelAutoRespond(e.target.checked)}
                      className="accent-rt-primary"
                    />
                    <span className="text-xs">Auto-respond to questions in channel</span>
                  </label>
                </div>

                <div className="flex justify-end pt-2">
                  <button
                    onClick={() => createChannelMutation.mutate()}
                    disabled={
                      createChannelMutation.isPending ||
                      !channelBotToken.trim() ||
                      !channelName.trim() ||
                      !channelId.trim() ||
                      !channelProductId
                    }
                    className="btn-primary flex items-center gap-2"
                  >
                    {createChannelMutation.isPending ? (
                      <>
                        <Loader2 className="w-4 h-4 animate-spin" />
                        Connecting...
                      </>
                    ) : (
                      <>
                        <Plus className="w-4 h-4" />
                        Connect Channel
                      </>
                    )}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </motion.div>

      {/* Company attribution */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.4 }}
        className="max-w-4xl mt-10 mb-4"
      >
        <div className="flex items-center justify-center gap-3 py-4 border-t border-rt-border/30">
          <div className="w-5 h-5 bg-gradient-to-br from-indigo-500 to-purple-600 rounded flex items-center justify-center">
            <svg className="w-3 h-3 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M4 4v16" /><path d="M4 4h8q5 0 5 5t-5 5H4" /><path d="M11 14l7 6" />
            </svg>
          </div>
          <div className="text-xs text-rt-text-muted">
            <span className="font-extrabold bg-gradient-to-r from-indigo-400 via-purple-400 to-violet-400 bg-clip-text text-transparent">ReTrace</span>
            <span className="opacity-60"> is a product of </span>
            <span className="font-semibold opacity-80">Lumena Technologies</span>
            <span className="opacity-40"> · </span>
            <a href="https://lumenatech.io" target="_blank" rel="noopener noreferrer" className="opacity-50 hover:opacity-80 hover:text-indigo-400 transition-all">lumenatech.io</a>
          </div>
        </div>
      </motion.div>
    </div>
  )
}
