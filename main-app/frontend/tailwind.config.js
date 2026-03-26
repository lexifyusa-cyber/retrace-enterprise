/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // All colors now reference CSS custom properties for theme switching
        'rt': {
          'bg':              'var(--rt-bg)',
          'bg-light':        'var(--rt-bg-light)',
          'bg-lighter':      'var(--rt-bg-lighter)',
          'surface':         'var(--rt-surface)',
          'border':          'var(--rt-border)',
          'text':            'var(--rt-text)',
          'text-muted':      'var(--rt-text-muted)',
          'primary':         'var(--rt-primary)',
          'primary-dark':    'var(--rt-primary-dark)',
          'primary-container':'var(--rt-primary-container)',
          'primary-fixed':   'var(--rt-primary-fixed)',
          'accent':          'var(--rt-accent)',
          'success':         'var(--rt-success)',
          'warning':         'var(--rt-warning)',
        },
        // Editorial design tokens
        'on-surface':         'var(--rt-text)',
        'on-surface-variant':  'var(--rt-on-surface-variant)',
        'outline-variant':     'var(--rt-outline-variant)',
        'surface-container-high': 'var(--rt-surface-container-high)',
        'surface-container-low':  'var(--rt-surface-container-low)',
        'primary-fixed':      'var(--rt-primary-fixed)',
      },
      fontFamily: {
        'headline': ['Noto Serif', 'serif'],
        'display': ['Noto Serif', 'serif'],
        'body': ['Inter', 'sans-serif'],
        'label': ['Inter', 'sans-serif'],
        'mono': ['JetBrains Mono', 'monospace'],
      },
      borderRadius: {
        'DEFAULT': '1rem',
        'lg': '1.5rem',
        'xl': '2rem',
        '2xl': '2rem',
        '3xl': '3rem',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'fade-in': 'fadeIn 0.3s ease-out',
        'slide-up': 'slideUp 0.3s ease-out',
        'lift': 'lift 0.2s ease-out',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(10px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        lift: {
          '0%': { transform: 'translateY(0)' },
          '100%': { transform: 'translateY(-4px)' },
        },
      },
    },
  },
  plugins: [],
}
