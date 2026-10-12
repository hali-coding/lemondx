import { useCallback, useRef, useState } from 'react'

export type ToastKind = 'success' | 'error' | 'info'

export interface Toast {
  id: number
  kind: ToastKind
  title: string
  detail?: string
}

/**
 * A standing notice: unlike a toast it is derived from state on every render
 * rather than pushed, so it lasts as long as the condition it reports unless
 * closed. `key` names one occurrence of the condition -- stable across polls,
 * but different the next time it happens (maintenance keys on its `since`), so
 * closing one never hides a later one.
 */
export interface Notice {
  key: string
  kind: 'warn' | 'error' | 'info'
  title: string
  detail?: string
  action?: { label: string; onClick: () => void }
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

/** Which notices are still showing: closing hides those keys for this page load. */
export function useNotices(notices: Notice[]) {
  const [closed, setClosed] = useState<ReadonlySet<string>>(() => new Set())
  const visible = notices.filter((n) => !closed.has(n.key))
  const close = useCallback((keys: string[]) => {
    setClosed((current) => new Set([...current, ...keys]))
  }, [])
  return { visible, close }
}
