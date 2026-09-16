import { useRef, useState } from 'react'
import { api } from '../lib/api'
import type { AutoSync } from '../lib/types'
import { Modal } from './Modal'

interface Props {
  onCancel: () => void
  onUploaded: (id: string, synced?: AutoSync | null) => void
}

const EXAMPLE = `#!/bin/sh
# name: Redis
# description: Install Redis and start it.
# order: 60
# param: REDIS_PORT=6379  Port to listen on

pkg_install redis
svc_enable redis
log "redis listening on \${REDIS_PORT}"
`

export function ModuleUploadDialog({ onCancel, onUploaded }: Props) {
  const [name, setName] = useState('')
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [needsOverwrite, setNeedsOverwrite] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  function pickFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    if (!file) return
    // Read locally; only the text is sent to the server.
    const reader = new FileReader()
    reader.onload = () => {
      setContent(String(reader.result ?? ''))
      if (!name.trim()) setName(file.name.replace(/\.sh$/i, ''))
      setError(null)
    }
    reader.onerror = () => setError('Could not read that file.')
    reader.readAsText(file)
  }

  async function submit(overwrite = false) {
    if (!name.trim() || !content.trim()) return
    setBusy(true)
    setError(null)
    try {
      const module = await api.uploadModule(name.trim(), content, overwrite)
      onUploaded(module.id, module.synced)
    } catch (cause) {
      const message = (cause as Error).message
      setError(message)
      setNeedsOverwrite(/already exists|built-in/i.test(message))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Upload a module"
      subtitle="A POSIX shell script that runs inside containers you select it for."
      onClose={busy ? () => {} : onCancel}
      footer={
        <>
          <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          {needsOverwrite && (
            <button className="btn btn-danger" onClick={() => submit(true)} disabled={busy}>
              Replace existing
            </button>
          )}
          <button className="btn btn-primary" onClick={() => submit(false)}
            disabled={busy || !name.trim() || !content.trim()}>
            {busy && <span className="spinner" />}
            Upload
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor="m-name">Name</label>
        <input
          id="m-name"
          className="input"
          value={name}
          placeholder="redis"
          autoComplete="off"
          disabled={busy}
          onChange={(event) => setName(event.target.value)}
        />
        <span className="hint">
          Becomes the module id. Lower-case letters, digits, dashes and dots.
        </span>
      </div>

      <div className="field">
        <label htmlFor="m-body">
          Script
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            style={{ marginLeft: 8 }}
            disabled={busy}
            onClick={() => fileInput.current?.click()}
          >
            Choose a .sh file…
          </button>
          {!content && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={busy}
              onClick={() => setContent(EXAMPLE)}
            >
              Start from an example
            </button>
          )}
        </label>
        <input
          ref={fileInput}
          type="file"
          accept=".sh,text/plain,application/x-shellscript"
          hidden
          onChange={pickFile}
        />
        <textarea
          id="m-body"
          className="input mono module-source"
          rows={14}
          value={content}
          spellCheck={false}
          disabled={busy}
          placeholder={EXAMPLE}
          onChange={(event) => setContent(event.target.value)}
        />
        <span className="hint">
          Runs under <span className="mono">/bin/sh</span> with the prelude's helpers
          (<span className="mono">pkg_install</span>, <span className="mono">svc_enable</span>,{' '}
          <span className="mono">install_ssh_keys</span>). The syntax is checked on upload.
        </span>
      </div>

      {error && (
        <div className="banner banner-error" style={{ margin: 0, padding: '10px 12px' }}>
          <div className="banner-body"><p style={{ margin: 0 }}>{error}</p></div>
        </div>
      )}
    </Modal>
  )
}
