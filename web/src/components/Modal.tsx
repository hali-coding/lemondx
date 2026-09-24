import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { ApiCall } from '../lib/api'
import { ApiExplorer } from './ApiExplorer'
import { CodeIcon } from './Icons'

interface Props {
  title: string
  subtitle?: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  /**
   * The request the dialog's main button would make, for the API explorer.
   * Leave it out for a dialog that makes none; null means "not yet known",
   * which still offers the explorer so it does not appear mid-typing.
   */
  api?: ApiCall | null
}

// Open or closed follows the person from dialog to dialog: someone reading
// the API wants it on every form, and nobody else wants it on any.
const EXPLORER_KEY = 'lemondx-api-explorer'

function storedExplorer() {
  try {
    return localStorage.getItem(EXPLORER_KEY) === 'open'
  } catch {
    return false
  }
}

export function Modal({ title, subtitle, onClose, children, footer, api }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const [explorer, setExplorer] = useState(storedExplorer)
  const showExplorer = explorer && api !== undefined

  function toggleExplorer() {
    setExplorer((open) => {
      try {
        localStorage.setItem(EXPLORER_KEY, open ? 'closed' : 'open')
      } catch { /* this dialog only, then */ }
      return !open
    })
  }

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
    // The form's first field, not the explorer toggle in the header.
    box.current?.querySelector<HTMLElement>(
      '.modal-body :is(input, select, textarea, button), .modal-foot button')?.focus()
  }, [])

  return (
    <div
      className="modal"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) closeRef.current()
      }}
    >
      <div className={`modal-box${showExplorer ? ' modal-box-api' : ''}`} ref={box}
        role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <div className="modal-title">
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          {api !== undefined && (
            <button type="button" className="btn btn-ghost btn-sm modal-api-toggle"
              aria-pressed={explorer} onClick={toggleExplorer}
              title={explorer ? 'Hide the API explorer'
                : 'Show how to make this request from curl, JavaScript or Python'}>
              <CodeIcon size={13} />API
            </button>
          )}
        </div>
        {/* One tree either way: swapping wrappers would remount the form
            and lose everything typed into it. */}
        <div className={showExplorer ? 'modal-main modal-split' : 'modal-main'}>
          <div className="modal-body">{children}</div>
          {showExplorer && <ApiExplorer call={api} />}
        </div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  )
}
