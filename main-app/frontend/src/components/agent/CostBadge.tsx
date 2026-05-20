import { Coins } from 'lucide-react'
import type { SessionCost } from '@/stores/opencodeStore'

export default function CostBadge({ cost }: { cost: SessionCost | null }) {
  if (!cost || cost.total_cost === 0) return null

  const fmt = (n: number) => n < 0.01 ? '<$0.01' : `$${n.toFixed(2)}`

  return (
    <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-rt-surface/60 border border-rt-border/40 text-[10px] text-rt-text-muted">
      <Coins className="w-3 h-3 text-amber-400" />
      <span>{fmt(cost.total_cost)}</span>
      <span className="opacity-50">·</span>
      <span>{cost.tokens.input + cost.tokens.output + cost.tokens.reasoning} tok</span>
    </div>
  )
}
