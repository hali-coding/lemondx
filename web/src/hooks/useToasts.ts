import { useCallback, useRef, useState } from 'react'

export type ToastKind = 'success' | 'error' | 'info'

export interface Toast {
  id: number
  kind: ToastKind
  title: string
  detail?: string
}

const LIFETIME: Record<ToastKind, number> = {
  success: 3200,
  info: 3800,
  error: 7000,
}

export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id))
  }, [])

  const push = useCallback((kind: ToastKind, title: string, detail?: string) => {
    const id = nextId.current++
    setToasts((current) => [...current, { id, kind, title, detail }])
    window.setTimeout(() => dismiss(id), LIFETIME[kind])
    return id
  }, [dismiss])

  return { toasts, push, dismiss }
}
