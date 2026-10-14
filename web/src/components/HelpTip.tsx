import type { MouseEvent, ReactNode } from 'react'
import { CloseIcon, HelpIcon } from './Icons'

/**
 * The ? beside a heading. The panel it opens is rendered by the parent where
 * it belongs, below the heading rather than over the form, so a dialog never
 * stacks a second dialog to explain itself.
 */
export function HelpButton({ open, onToggle, label }: {
  open: boolean
  onToggle: () => void
  /** What it explains, for screen readers and the tooltip. */
  label: string
}) {
  return (
    <button type="button" className={`help-toggle${open ? ' is-open' : ''}`}
      aria-expanded={open} aria-label={label} title={label}
      // Inside a <summary>, a click would also fold the section.
      onClick={(event: MouseEvent) => { event.preventDefault(); event.stopPropagation(); onToggle() }}>
      <HelpIcon size={14} />
    </button>
  )
}

export function HelpPanel({ title, onClose, children }: {
  title: string
  onClose: () => void
  children: ReactNode
}) {
  return (
    <div className="help-panel" role="note" aria-label={title}>
      <div className="help-panel-head">
        <strong>{title}</strong>
        <button type="button" className="help-toggle" aria-label="Close help" onClick={onClose}>
          <CloseIcon size={13} />
        </button>
      </div>
      {children}
    </div>
  )
}
