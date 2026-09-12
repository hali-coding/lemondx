import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'

interface Props {
  title: string
  subtitle?: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}

export function Modal({ title, subtitle, onClose, children, footer }: Props) {
  const box = useRef<HTMLDivElement>(null)

  // Callers build onClose inline, so its identity changes on every render.
  // Reading it through a ref keeps the effects below from re-running.
  const closeRef = useRef(onClose)
  useEffect(() => {
    closeRef.current = onClose
  }, [onClose])

  // Escape closes the dialog. Bound once, for the life of the dialog.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  // Move focus into the dialog once, when it opens. This must not depend on
  // anything that changes per render: re-running it mid-typing would yank the
  // caret back to the first field.
  useEffect(() => {
    box.current?.querySelector<HTMLElement>('input, select, textarea, button')?.focus()
  }, [])

  return (
    <div
      className="modal"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) closeRef.current()
      }}
    >
      <div className="modal-box" ref={box} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  )
}
