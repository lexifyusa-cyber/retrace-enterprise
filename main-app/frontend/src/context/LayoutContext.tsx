import { createContext, useContext, useState, useEffect, ReactNode } from 'react'

export type LayoutMode = 'classic' | 'command-center'
export type ThemeMode = 'dark' | 'light' | 'colorful'

interface LayoutContextType {
  layoutMode: LayoutMode
  setLayoutMode: (mode: LayoutMode) => void
  themeMode: ThemeMode
  setThemeMode: (mode: ThemeMode) => void
}

const LayoutContext = createContext<LayoutContextType>({
  layoutMode: 'classic',
  setLayoutMode: () => {},
  themeMode: 'dark',
  setThemeMode: () => {},
})

export function LayoutProvider({ children }: { children: ReactNode }) {
  const [layoutMode, setLayoutMode] = useState<LayoutMode>(() => {
    const saved = localStorage.getItem('rt-layout-mode')
    return (saved === 'command-center' ? 'command-center' : 'classic') as LayoutMode
  })

  const [themeMode, setThemeMode] = useState<ThemeMode>(() => {
    const saved = localStorage.getItem('rt-theme-mode')
    if (saved === 'light' || saved === 'colorful') return saved
    return 'dark'
  })

  useEffect(() => {
    localStorage.setItem('rt-layout-mode', layoutMode)
  }, [layoutMode])

  useEffect(() => {
    localStorage.setItem('rt-theme-mode', themeMode)
    // Apply theme to the document root
    document.documentElement.setAttribute('data-theme', themeMode)
  }, [themeMode])

  return (
    <LayoutContext.Provider value={{ layoutMode, setLayoutMode, themeMode, setThemeMode }}>
      {children}
    </LayoutContext.Provider>
  )
}

export const useLayout = () => useContext(LayoutContext)
