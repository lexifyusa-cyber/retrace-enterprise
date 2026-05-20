import { useState, useRef, useCallback } from 'react'
import { Send, Loader2, Bot, ClipboardList, Undo2, Redo2 } from 'lucide-react'

interface ChatInputProps {
  onSend: (content: string) => void
  isRunning: boolean
  canUndo: boolean
  canRedo: boolean
  onUndo: () => void
  onRedo: () => void
  agent: 'build' | 'plan'
  onSetAgent: (agent: 'build' | 'plan') => void
}

export default function ChatInput({
  onSend, isRunning, canUndo, canRedo, onUndo, onRedo, agent, onSetAgent,
}: ChatInputProps) {
  const [input, setInput] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const handleSubmit = useCallback(() => {
    const trimmed = input.trim()
    if (!trimmed || isRunning) return
    onSend(trimmed)
    setInput('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }, [input, isRunning, onSend])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }, [handleSubmit])

  const handleInput = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value)
    const el = e.target
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 200) + 'px'
  }, [])

  return (
    <div className="border-t border-rt-border bg-rt-bg-light/50 p-4">
      {/* Agent + undo/redo bar */}
      <div className="flex items-center gap-2 mb-2">
        <div className="flex items-center rounded-lg border border-rt-border overflow-hidden">
          <button type="button" onClick={() => { if (!isRunning) onSetAgent('build') }}
            disabled={isRunning}
            className={`flex items-center gap-1 px-2 py-1 text-[11px] font-medium transition-colors ${
              agent === 'build'
                ? 'bg-rt-primary/15 text-rt-primary'
                : 'bg-rt-surface/50 text-rt-text-muted/50 hover:text-rt-text-muted'
            }`}>
            <Bot className="w-3 h-3" /> Build
          </button>
          <button type="button" onClick={() => { if (!isRunning) onSetAgent('plan') }}
            disabled={isRunning}
            className={`flex items-center gap-1 px-2 py-1 text-[11px] font-medium transition-colors border-l border-rt-border ${
              agent === 'plan'
                ? 'bg-blue-500/15 text-blue-400'
                : 'bg-rt-surface/50 text-rt-text-muted/50 hover:text-rt-text-muted'
            }`}>
            <ClipboardList className="w-3 h-3" /> Plan
          </button>
        </div>
        <div className="flex-1" />
        {canUndo && (
          <button type="button" onClick={onUndo} title="Undo last turn"
            className="p-1.5 rounded-lg hover:bg-rt-surface text-rt-text-muted hover:text-rt-text transition-colors">
            <Undo2 className="w-3.5 h-3.5" />
          </button>
        )}
        {canRedo && (
          <button type="button" onClick={onRedo} title="Redo last turn"
            className="p-1.5 rounded-lg hover:bg-rt-surface text-rt-text-muted hover:text-rt-text transition-colors">
            <Redo2 className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      {/* Input area */}
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={input}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder={agent === 'plan' ? 'Ask the Plan agent to analyze or review...' : 'Ask the agent to build, fix, or automate...'}
          disabled={isRunning}
          rows={1}
          className="flex-1 resize-none rounded-lg border border-rt-border bg-rt-bg px-3 py-2 text-sm focus:outline-none focus:border-rt-primary disabled:opacity-50"
        />
        <button
          type="button"
          onClick={handleSubmit}
          disabled={isRunning || !input.trim()}
          className="flex items-center justify-center w-9 h-9 rounded-lg bg-rt-primary text-white disabled:opacity-30 hover:bg-rt-primary/90 transition-colors"
        >
          {isRunning ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
        </button>
      </div>
    </div>
  )
}
