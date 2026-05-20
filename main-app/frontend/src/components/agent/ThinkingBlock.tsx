import { Brain, ChevronDown, ChevronRight } from 'lucide-react'
import { useState } from 'react'

/**
 * Collapsed-by-default reasoning trace from the model. Single-line header in
 * the same muted "neutral pill" style as tool calls — no colored background,
 * no inline preview. Click to expand the full reasoning.
 */
export default function ThinkingBlock({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false)
  if (!text) return null

  return (
    <div className="my-1.5 rounded-md border border-rt-border/40 bg-rt-surface/30 overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-rt-surface/50 transition-colors"
      >
        {expanded ? (
          <ChevronDown className="w-3 h-3 text-rt-text-muted/60 flex-shrink-0" />
        ) : (
          <ChevronRight className="w-3 h-3 text-rt-text-muted/60 flex-shrink-0" />
        )}
        <Brain className="w-3.5 h-3.5 text-rt-text-muted/70 flex-shrink-0" />
        <span className="text-xs font-medium text-rt-text">Thinking</span>
        <span className="ml-auto text-[10px] text-rt-text-muted/50">{text.length} chars</span>
      </button>
      {expanded && (
        <div className="border-t border-rt-border/20 px-3 py-2 text-xs text-rt-text-muted/80 whitespace-pre-wrap font-mono leading-relaxed max-h-[400px] overflow-auto">
          {text}
        </div>
      )}
    </div>
  )
}
