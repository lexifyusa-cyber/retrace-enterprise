import { useState } from 'react'
import { CalendarClock, CheckCircle2, Loader2 } from 'lucide-react'

const COMMON_TIMEZONES = [
  'UTC', 'US/Eastern', 'US/Central', 'US/Mountain', 'US/Pacific',
  'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Asia/Tokyo',
  'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dubai', 'Australia/Sydney',
  'Pacific/Auckland', 'America/Sao_Paulo', 'America/Chicago',
  'America/Los_Angeles', 'America/New_York', 'Canada/Eastern',
]

export default function SchedulePicker({ onConfirm, onCancel, isApproving }: {
  onConfirm: (scheduleType: string, scheduleConfig?: Record<string, any>) => void
  onCancel: () => void
  isApproving: boolean
}) {
  const [scheduleType, setScheduleType] = useState('none')
  const [timezone, setTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone)
  const [intervalEvery, setIntervalEvery] = useState(30)
  const [intervalUnit, setIntervalUnit] = useState('minutes')
  const [dailyTime, setDailyTime] = useState('09:00')
  const [weeklyDays, setWeeklyDays] = useState<string[]>(['monday'])
  const [weeklyTime, setWeeklyTime] = useState('09:00')
  const [monthlyDay, setMonthlyDay] = useState(1)
  const [monthlyTime, setMonthlyTime] = useState('09:00')
  const [onceDate, setOnceDate] = useState('')
  const [cronExpr, setCronExpr] = useState('0 9 * * 1-5')

  const toggleDay = (day: string) => {
    setWeeklyDays(prev => prev.includes(day) ? prev.filter(d => d !== day) : [...prev, day])
  }

  const buildConfig = (): Record<string, any> | undefined => {
    const tz = timezone
    switch (scheduleType) {
      case 'once': return { run_at: onceDate || new Date(Date.now() + 3600000).toISOString(), timezone: tz }
      case 'interval': return { every: intervalEvery, unit: intervalUnit, timezone: tz }
      case 'daily': return { time: dailyTime, timezone: tz }
      case 'weekly': return { days: weeklyDays, time: weeklyTime, timezone: tz }
      case 'monthly': return { day_of_month: monthlyDay, time: monthlyTime, timezone: tz }
      case 'cron': return { expression: cronExpr, timezone: tz }
      default: return undefined
    }
  }

  const scheduleOptions = [
    { value: 'none', label: 'No Schedule', desc: 'Approve without scheduling', icon: '✓' },
    { value: 'once', label: 'One-Time', desc: 'Run once at a specific time', icon: '📅' },
    { value: 'interval', label: 'Interval', desc: 'Run every N minutes/hours/days', icon: '⏱' },
    { value: 'daily', label: 'Daily', desc: 'Run every day at a set time', icon: '🔄' },
    { value: 'weekly', label: 'Weekly', desc: 'Run on specific days of the week', icon: '📆' },
    { value: 'monthly', label: 'Monthly', desc: 'Run on a specific day each month', icon: '🗓' },
    { value: 'cron', label: 'Custom (Cron)', desc: 'Advanced cron expression', icon: '⚙' },
  ]

  const allDays = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

  return (
    <div className="w-full">
      <div className="flex items-center gap-2 mb-3">
        <CalendarClock className="w-4 h-4 text-emerald-400" />
        <span className="text-sm font-semibold text-emerald-400">Approve & Schedule</span>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5 mb-3">
        {scheduleOptions.map(opt => (
          <button
            key={opt.value}
            type="button"
            onClick={() => setScheduleType(opt.value)}
            className={`p-2 rounded-lg border text-left transition-all text-xs ${
              scheduleType === opt.value
                ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400'
                : 'border-rt-border hover:border-rt-text-muted/30 text-rt-text-muted'
            }`}
          >
            <div className="flex items-center gap-1.5 mb-0.5">
              <span>{opt.icon}</span>
              <span className="font-medium">{opt.label}</span>
            </div>
            <p className="text-[10px] opacity-70 leading-tight">{opt.desc}</p>
          </button>
        ))}
      </div>

      {scheduleType === 'once' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Run at:</label>
          <input type="datetime-local" value={onceDate} onChange={e => setOnceDate(e.target.value)}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500 flex-1" />
        </div>
      )}

      {scheduleType === 'interval' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Every</label>
          <input type="number" min={1} value={intervalEvery} onChange={e => setIntervalEvery(Number(e.target.value))}
            className="w-16 bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs text-center focus:outline-none focus:border-emerald-500" />
          <select value={intervalUnit} onChange={e => setIntervalUnit(e.target.value)}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500">
            <option value="minutes">Minutes</option>
            <option value="hours">Hours</option>
            <option value="days">Days</option>
          </select>
        </div>
      )}

      {scheduleType === 'daily' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Every day at:</label>
          <input type="time" value={dailyTime} onChange={e => setDailyTime(e.target.value)}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500" />
        </div>
      )}

      {scheduleType === 'weekly' && (
        <div className="mb-3">
          <div className="flex flex-wrap gap-1 mb-2">
            {allDays.map(day => (
              <button key={day} type="button" onClick={() => toggleDay(day)}
                className={`px-2 py-1 rounded-md text-[10px] font-medium transition-colors ${
                  weeklyDays.includes(day)
                    ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                    : 'bg-rt-surface text-rt-text-muted border border-rt-border hover:border-rt-text-muted/30'
                }`}>
                {day.charAt(0).toUpperCase() + day.slice(1, 3)}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-rt-text-muted">At:</label>
            <input type="time" value={weeklyTime} onChange={e => setWeeklyTime(e.target.value)}
              className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500" />
          </div>
        </div>
      )}

      {scheduleType === 'monthly' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Day</label>
          <input type="number" min={1} max={28} value={monthlyDay} onChange={e => setMonthlyDay(Number(e.target.value))}
            className="w-14 bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs text-center focus:outline-none focus:border-emerald-500" />
          <label className="text-xs text-rt-text-muted">at</label>
          <input type="time" value={monthlyTime} onChange={e => setMonthlyTime(e.target.value)}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500" />
        </div>
      )}

      {scheduleType === 'cron' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Cron:</label>
          <input type="text" value={cronExpr} onChange={e => setCronExpr(e.target.value)}
            placeholder="0 9 * * 1-5"
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500 flex-1 font-mono" />
          <span className="text-[10px] text-rt-text-muted/60">min hr dom mon dow</span>
        </div>
      )}

      {scheduleType !== 'none' && (
        <div className="flex items-center gap-2 mb-3">
          <label className="text-xs text-rt-text-muted">Timezone:</label>
          <select value={timezone} onChange={e => setTimezone(e.target.value)}
            className="bg-rt-surface border border-rt-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-emerald-500">
            {COMMON_TIMEZONES.map(tz => <option key={tz} value={tz}>{tz}</option>)}
          </select>
        </div>
      )}

      <div className="flex items-center gap-2">
        <button
          onClick={() => onConfirm(scheduleType, buildConfig())}
          disabled={isApproving}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25 transition-colors disabled:opacity-50"
        >
          {isApproving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
          {scheduleType === 'none' ? 'Approve' : 'Approve & Schedule'}
        </button>
        <button
          onClick={onCancel}
          disabled={isApproving}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-rt-text-muted hover:bg-rt-surface transition-colors"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

export function scheduleLabel(scheduleType?: string, scheduleConfig?: Record<string, any>): string {
  if (!scheduleType || scheduleType === 'none') return 'No schedule'
  switch (scheduleType) {
    case 'once': return `One-time at ${scheduleConfig?.run_at ? new Date(scheduleConfig.run_at).toLocaleString() : 'TBD'}`
    case 'interval': return `Every ${scheduleConfig?.every || 30} ${scheduleConfig?.unit || 'minutes'}`
    case 'daily': return `Daily at ${scheduleConfig?.time || '09:00'}`
    case 'weekly': return `Weekly on ${(scheduleConfig?.days || []).join(', ')} at ${scheduleConfig?.time || '09:00'}`
    case 'monthly': return `Monthly on day ${scheduleConfig?.day_of_month || 1} at ${scheduleConfig?.time || '09:00'}`
    case 'cron': return `Cron: ${scheduleConfig?.expression || '?'}`
    default: return scheduleType
  }
}
