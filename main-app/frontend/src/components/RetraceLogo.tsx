/**
 * ReTrace brand — amber arrow mark + "ReTrace" wordmark.
 * Matches the reference: ▶ ReTrace
 */

type Variant = 'icon' | 'sm' | 'md' | 'lg'

const cfg = {
  icon: { arrow: 20, text: 0,  gap: 0  },
  sm:   { arrow: 16, text: 15, gap: 6  },
  md:   { arrow: 20, text: 18, gap: 7  },
  lg:   { arrow: 32, text: 30, gap: 10 },
} as const

export default function RetraceLogo({ variant = 'md' }: { variant?: Variant }) {
  const c = cfg[variant]

  return (
    <div className="flex items-center flex-shrink-0" style={{ gap: c.gap }}>
      {/* ── Amber arrow mark ── */}
      <svg
        width={c.arrow}
        height={c.arrow}
        viewBox="0 0 40 40"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        className="flex-shrink-0"
      >
        <defs>
          <linearGradient id={`arr-${variant}`} x1="0" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse">
            <stop stopColor="#f59e0b" />
            <stop offset="1" stopColor="#d97706" />
          </linearGradient>
        </defs>
        {/* Right-pointing chevron > */}
        <path
          d="M10 6 L30 20 L10 34"
          stroke={`url(#arr-${variant})`}
          strokeWidth="7"
          strokeLinecap="round"
          strokeLinejoin="round"
          fill="none"
        />
      </svg>

      {/* ── "ReTrace" wordmark ── */}
      {c.text > 0 && (
        <span
          className="font-headline font-bold tracking-tight text-gray-900 dark:text-rt-text"
          style={{ fontSize: c.text, lineHeight: 1, letterSpacing: '-0.02em' }}
        >
          ReTrace
        </span>
      )}
    </div>
  )
}
