import { focusManager } from '@tanstack/react-query'

/**
 * Pause React Query's background polling when the user clearly isn't using the
 * page, so an open-but-ignored tab stops generating traffic (which lets the Fly
 * dev machine auto-stop). The app counts as "focused" only when the tab is
 * visible AND the user interacted within `idleMs`; otherwise the refetchInterval
 * timers pause (they already respect focus). On return to focus, React Query
 * refetches active queries immediately (refetchOnWindowFocus), so no stale data.
 *
 * Purely an efficiency behavior — on always-on infra it just trims idle polling;
 * functionality is unchanged. (React Query already pauses polling when the tab
 * is hidden by default; this only adds the "visible but idle" case.)
 */
export function installIdleFocus(idleMs = 5 * 60 * 1000): void {
  if (typeof document === 'undefined') return

  focusManager.setEventListener((handleFocus) => {
    let last = Date.now()
    let timer: ReturnType<typeof setInterval> | undefined

    const focused = () =>
      document.visibilityState === 'visible' && Date.now() - last < idleMs

    const update = () => handleFocus(focused())

    const onActivity = () => {
      const wasIdle = Date.now() - last >= idleMs
      last = Date.now()
      if (wasIdle) update() // resume the moment the user comes back
    }

    const onVisibility = () => {
      if (document.visibilityState === 'visible') last = Date.now()
      update()
    }

    const events: Array<keyof DocumentEventMap> = [
      'mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll', 'wheel',
    ]
    events.forEach((e) => document.addEventListener(e, onActivity, { passive: true }))
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('focus', onActivity)
    timer = setInterval(update, 30_000) // become idle without needing an event
    update()

    return () => {
      events.forEach((e) => document.removeEventListener(e, onActivity))
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('focus', onActivity)
      if (timer) clearInterval(timer)
    }
  })
}
