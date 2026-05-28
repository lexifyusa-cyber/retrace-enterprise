import { useEffect, useRef, useState } from 'react'

/**
 * React-hook companion to `installIdleFocus` (which only covers React Query):
 * returns true only while the tab is visible AND the user interacted within
 * `idleMs`. Use it to pause manual pollers and live WebSockets when the Agent
 * page is open but idle, so the scale-to-zero backend can actually suspend.
 * Resumes immediately on return (visibility change / interaction).
 */
export function usePageActive(idleMs = 60_000): boolean {
  const [active, setActive] = useState<boolean>(
    () => typeof document === 'undefined' || document.visibilityState === 'visible'
  )
  const last = useRef(Date.now())

  useEffect(() => {
    const focused = () =>
      document.visibilityState === 'visible' && Date.now() - last.current < idleMs
    const update = () => setActive(focused())
    const onActivity = () => {
      const wasIdle = Date.now() - last.current >= idleMs
      last.current = Date.now()
      if (wasIdle) update()
    }
    const onVisibility = () => {
      if (document.visibilityState === 'visible') last.current = Date.now()
      update()
    }
    const events: Array<keyof DocumentEventMap> = [
      'mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll', 'wheel',
    ]
    events.forEach((e) => document.addEventListener(e, onActivity, { passive: true }))
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('focus', onActivity)
    const timer = setInterval(update, 15_000)
    update()
    return () => {
      events.forEach((e) => document.removeEventListener(e, onActivity))
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('focus', onActivity)
      clearInterval(timer)
    }
  }, [idleMs])

  return active
}
