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
          'bg':          'var(--rt-bg)',
          'bg-light':    'var(--rt-bg-light)',
          'bg-lighter':  'var(--rt-bg-lighter)',
          'surface':     'var(--rt-surface)',
          'border':      'var(--rt-border)',
          'text':        'var(--rt-text)',
          'text-muted':  'var(--rt-text-muted)',
          'primary':     'var(--rt-primary)',
          'primary-dark':'var(--rt-primary-dark)',
          'accent':      'var(--rt-accent)',
          'success':     'var(--rt-success)',
          'warning':     'var(--rt-warning)',
        }
      },
      fontFamily: {
        'display': ['Outfit', 'sans-serif'],
        'body': ['Source Sans 3', 'sans-serif'],
        'mono': ['JetBrains Mono', 'monospace'],
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'fade-in': 'fadeIn 0.3s ease-out',
        'slide-up': 'slideUp 0.3s ease-out',
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
      },
    },
  },
  plugins: [],
}
