import { useEffect, useRef, useState, useCallback } from 'react'
import { Plug, RefreshCw, Loader2, Check, AlertCircle, Power } from 'lucide-react'
import toast from 'react-hot-toast'
import { agentApi } from '@/utils/api'

/**
 * Compact pill that shows live MCP-server connection status and offers a
 * Reconnect button per server. Mirrors OC's `/mcp` endpoint output. Polls
 * every 8s while open + immediately on connect/disconnect actions.
 *
 * Renders a single chip in the agent header row. Click to expand a
 * dropdown with each server, status, and (when relevant) a reconnect
 * action — the same pattern as the team picker next to it.
 */

type McpStatus = { status: string; [k: string]: unknown }

export default function McpStatusPill({ disabled }: { disabled?: boolean }) {
  const [open, setOpen] = useState(false)
  const [servers, setServers] = useState<Record<string, McpStatus>>({})
  const [loading, setLoading] = useState(false)
  const [acting, setActing] = useState<string | null>(null)  // server name in-flight
  const ref = useRef<HTMLDivElement>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const data = await agentApi.opencode.listMcpServers()
      setServers(data || {})
    } catch (err) {
      console.warn('[McpStatusPill] fetch failed', err)
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial load + 8s poll while OPEN. Closed → no polling.
  useEffect(() => {
    refresh()
    if (!open) return
    const id = setInterval(refresh, 8000)
    return () => clearInterval(id)
  }, [open, refresh])

  // Click-outside / Esc dismissal.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  // Hide retrace-* entries: they're ReTrace's built-in tools that happen to
  // be implemented as MCP subprocesses for OC. To the user they're part of
  // the app, not "MCPs you installed." Built-ins are already represented as
  // chips in the toolbar — surfacing them here too leaks the implementation
  // detail and gives users a footgun (the per-server power button).
  const entries = Object.entries(servers).filter(([name]) => !name.startsWith('retrace-'))
  const total = entries.length
  const connected = entries.filter(([, s]) => (s.status || '').toLowerCase() === 'connected').length
  const failed = entries.filter(([, s]) => {
    const st = (s.status || '').toLowerCase()
    return st === 'failed' || st === 'error'
  }).length

  const handleReconnect = async (name: string) => {
    setActing(name)
    try {
      await agentApi.opencode.mcpConnect(name)
      toast.success(`Reconnected ${name}`)
      await refresh()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || `Failed to reconnect ${name}`)
    } finally {
      setActing(null)
    }
  }

  const handleDisconnect = async (name: string) => {
    setActing(name)
    try {
      await agentApi.opencode.mcpDisconnect(name)
      toast.success(`Disconnected ${name}`)
      await refresh()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || `Failed to disconnect ${name}`)
    } finally {
      setActing(null)
    }
  }

  // Hide the chip entirely until we know there are servers to surface.
  if (total === 0 && !loading) return null

  // Chip color reflects worst-state-first: failed > all-good > unknown.
  const chipClass = failed > 0
    ? 'text-red-400 border-red-400/40 bg-red-500/5'
    : connected === total && total > 0
      ? 'text-emerald-400 border-emerald-400/40 bg-emerald-500/5'
      : 'text-rt-text-muted border-rt-border/40 bg-rt-surface/40'

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen(o => !o)}
        title={failed > 0 ? `${failed} MCP server(s) failed — click to reconnect` : 'MCP server status'}
        className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs transition-colors ${chipClass} ${disabled ? 'opacity-60 cursor-not-allowed' : 'hover:brightness-110'}`}
      >
        <Plug className="w-3 h-3" />
        <span className="font-mono">
          {connected}/{total}
        </span>
        {failed > 0 && <AlertCircle className="w-3 h-3" />}
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-1 min-w-[280px] bg-rt-bg-light border border-rt-border rounded-md shadow-xl py-2 z-50">
          <div className="px-3 pb-2 flex items-center gap-2 border-b border-rt-border/40">
            <Plug className="w-3 h-3 text-rt-text-muted/70" />
            <span className="text-[10px] font-semibold text-rt-text-muted/70 uppercase tracking-wide flex-1">MCP Servers</span>
            <button
              type="button"
              onClick={refresh}
              className="p-1 rounded text-rt-text-muted/60 hover:text-rt-text hover:bg-rt-surface transition-colors"
              title="Refresh"
            >
              <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
          <div className="py-1">
            {entries.map(([name, info]) => {
              const status = (info.status || 'unknown').toLowerCase()
              const isOk = status === 'connected'
              const isFail = status === 'failed' || status === 'error'
              const isOff = status === 'disabled' || status === 'disconnected'
              return (
                <div key={name} className="flex items-center gap-2 px-3 py-1.5 hover:bg-rt-surface/40 transition-colors">
                  <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                    isOk ? 'bg-emerald-400' : isFail ? 'bg-red-400' : isOff ? 'bg-rt-text-muted/40' : 'bg-amber-400'
                  }`} />
                  <span className="text-xs text-rt-text flex-1 truncate font-mono">{name}</span>
                  <span className={`text-[10px] font-mono ${
                    isOk ? 'text-emerald-400/70' : isFail ? 'text-red-400/80' : 'text-rt-text-muted/60'
                  }`}>{status}</span>
                  {acting === name ? (
                    <Loader2 className="w-3 h-3 text-rt-text-muted/60 animate-spin" />
                  ) : isOk ? (
                    <button
                      type="button"
                      onClick={() => handleDisconnect(name)}
                      title="Disconnect"
                      className="p-1 rounded text-rt-text-muted/40 hover:text-red-400 hover:bg-red-500/10 transition-colors"
                    >
                      <Power className="w-3 h-3" />
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => handleReconnect(name)}
                      title="Reconnect"
                      className="p-1 rounded text-blue-400 hover:bg-blue-500/10 transition-colors"
                    >
                      {isOff ? <Power className="w-3 h-3" /> : <RefreshCw className="w-3 h-3" />}
                    </button>
                  )}
                </div>
              )
            })}
            {entries.length === 0 && !loading && (
              <div className="px-3 py-2 text-xs text-rt-text-muted/50 italic">No MCP servers configured</div>
            )}
          </div>
          {connected < total && (
            <div className="px-3 pt-2 mt-1 border-t border-rt-border/40 flex items-center gap-1.5 text-[10px] text-rt-text-muted/60">
              <Check className="w-3 h-3 text-emerald-400/60" />
              <span>{connected} of {total} connected</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
