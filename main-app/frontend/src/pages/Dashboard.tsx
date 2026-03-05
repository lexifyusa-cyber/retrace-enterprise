import { useQuery } from '@tanstack/react-query'
import {
  Server, FolderTree, Activity, MessageSquare, ArrowRight, Bot,
  ClipboardList, FileText, CheckCircle2, XCircle, Loader2, Clock,
  Zap, TrendingUp, Calendar, Timer, ChevronRight, Play, BarChart3,
  Sparkles,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { agentApi } from '@/utils/api'
import { motion } from 'framer-motion'

export default function Dashboard() {
  const { data, isLoading } = useQuery({
    queryKey: ['dashboard-stats'],
    queryFn: () => agentApi.getDashboardStats(),
    refetchInterval: 30000, // auto-refresh every 30s
  })

  const stats = data?.stats
  const recentRuns = data?.recent_runs || []
  const upcomingSchedules = data?.upcoming_schedules || []
  const productCoverage = data?.product_coverage || []

  const greeting = getGreeting()

  return (
    <div className="p-8 max-w-[1400px] mx-auto">
      {/* Header */}
      <motion.div
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        className="mb-8"
      >
        <h1 className="text-3xl font-display font-bold mb-1">{greeting}</h1>
        <p className="text-rt-text-muted">
          {stats?.conversations_today
            ? `${stats.conversations_today} agent session${stats.conversations_today === 1 ? '' : 's'} today · `
            : ''}
          {stats ? `${stats.total_automations} automation${stats.total_automations === 1 ? '' : 's'} · ${stats.total_docs} document${stats.total_docs === 1 ? '' : 's'}` : 'Loading your workspace...'}
        </p>
      </motion.div>

      {isLoading ? (
        <div className="flex items-center justify-center py-20 gap-2 text-rt-text-muted">
          <Loader2 className="w-5 h-5 animate-spin" />
          Loading dashboard...
        </div>
      ) : (
        <>
          {/* ============================================================
              ROW 1: Hero Stat Cards
              ============================================================ */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-8">
            <StatCard
              label="Knowledge Coverage"
              value={`${stats?.knowledge_coverage_pct || 0}%`}
              sub={`${stats?.trained_groups || 0}/${stats?.total_groups || 0} groups`}
              icon={BarChart3}
              color="text-rt-primary"
              bg="bg-rt-primary/10"
              href="/products"
              delay={0}
              ring={stats?.knowledge_coverage_pct}
            />
            <StatCard
              label="Active Automations"
              value={stats?.active_automations || 0}
              sub={`${stats?.approved_automations || 0} approved`}
              icon={ClipboardList}
              color="text-emerald-400"
              bg="bg-emerald-500/10"
              href="/sops"
              delay={0.05}
            />
            <StatCard
              label="Agent Sessions"
              value={stats?.conversations_today || 0}
              sub="today"
              icon={Bot}
              color="text-blue-400"
              bg="bg-blue-500/10"
              href="/agent"
              delay={0.1}
            />
            <StatCard
              label="Documents"
              value={stats?.total_docs || 0}
              sub={stats?.docs_this_week ? `+${stats.docs_this_week} this week` : 'none this week'}
              icon={FileText}
              color="text-purple-400"
              bg="bg-purple-500/10"
              href="/docs"
              delay={0.15}
            />
            <StatCard
              label="Success Rate"
              value={`${stats?.execution_success_rate || 100}%`}
              sub={`${stats?.total_runs || 0} total runs`}
              icon={TrendingUp}
              color={stats?.execution_success_rate && stats.execution_success_rate < 80 ? 'text-amber-400' : 'text-emerald-400'}
              bg={stats?.execution_success_rate && stats.execution_success_rate < 80 ? 'bg-amber-500/10' : 'bg-emerald-500/10'}
              href="/sops"
              delay={0.2}
            />
            <StatCard
              label="Connected PODs"
              value={`${stats?.online_pods || 0}/${stats?.total_pods || 0}`}
              sub={stats?.online_pods ? 'online' : 'none connected'}
              icon={Server}
              color={stats?.online_pods ? 'text-emerald-400' : 'text-rt-text-muted'}
              bg={stats?.online_pods ? 'bg-emerald-500/10' : 'bg-rt-surface'}
              href="/pods"
              delay={0.25}
            />
          </div>

          {/* ============================================================
              ROW 2: Activity Timeline + Knowledge Coverage
              ============================================================ */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
            {/* Automation Activity */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.3 }}
              className="card"
            >
              <div className="flex items-center justify-between mb-5">
                <div className="flex items-center gap-2">
                  <Activity className="w-4 h-4 text-rt-primary" />
                  <h2 className="text-sm font-display font-semibold">Automation Activity</h2>
                </div>
                <Link to="/sops" className="text-xs text-rt-primary hover:underline">View all</Link>
              </div>
              {recentRuns.length === 0 ? (
                <div className="text-center py-10">
                  <Activity className="w-8 h-8 mx-auto mb-2 text-rt-text-muted/20" />
                  <p className="text-xs text-rt-text-muted">No automation runs yet</p>
                  <Link to="/sops" className="text-xs text-rt-primary hover:underline mt-1 inline-block">Create your first automation</Link>
                </div>
              ) : (
                <div className="space-y-1 max-h-[320px] overflow-y-auto pr-1">
                  {recentRuns.map((run: any, i: number) => (
                    <div key={run.run_id} className="flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-rt-surface/40 transition-colors group">
                      <div className="flex-shrink-0">
                        {run.status === 'completed' ? (
                          <CheckCircle2 className="w-4 h-4 text-emerald-400" />
                        ) : run.status === 'failed' ? (
                          <XCircle className="w-4 h-4 text-red-400" />
                        ) : (
                          <Loader2 className="w-4 h-4 text-blue-400 animate-spin" />
                        )}
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="text-xs font-medium truncate">{run.sop_title}</p>
                        <p className="text-[10px] text-rt-text-muted">
                          {run.trigger === 'manual' ? '🖱 Manual' : '⏰ Scheduled'}
                          {' · '}{run.steps_completed}/{run.steps_total} steps
                          {run.duration_ms ? ` · ${(run.duration_ms / 1000).toFixed(1)}s` : ''}
                        </p>
                      </div>
                      <span className="text-[10px] text-rt-text-muted/60 flex-shrink-0">
                        {run.started_at ? timeAgo(run.started_at) : ''}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </motion.div>

            {/* Knowledge Coverage by Product */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.35 }}
              className="card"
            >
              <div className="flex items-center justify-between mb-5">
                <div className="flex items-center gap-2">
                  <FolderTree className="w-4 h-4 text-rt-primary" />
                  <h2 className="text-sm font-display font-semibold">Knowledge Coverage by Product</h2>
                </div>
                <Link to="/products" className="text-xs text-rt-primary hover:underline">View all</Link>
              </div>
              {productCoverage.length === 0 ? (
                <div className="text-center py-10">
                  <FolderTree className="w-8 h-8 mx-auto mb-2 text-rt-text-muted/20" />
                  <p className="text-xs text-rt-text-muted">No products created yet</p>
                  <Link to="/products" className="text-xs text-rt-primary hover:underline mt-1 inline-block">Create your first product</Link>
                </div>
              ) : (
                <div className="space-y-3 max-h-[320px] overflow-y-auto pr-1">
                  {productCoverage.map((p: any) => (
                    <Link key={p.product_id} to="/products" className="block group">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-xs font-medium truncate">{p.product_name}</span>
                        <span className="text-[10px] text-rt-text-muted">{p.coverage_pct}%</span>
                      </div>
                      <div className="w-full h-2 rounded-full bg-rt-surface overflow-hidden">
                        <div className="h-full rounded-full bg-gradient-to-r from-rt-primary to-emerald-400 transition-all duration-700"
                          style={{ width: `${p.coverage_pct}%` }} />
                      </div>
                      <div className="flex items-center gap-3 mt-1 text-[10px] text-rt-text-muted">
                        <span>{p.trained_groups} trained</span>
                        {p.in_progress_groups > 0 && <span className="text-amber-400">{p.in_progress_groups} training</span>}
                        <span>{p.total_groups - p.trained_groups - p.in_progress_groups} pending</span>
                      </div>
                    </Link>
                  ))}
                </div>
              )}
            </motion.div>
          </div>

          {/* ============================================================
              ROW 3: Upcoming Schedules + System Overview + Quick Actions
              ============================================================ */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {/* Upcoming Scheduled Runs */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.4 }}
              className="card"
            >
              <div className="flex items-center gap-2 mb-5">
                <Calendar className="w-4 h-4 text-rt-primary" />
                <h2 className="text-sm font-display font-semibold">Upcoming Runs</h2>
              </div>
              {upcomingSchedules.length === 0 ? (
                <div className="text-center py-8">
                  <Calendar className="w-8 h-8 mx-auto mb-2 text-rt-text-muted/20" />
                  <p className="text-xs text-rt-text-muted">No scheduled runs</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {upcomingSchedules.map((s: any) => (
                    <div key={s.sop_id} className="flex items-center gap-3 px-3 py-2 rounded-lg bg-rt-surface/30">
                      <Timer className="w-4 h-4 text-blue-400 flex-shrink-0" />
                      <div className="flex-1 min-w-0">
                        <p className="text-xs font-medium truncate">{s.title}</p>
                        <p className="text-[10px] text-rt-text-muted">
                          {scheduleLabel(s.schedule_type, s.schedule_config)}
                        </p>
                      </div>
                      <span className="text-[10px] text-blue-400 flex-shrink-0 font-medium">
                        {s.next_run_at ? formatNextRun(s.next_run_at) : '—'}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </motion.div>

            {/* System Overview */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.45 }}
              className="card"
            >
              <div className="flex items-center gap-2 mb-5">
                <Zap className="w-4 h-4 text-rt-primary" />
                <h2 className="text-sm font-display font-semibold">System Overview</h2>
              </div>
              <div className="space-y-3">
                <SystemRow label="Products" value={stats?.total_products || 0} icon={FolderTree} color="text-rt-primary" />
                <SystemRow label="Total Automations" value={stats?.total_automations || 0} icon={ClipboardList} color="text-emerald-400" />
                <SystemRow label="Approved Docs" value={`${stats?.approved_docs || 0} / ${stats?.total_docs || 0}`} icon={FileText} color="text-purple-400" />
                <SystemRow label="Agent Conversations" value={stats?.total_conversations || 0} icon={MessageSquare} color="text-blue-400" />
                <SystemRow label="Execution Runs" value={stats?.total_runs || 0} icon={Play} color="text-amber-400" />
                <SystemRow label="Failed Runs" value={stats?.failed_runs || 0} icon={XCircle} color={stats?.failed_runs ? 'text-red-400' : 'text-rt-text-muted'} />
              </div>
            </motion.div>

            {/* Quick Actions */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.5 }}
              className="card"
            >
              <div className="flex items-center gap-2 mb-5">
                <Sparkles className="w-4 h-4 text-rt-primary" />
                <h2 className="text-sm font-display font-semibold">Quick Actions</h2>
              </div>
              <div className="space-y-2">
                <QuickAction to="/agent" icon={Bot} label="Start Agent Task" desc="Run an AI-powered task" color="text-blue-400" bg="bg-blue-500/10" />
                <QuickAction to="/products" icon={FolderTree} label="Create Product" desc="Set up a new knowledge product" color="text-rt-primary" bg="bg-rt-primary/10" />
                <QuickAction to="/sops" icon={ClipboardList} label="View Automations" desc="Manage scheduled workflows" color="text-emerald-400" bg="bg-emerald-500/10" />
                <QuickAction to="/docs" icon={FileText} label="Browse Documentation" desc="Knowledge articles library" color="text-purple-400" bg="bg-purple-500/10" />
                <QuickAction to="/pods" icon={Server} label="Manage PODs" desc="Distributed infrastructure" color="text-amber-400" bg="bg-amber-500/10" />
              </div>
            </motion.div>
          </div>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatCard({ label, value, sub, icon: Icon, color, bg, href, delay, ring }: {
  label: string; value: any; sub: string; icon: any;
  color: string; bg: string; href: string; delay: number; ring?: number
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay }}
    >
      <Link to={href} className="card !p-4 flex flex-col hover:border-rt-primary/30 transition-all group relative overflow-hidden">
        <div className="flex items-center justify-between mb-3">
          <div className={`w-9 h-9 rounded-lg ${bg} flex items-center justify-center`}>
            <Icon className={`w-4.5 h-4.5 ${color}`} />
          </div>
          {ring !== undefined && (
            <svg className="w-9 h-9" viewBox="0 0 36 36">
              <circle cx="18" cy="18" r="15" fill="none" stroke="currentColor" className="text-rt-surface" strokeWidth="3" />
              <circle cx="18" cy="18" r="15" fill="none" stroke="currentColor" className={color} strokeWidth="3"
                strokeDasharray={`${ring * 0.94} 100`}
                strokeLinecap="round" transform="rotate(-90 18 18)" />
            </svg>
          )}
        </div>
        <p className="text-2xl font-display font-bold leading-none mb-0.5">{value}</p>
        <p className="text-[11px] text-rt-text-muted">{label}</p>
        <p className="text-[10px] text-rt-text-muted/60 mt-0.5">{sub}</p>
        <ArrowRight className="w-3.5 h-3.5 text-rt-text-muted absolute bottom-3 right-3 opacity-0 group-hover:opacity-100 transition-opacity" />
      </Link>
    </motion.div>
  )
}

function SystemRow({ label, value, icon: Icon, color }: {
  label: string; value: any; icon: any; color: string
}) {
  return (
    <div className="flex items-center gap-3 px-3 py-2 rounded-lg bg-rt-surface/30">
      <Icon className={`w-4 h-4 ${color} flex-shrink-0`} />
      <span className="text-xs flex-1">{label}</span>
      <span className="text-sm font-semibold font-display">{value}</span>
    </div>
  )
}

function QuickAction({ to, icon: Icon, label, desc, color, bg }: {
  to: string; icon: any; label: string; desc: string; color: string; bg: string
}) {
  return (
    <Link to={to} className="flex items-center gap-3 px-3 py-2.5 rounded-lg hover:bg-rt-surface/40 transition-colors group">
      <div className={`w-8 h-8 rounded-lg ${bg} flex items-center justify-center flex-shrink-0`}>
        <Icon className={`w-4 h-4 ${color}`} />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-xs font-medium">{label}</p>
        <p className="text-[10px] text-rt-text-muted">{desc}</p>
      </div>
      <ChevronRight className="w-3.5 h-3.5 text-rt-text-muted opacity-0 group-hover:opacity-100 transition-opacity" />
    </Link>
  )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getGreeting(): string {
  const hour = new Date().getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 17) return 'Good afternoon'
  return 'Good evening'
}

function timeAgo(dateStr: string): string {
  const now = new Date()
  const date = new Date(dateStr)
  const diff = Math.floor((now.getTime() - date.getTime()) / 1000)
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`
  return date.toLocaleDateString()
}

function scheduleLabel(type: string, config?: Record<string, any>): string {
  if (!type || type === 'none') return ''
  if (type === 'once') return 'One-time'
  if (type === 'interval') return `Every ${config?.every || '?'} ${config?.unit || 'min'}`
  if (type === 'daily') return `Daily ${config?.time || ''}`
  if (type === 'weekly') return 'Weekly'
  if (type === 'monthly') return 'Monthly'
  if (type === 'cron') return 'Cron'
  return type
}

function formatNextRun(dateStr: string): string {
  const date = new Date(dateStr)
  const now = new Date()
  const diff = date.getTime() - now.getTime()
  if (diff < 0) return 'overdue'
  if (diff < 3600000) return `${Math.ceil(diff / 60000)}m`
  if (diff < 86400000) return `${Math.ceil(diff / 3600000)}h`
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
