import { useQuery } from '@tanstack/react-query'
import {
  FolderTree, Activity, ArrowRight, Bot,
  CheckCircle2, XCircle, Loader2,
  Zap, TrendingUp, Calendar,
  Sparkles, RefreshCw, Share2, SlidersHorizontal,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { agentApi } from '@/utils/api'
import { motion } from 'framer-motion'
import { useAuth } from '@/context/AuthContext'

export default function Dashboard() {
  const { data, isLoading } = useQuery({
    queryKey: ['dashboard-stats'],
    queryFn: () => agentApi.getDashboardStats(),
    refetchInterval: 30000,
  })

  const { user } = useAuth()
  const stats = data?.stats
  const recentRuns = data?.recent_runs || []
  const upcomingSchedules = data?.upcoming_schedules || []
  const productCoverage = data?.product_coverage || []

  const greeting = getGreeting()
  const firstName = user?.display_name?.split(' ')[0] || user?.email?.split('@')[0] || ''

  return (
    <div className="px-12 pb-20 pt-8">
      {/* Hero Header — Editorial Style */}
      <motion.section
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        className="mb-12"
      >
        <h1 className="text-5xl font-headline font-bold tracking-tight mb-4">
          {greeting}, <span className="text-rt-primary-container italic">{firstName}.</span>
        </h1>
        <p className="text-on-surface-variant text-lg max-w-2xl leading-relaxed">
          {stats
            ? <>Your digital curation engine is running at <span className="font-bold text-rt-primary">{stats.execution_success_rate || 100}% efficiency</span>. Here is what has evolved since your last session.</>
            : 'Loading your workspace...'}
        </p>
      </motion.section>

      {isLoading ? (
        <div className="flex items-center justify-center py-20 gap-3 text-rt-text-muted">
          <Loader2 className="w-5 h-5 animate-spin" />
          <span className="font-body text-sm">Loading dashboard...</span>
        </div>
      ) : (
        <>
          {/* ============================================================
              ROW 1: Hero Stat Cards — Editorial Style
              ============================================================ */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-8 mb-16">
            <StatCard
              label="Total Artifacts"
              value={stats?.total_docs || 0}
              icon={<Sparkles className="w-5 h-5 text-rt-primary" />}
              badge={stats?.docs_this_week ? `+${stats.docs_this_week}` : undefined}
              badgeColor="text-green-700 bg-green-50"
              href="/docs"
              delay={0}
            />
            <StatCard
              label="Active Agents"
              value={stats?.active_automations || 0}
              icon={<Bot className="w-5 h-5 text-rt-primary" />}
              badge="Active"
              badgeColor="text-rt-primary bg-rt-primary-fixed/20"
              href="/sops"
              delay={0.05}
            />
            <StatCard
              label="Knowledge Groups"
              value={`${stats?.trained_groups || 0}/${stats?.total_groups || 0}`}
              icon={<FolderTree className="w-5 h-5 text-rt-primary" />}
              badge={stats?.knowledge_coverage_pct === 100 ? 'Complete' : 'Healthy'}
              badgeColor="text-rt-primary bg-rt-primary-fixed/20"
              href="/products"
              delay={0.1}
            />
            <StatCard
              label="Success Rate"
              value={`${stats?.execution_success_rate || 100}%`}
              icon={<TrendingUp className="w-5 h-5 text-rt-primary" />}
              badge="Optimized"
              badgeColor="text-rt-primary bg-rt-primary-fixed/20"
              href="/sops"
              delay={0.15}
            />
          </div>

          {/* ============================================================
              ROW 2: Asymmetric Layout — Activity + Knowledge Coverage
              ============================================================ */}
          <div className="grid grid-cols-12 gap-8 mb-16">
            {/* Automation Activity (Large Column) */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.2 }}
              className="col-span-12 lg:col-span-8 card p-10"
            >
              <div className="flex justify-between items-end mb-8">
                <div>
                  <h3 className="text-2xl font-headline font-bold mb-2">Automation Activity</h3>
                  <p className="text-on-surface-variant text-sm">Synthetic workload distribution over the last 24 hours.</p>
                </div>
                <Link to="/sops" className="text-rt-primary font-bold text-sm flex items-center gap-1 hover:gap-2 transition-all">
                  View Report <ArrowRight className="w-4 h-4" />
                </Link>
              </div>
              {recentRuns.length === 0 ? (
                <div className="text-center py-16">
                  <Activity className="w-10 h-10 mx-auto mb-3 text-rt-text-muted/20" />
                  <p className="text-sm text-rt-text-muted">No automation runs yet</p>
                  <Link to="/sops" className="text-sm text-rt-primary hover:underline mt-2 inline-block font-semibold">Create your first automation</Link>
                </div>
              ) : (
                <div className="space-y-1 max-h-[320px] overflow-y-auto pr-1">
                  {recentRuns.map((run: any) => (
                    <div key={run.run_id} className="flex items-center gap-4 px-4 py-3 rounded-xl hover:bg-rt-bg-lighter transition-colors group">
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
                        <p className="text-sm font-semibold truncate">{run.sop_title}</p>
                        <p className="text-xs text-rt-text-muted">
                          {run.trigger === 'manual' ? 'Manual' : 'Scheduled'}
                          {' · '}{run.steps_completed}/{run.steps_total} steps
                          {run.duration_ms ? ` · ${(run.duration_ms / 1000).toFixed(1)}s` : ''}
                        </p>
                      </div>
                      <span className="text-xs text-rt-text-muted/60 flex-shrink-0 italic">
                        {run.started_at ? timeAgo(run.started_at) : ''}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </motion.div>

            {/* Knowledge Coverage (Amber Feature Card) */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.25 }}
              className="col-span-12 lg:col-span-4 bg-rt-primary-container rounded-2xl p-10 text-[#2a1700] flex flex-col relative overflow-hidden editorial-shadow"
            >
              <div className="relative z-10">
                <h3 className="text-2xl font-headline font-bold mb-4">Knowledge <span className="italic">Coverage</span></h3>
                <p className="text-[#2a1700]/70 text-sm leading-relaxed mb-8">
                  {productCoverage.length > 0
                    ? `Your digital library spans ${productCoverage.length} product${productCoverage.length === 1 ? '' : 's'}. Expand your reach today.`
                    : 'Start by creating products to build your knowledge base.'}
                </p>
                <div className="space-y-5">
                  {productCoverage.slice(0, 3).map((p: any) => (
                    <div key={p.product_id} className="space-y-2">
                      <div className="flex justify-between text-xs font-bold uppercase tracking-wider">
                        <span className="truncate mr-2">{p.product_name}</span>
                        <span>{p.coverage_pct}%</span>
                      </div>
                      <div className="w-full bg-white/20 h-1 rounded-full">
                        <div className="bg-[#2a1700] h-full rounded-full transition-all duration-700"
                          style={{ width: `${p.coverage_pct}%` }} />
                      </div>
                    </div>
                  ))}
                  {productCoverage.length === 0 && (
                    <p className="text-xs text-[#2a1700]/50 italic">No products created yet</p>
                  )}
                </div>
                <Link to="/products" className="mt-10 bg-[#2a1700] text-white px-6 py-3 rounded-full font-bold text-sm w-full hover:bg-rt-primary transition-all active:scale-95 inline-block text-center">
                  Expand Curations
                </Link>
              </div>
              {/* Decorative blur */}
              <div className="absolute -bottom-10 -right-10 w-40 h-40 bg-white/10 rounded-full blur-3xl"></div>
            </motion.div>
          </div>

          {/* ============================================================
              ROW 3: Bottom Three Columns
              ============================================================ */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
            {/* Upcoming Scheduled Runs */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.3 }}
              className="card p-8"
            >
              <h4 className="font-headline font-bold text-xl mb-6 flex items-center gap-2">
                <Calendar className="w-5 h-5 text-rt-primary" />
                Upcoming Runs
              </h4>
              {upcomingSchedules.length === 0 ? (
                <div className="text-center py-8">
                  <Calendar className="w-8 h-8 mx-auto mb-2 text-rt-text-muted/20" />
                  <p className="text-sm text-rt-text-muted">No scheduled runs</p>
                </div>
              ) : (
                <div className="space-y-5">
                  {upcomingSchedules.map((s: any) => (
                    <div key={s.sop_id} className="flex items-start gap-4">
                      <div className="mt-1.5 w-2 h-2 rounded-full bg-rt-primary-container flex-shrink-0"></div>
                      <div>
                        <p className="text-sm font-bold">{s.title}</p>
                        <p className="text-xs text-rt-text-muted italic">
                          {s.next_run_at ? `Scheduled for ${formatNextRun(s.next_run_at)}` : scheduleLabel(s.schedule_type, s.schedule_config)}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </motion.div>

            {/* System Overview */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.35 }}
              className="card p-8"
            >
              <h4 className="font-headline font-bold text-xl mb-6 flex items-center gap-2">
                <Zap className="w-5 h-5 text-rt-primary" />
                System Overview
              </h4>
              <div className="space-y-0">
                <SystemRow label="Products" value={stats?.total_products || 0} />
                <SystemRow label="Total Automations" value={stats?.total_automations || 0} />
                <SystemRow label="Approved Docs" value={`${stats?.approved_docs || 0} / ${stats?.total_docs || 0}`} />
                <SystemRow label="Agent Conversations" value={stats?.total_conversations || 0} />
                <SystemRow label="Execution Runs" value={stats?.total_runs || 0} />
                <SystemRow label="Failed Runs" value={stats?.failed_runs || 0} highlight={!!stats?.failed_runs} />
              </div>
            </motion.div>

            {/* Quick Actions */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.4 }}
              className="card p-8"
            >
              <h4 className="font-headline font-bold text-xl mb-6">Quick Actions</h4>
              <div className="grid grid-cols-2 gap-4">
                <QuickActionButton to="/agent" icon={<Bot className="w-5 h-5 text-rt-primary" />} label="Add Task" />
                <QuickActionButton to="/sops" icon={<RefreshCw className="w-5 h-5 text-rt-primary" />} label="Sync All" />
                <QuickActionButton to="/docs" icon={<Share2 className="w-5 h-5 text-rt-primary" />} label="Export" />
                <QuickActionButton to="/settings" icon={<SlidersHorizontal className="w-5 h-5 text-rt-primary" />} label="Tune AI" />
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

function StatCard({ label, value, icon, badge, badgeColor, href, delay }: {
  label: string; value: any; icon: React.ReactNode;
  badge?: string; badgeColor?: string; href: string; delay: number
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay }}
    >
      <Link to={href} className="card !p-8 flex flex-col hover:-translate-y-1 transition-all duration-300 group relative overflow-hidden">
        <div className="flex items-center justify-between mb-6">
          <div className="icon-orb">
            {icon}
          </div>
          {badge && (
            <span className={`text-[10px] font-bold px-2.5 py-1 rounded-full ${badgeColor || ''}`}>
              {badge}
            </span>
          )}
        </div>
        <p className="text-3xl font-headline font-bold mb-1">{value}</p>
        <p className="text-xs text-on-surface-variant uppercase tracking-[0.15em] font-bold">{label}</p>
      </Link>
    </motion.div>
  )
}

function SystemRow({ label, value, highlight }: {
  label: string; value: any; highlight?: boolean
}) {
  return (
    <div className="flex justify-between items-center py-3 border-b border-outline-variant/10 last:border-b-0">
      <span className="text-sm text-on-surface-variant">{label}</span>
      <span className={`text-sm font-bold ${highlight ? 'text-red-500' : ''}`}>{value}</span>
    </div>
  )
}

function QuickActionButton({ to, icon, label }: {
  to: string; icon: React.ReactNode; label: string
}) {
  return (
    <Link to={to} className="flex flex-col items-center justify-center p-4 rounded-xl bg-rt-bg-lighter hover:bg-rt-primary-fixed/30 transition-colors group">
      <div className="mb-2">{icon}</div>
      <span className="text-[10px] font-bold uppercase tracking-[0.15em] text-rt-text-muted group-hover:text-rt-primary">{label}</span>
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
