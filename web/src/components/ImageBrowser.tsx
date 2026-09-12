import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import { bytes } from '../lib/format'
import type { ImageBrowse, RemoteImage } from '../lib/types'

interface Props {
  value: string
  onPick: (alias: string) => void
  onBack: () => void
  disabled?: boolean
  /** Show VM images as available rather than container ones. */
  forVm?: boolean
}

/** Search the remotes' full catalogs, with already-downloaded images marked. */
export function ImageBrowser({ value, onPick, onBack, disabled, forVm }: Props) {
  const [data, setData] = useState<ImageBrowse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [remote, setRemote] = useState('')
  const [downloadedOnly, setDownloadedOnly] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setError(null)
    api.browseImages({}, controller.signal)
      .then(setData)
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
      })
    return () => controller.abort()
  }, [])

  const isCached = (image: RemoteImage) => (forVm ? image.cached_vm : image.cached)

  const matches = useMemo(() => {
    if (!data) return []
    const needle = query.trim().toLowerCase()
    return data.entries.filter((image) => {
      if (remote && image.remote !== remote) return false
      if (downloadedOnly && !isCached(image)) return false
      if (forVm && !image.vm_fingerprint) return false
      if (!needle) return true
      return (
        image.full_alias.toLowerCase().includes(needle) ||
        image.os.toLowerCase().includes(needle) ||
        image.release.toLowerCase().includes(needle) ||
        image.release_title.toLowerCase().includes(needle) ||
        image.label.toLowerCase().includes(needle) ||
        image.variant.toLowerCase().includes(needle)
      )
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, query, remote, downloadedOnly, forVm])

  const downloadedCount = useMemo(
    () => (data ? data.entries.filter(isCached).length : 0),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data, forVm],
  )

  return (
    <div className="image-browser">
      <div className="browser-controls">
        <input
          className="input"
          value={query}
          placeholder="Search — debian, 24.04, alpine…"
          aria-label="Search images"
          autoComplete="off"
          disabled={disabled}
          onChange={(event) => setQuery(event.target.value)}
        />
        {data && data.remotes.length > 1 && (
          <select
            className="select browser-remote"
            value={remote}
            aria-label="Filter by remote"
            disabled={disabled}
            onChange={(event) => setRemote(event.target.value)}
          >
            <option value="">All remotes</option>
            {data.browsed.map((name) => (
              <option key={name} value={name}>{name}:</option>
            ))}
          </select>
        )}
      </div>

      <div className="browser-meta">
        <label className="checkbox">
          <input
            type="checkbox"
            checked={downloadedOnly}
            disabled={disabled}
            onChange={(event) => setDownloadedOnly(event.target.checked)}
          />
          Downloaded only
          {downloadedCount > 0 && <span className="badge badge-ok">{downloadedCount}</span>}
        </label>
        <span className="faint">
          {data ? `${matches.length} of ${data.entries.length} · ${data.architecture}` : ''}
        </span>
      </div>

      {error && (
        <p className="hint" style={{ color: 'var(--danger)' }}>
          Could not read the remote catalogs: {error}. You can still type an alias below.
        </p>
      )}
      {data && Object.entries(data.errors).map(([name, message]) => (
        <p className="hint" key={name} style={{ color: 'var(--warn)' }}>
          {name}: {message}
        </p>
      ))}

      {!data && !error && (
        <div className="loading-wrap"><span className="spinner" /> Reading catalogs…</div>
      )}

      {data && (
        <div className="browser-list">
          {matches.length === 0 && (
            <p className="hint" style={{ padding: '10px 2px' }}>Nothing matches “{query}”.</p>
          )}
          {matches.map((image) => {
            const cached = isCached(image)
            const selected = value === image.full_alias
            return (
              <button
                type="button"
                key={`${image.remote}/${image.alias}/${image.arch}`}
                className="browser-row"
                aria-pressed={selected}
                disabled={disabled}
                onClick={() => onPick(image.full_alias)}
              >
                <span className="browser-row-main">
                  <span className="browser-name">
                    {image.label}
                    {image.variant !== 'default' && (
                      <span className="faint"> · {image.variant}</span>
                    )}
                  </span>
                  <span className="mono faint">{image.full_alias}</span>
                </span>
                <span className="browser-row-side">
                  {cached ? (
                    <span className="badge badge-ok">Downloaded</span>
                  ) : (
                    <span className="faint num">
                      {bytes(forVm ? image.vm_size : image.size)}
                    </span>
                  )}
                </span>
              </button>
            )
          })}
        </div>
      )}

      <div className="browser-footer">
        <input
          className="input mono"
          value={value}
          placeholder="or type an alias: images:debian/12"
          aria-label="Image alias"
          autoComplete="off"
          spellCheck={false}
          disabled={disabled}
          onChange={(event) => onPick(event.target.value)}
        />
        <button type="button" className="btn" onClick={onBack} disabled={disabled}>
          Done
        </button>
      </div>
    </div>
  )
}
