import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { LucideIcon } from 'lucide-react'

/**
 * Right-click context menu for messages. Mirrors OpenCode's native menu
 * surfaced via the `context-menu-*` data-slots in its web UI — the menu hosts
 * Copy / Copy response / Revert / Fork actions that don't fit as inline hover
 * buttons.
 *
 * The hover-revealed inline Copy + Revert in `UserMessage` stay; this menu is
 * the discoverable full-action surface (the same way OC keeps the per-bubble
 * Copy chip visible AND offers Fork-to-new-session in the right-click menu).
 *
 * Positioning rules:
 *   - Initial render uses (clientX, clientY) from the trigger event.
 *   - useLayoutEffect measures the rendered menu and clamps x/y so the menu
 *     never overflows the viewport (right edge / bottom edge).
 *   - Closes on click-outside (mousedown), Esc, blur, or any item click.
 */

export interface ContextMenuItem {
  label: string
  Icon: LucideIcon
  onClick: () => void | Promise<void>
  /** Disabled items render greyed and are not clickable. */
  disabled?: boolean
  /** Visually distinguishes destructive items (red). */
  danger?: boolean
  /** Inserts a divider line above this item. */
  separator?: boolean
}

interface Props {
  x: number
  y: number
  items: ContextMenuItem[]
  onClose: () => void
}

export default function MessageContextMenu({ x, y, items, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ left: x, top: y, opacity: 0 })

  // Clamp the menu to the viewport. We render once invisibly so we can measure
  // its real dimensions, then adjust and fade in.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const margin = 8
    let left = x
    let top = y
    if (left + rect.width + margin > window.innerWidth) {
      left = Math.max(margin, window.innerWidth - rect.width - margin)
    }
    if (top + rect.height + margin > window.innerHeight) {
      top = Math.max(margin, window.innerHeight - rect.height - margin)
    }
    setPos({ left, top, opacity: 1 })
  }, [x, y])

  // Dismissal: click anywhere outside the menu, Esc key, or blur.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    // mousedown fires before the click handler on the menu items, so we use
    // a capture-phase listener that no-ops if the target is inside the menu.
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    window.addEventListener('blur', onClose)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('blur', onClose)
    }
  }, [onClose])

  // Scroll-while-open closes — the menu was anchored to a pointer position
  // that no longer corresponds to its target.
  useEffect(() => {
    const onScroll = () => onClose()
    window.addEventListener('scroll', onScroll, { capture: true, passive: true })
    return () => window.removeEventListener('scroll', onScroll, { capture: true } as any)
  }, [onClose])

  return (
    <div
      ref={ref}
      role="menu"
      className="fixed z-[9999] min-w-[200px] bg-rt-bg-light border border-rt-border rounded-md shadow-xl py-1 select-none transition-opacity"
      style={{ left: pos.left, top: pos.top, opacity: pos.opacity }}
      onContextMenu={e => e.preventDefault()}
    >
      {items.map((it, i) => {
        const Icon = it.Icon
        return (
          <div key={i}>
            {it.separator && <div className="my-1 border-t border-rt-border/40" />}
            <button
              type="button"
              role="menuitem"
              disabled={it.disabled}
              onClick={() => {
                if (it.disabled) return
                it.onClick()
                onClose()
              }}
              className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left transition-colors ${
                it.disabled
                  ? 'text-rt-text-muted/40 cursor-not-allowed'
                  : it.danger
                    ? 'text-rt-text hover:bg-red-500/10 hover:text-red-300'
                    : 'text-rt-text hover:bg-rt-surface/60'
              }`}
            >
              <Icon className={`w-3.5 h-3.5 flex-shrink-0 ${it.disabled ? 'text-rt-text-muted/40' : it.danger ? 'text-red-400' : 'text-rt-text-muted/70'}`} />
              <span>{it.label}</span>
            </button>
          </div>
        )
      })}
    </div>
  )
}
