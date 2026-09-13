import { useEffect, useState } from 'react'
import { CheckIcon, CopyIcon } from './Icons'

interface Props {
  text: string
  /** What is being copied, for the accessible label, e.g. "IP address of web-1". */
  label: string
}

async function copyText(text: string) {
  // The async API only exists in a secure context, and `serve --host` on a LAN
  // address over plain HTTP is not one -- so fall back to the old selection copy.
  if (window.isSecureContext && navigator.clipboard) {
    await navigator.clipboard.writeText(text)
    return
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  try {
    if (!document.execCommand('copy')) throw new Error('Copy was refused')
  } finally {
    area.remove()
  }
}

/** A small icon button that copies `text` and briefly confirms it did. */
export function CopyButton({ text, label }: Props) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')

  useEffect(() => {
    if (state === 'idle') return
    const timer = window.setTimeout(() => setState('idle'), 1500)
    return () => window.clearTimeout(timer)
  }, [state])

  return (
    <button
      type="button"
      className={`copy-btn${state === 'copied' ? ' copy-btn-done' : ''}`}
      title={state === 'copied' ? 'Copied' : state === 'failed' ? 'Could not copy' : 'Copy'}
      aria-label={`Copy ${label}`}
      onClick={(event) => {
        // Inside a table row, a click would otherwise open the container.
        event.stopPropagation()
        copyText(text).then(() => setState('copied'), () => setState('failed'))
      }}
      onKeyDown={(event) => event.stopPropagation()}
    >
      {state === 'copied' ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
    </button>
  )
}
