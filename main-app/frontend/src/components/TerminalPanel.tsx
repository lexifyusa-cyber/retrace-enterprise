import { useEffect, useRef, useCallback, useState } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import { Terminal, X, Maximize2, Minimize2, RotateCcw } from 'lucide-react'

interface TerminalPanelProps {
  conversationId: string | null
  isVisible: boolean
  onToggle: () => void
}

const THEMES: Record<string, Record<string, string>> = {
  dark: {
    background: '#1e2128',
    foreground: '#eceff4',
    cursor: '#88c0d0',
    cursorAccent: '#1e2128',
    selectionBackground: '#434c5e',
    selectionForeground: '#eceff4',
    black: '#3b4252',
    red: '#bf616a',
    green: '#a3be8c',
    yellow: '#ebcb8b',
    blue: '#81a1c1',
    magenta: '#b48ead',
    cyan: '#88c0d0',
    white: '#e5e9f0',
    brightBlack: '#4c566a',
    brightRed: '#bf616a',
    brightGreen: '#a3be8c',
    brightYellow: '#ebcb8b',
    brightBlue: '#81a1c1',
    brightMagenta: '#b48ead',
    brightCyan: '#8fbcbb',
    brightWhite: '#eceff4',
  },
  light: {
    background: '#f8f9fc',
    foreground: '#1a1d23',
    cursor: '#3b82f6',
    cursorAccent: '#f8f9fc',
    selectionBackground: '#d1d9e6',
    selectionForeground: '#1a1d23',
    black: '#1a1d23',
    red: '#ef4444',
    green: '#22c55e',
    yellow: '#f59e0b',
    blue: '#3b82f6',
    magenta: '#a855f7',
    cyan: '#06b6d4',
    white: '#e8ecf4',
    brightBlack: '#64748b',
    brightRed: '#f87171',
    brightGreen: '#4ade80',
    brightYellow: '#fbbf24',
    brightBlue: '#60a5fa',
    brightMagenta: '#c084fc',
    brightCyan: '#22d3ee',
    brightWhite: '#f8f9fc',
  },
  colorful: {
    background: '#0f0a1a',
    foreground: '#f0eeff',
    cursor: '#a78bfa',
    cursorAccent: '#0f0a1a',
    selectionBackground: '#3d2f66',
    selectionForeground: '#f0eeff',
    black: '#1e1538',
    red: '#fb7185',
    green: '#34d399',
    yellow: '#fbbf24',
    blue: '#818cf8',
    magenta: '#a78bfa',
    cyan: '#67e8f9',
    white: '#e8e0ff',
    brightBlack: '#2a2045',
    brightRed: '#fda4af',
    brightGreen: '#6ee7b7',
    brightYellow: '#fcd34d',
    brightBlue: '#a5b4fc',
    brightMagenta: '#c4b5fd',
    brightCyan: '#a5f3fc',
    brightWhite: '#f0eeff',
  },
}

function getActiveTheme(): string {
  const el = document.documentElement
  return el.getAttribute('data-theme') || 'dark'
}

export default function TerminalPanel({ conversationId, isVisible, onToggle }: TerminalPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<XTerm | null>(null)
  const fitAddonRef = useRef<FitAddon | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const [isMaximized, setIsMaximized] = useState(false)
  const [isConnected, setIsConnected] = useState(false)

  const connect = useCallback(() => {
    if (!conversationId) return

    // Close existing connection
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/terminal/${conversationId}`)
    wsRef.current = ws

    ws.onopen = () => {
      setIsConnected(true)
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        const term = xtermRef.current
        if (!term) return

        if (msg.type === 'output' || msg.type === 'scrollback') {
          term.write(msg.data)
        } else if (msg.type === 'exited') {
          term.writeln('\r\n\x1b[90m[Session ended]\x1b[0m')
          setIsConnected(false)
        } else if (msg.type === 'error') {
          term.writeln(`\r\n\x1b[31m[Error: ${msg.data}]\x1b[0m`)
        }
      } catch {
        // binary or non-JSON — write raw
        xtermRef.current?.write(event.data)
      }
    }

    ws.onclose = () => {
      setIsConnected(false)
    }

    ws.onerror = () => {
      setIsConnected(false)
    }
  }, [conversationId])

  // Create / destroy xterm when visibility or conversationId changes
  useEffect(() => {
    if (!isVisible || !conversationId || !containerRef.current) {
      return
    }

    const themeName = getActiveTheme()
    const themeColors = THEMES[themeName] || THEMES.dark

    const term = new XTerm({
      cursorBlink: true,
      cursorStyle: 'bar',
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
      lineHeight: 1.3,
      scrollback: 10000,
      theme: themeColors,
      allowProposedApi: true,
    })

    const fitAddon = new FitAddon()
    const webLinksAddon = new WebLinksAddon()
    term.loadAddon(fitAddon)
    term.loadAddon(webLinksAddon)

    term.open(containerRef.current)

    // Small delay to let the DOM settle before fitting
    requestAnimationFrame(() => {
      fitAddon.fit()
    })

    xtermRef.current = term
    fitAddonRef.current = fitAddon

    // User keystrokes → WebSocket
    term.onData((data) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'input', data }))
      }
    })

    // Resize → WebSocket
    term.onResize(({ cols, rows }) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'resize', cols, rows }))
      }
    })

    // Connect WebSocket
    connect()

    return () => {
      wsRef.current?.close()
      wsRef.current = null
      term.dispose()
      xtermRef.current = null
      fitAddonRef.current = null
      setIsConnected(false)
    }
  }, [isVisible, conversationId, connect])

  // Re-fit on window resize or maximize toggle
  useEffect(() => {
    if (!isVisible) return

    const handleResize = () => {
      requestAnimationFrame(() => {
        fitAddonRef.current?.fit()
      })
    }

    window.addEventListener('resize', handleResize)
    // Also fit when maximized state changes
    const timer = setTimeout(handleResize, 50)

    return () => {
      window.removeEventListener('resize', handleResize)
      clearTimeout(timer)
    }
  }, [isVisible, isMaximized])

  // Theme observer: re-apply theme when data-theme changes
  useEffect(() => {
    if (!xtermRef.current) return

    const observer = new MutationObserver(() => {
      const themeName = getActiveTheme()
      const themeColors = THEMES[themeName] || THEMES.dark
      xtermRef.current?.options && (xtermRef.current.options.theme = themeColors)
    })

    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })

    return () => observer.disconnect()
  }, [isVisible, conversationId])

  if (!isVisible) return null

  return (
    <div
      className={`flex flex-col border-t border-rt-border bg-rt-bg ${
        isMaximized ? 'fixed inset-0 z-50' : ''
      }`}
      style={isMaximized ? undefined : { height: '320px', minHeight: '160px' }}
    >
      {/* Terminal header bar */}
      <div className="flex items-center justify-between px-4 py-1.5 bg-rt-bg-light border-b border-rt-border select-none">
        <div className="flex items-center gap-2 text-xs">
          <Terminal className="w-3.5 h-3.5 text-rt-primary" />
          <span className="font-medium text-rt-text">Terminal</span>
          <div className={`w-1.5 h-1.5 rounded-full ${isConnected ? 'bg-rt-success' : 'bg-rt-text-muted/30'}`} />
          <span className="text-rt-text-muted">
            {isConnected ? 'Connected' : conversationId ? 'Disconnected' : 'No session'}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {conversationId && (
            <button
              type="button"
              onClick={() => {
                xtermRef.current?.clear()
                connect()
              }}
              className="p-1 rounded hover:bg-rt-surface transition-colors text-rt-text-muted hover:text-rt-text"
              title="Reconnect"
            >
              <RotateCcw className="w-3.5 h-3.5" />
            </button>
          )}
          <button
            type="button"
            onClick={() => setIsMaximized(!isMaximized)}
            className="p-1 rounded hover:bg-rt-surface transition-colors text-rt-text-muted hover:text-rt-text"
            title={isMaximized ? 'Restore' : 'Maximize'}
          >
            {isMaximized ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
          </button>
          <button
            type="button"
            onClick={onToggle}
            className="p-1 rounded hover:bg-rt-surface transition-colors text-rt-text-muted hover:text-rt-text"
            title="Close terminal"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Terminal body */}
      <div
        ref={containerRef}
        className="flex-1 overflow-hidden"
        style={{ padding: '4px 0 0 8px' }}
      />
    </div>
  )
}
