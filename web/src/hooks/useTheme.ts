import { useCallback, useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'lemondx-theme'

function initialTheme(): Theme {
  // index.html already resolved this before paint; trust the DOM, then storage.
  const attribute = document.documentElement.dataset.theme
  if (attribute === 'dark' || attribute === 'light') return attribute
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'dark' || stored === 'light') return stored
  } catch { /* storage can be blocked; fall through */ }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(initialTheme)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem(STORAGE_KEY, theme)
    } catch { /* ignore */ }
  }, [theme])

  // Follow the OS only while the user has not made an explicit choice.
  useEffect(() => {
    let explicit = false
    try {
      explicit = localStorage.getItem(STORAGE_KEY) !== null
    } catch { /* ignore */ }
    if (explicit) return

    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (event: MediaQueryListEvent) => setTheme(event.matches ? 'dark' : 'light')
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [])

  const toggle = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), [])

  return { theme, toggle }
}
