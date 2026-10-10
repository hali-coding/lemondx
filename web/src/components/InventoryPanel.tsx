import { useCallback, useEffect, useState } from 'react'
import { useCanWrite } from '../hooks/useAuth'
import { api } from '../lib/api'
import type { ForeignInstance, InstanceTemplate, Inventory } from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import { RefreshIcon } from './Icons'

interface Props {
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
}

const keyOf = (c: { node: string; name: string }) => `${c.node}/${c.name}`

/**
 * Instances on any node that lemondx did not make: someone's `lxc launch`, a
 * script, a lemondx from before instances were marked. Listed when the tab
 * opens and again on Force inventory -- every node is asked there and then,
 * since lemondx keeps no list of its own and the daemons' listings are the
 * inventory. Importing marks them as lemondx's, and can make them one
 * template's, which is the step with a cost: that template's Recreate
 * deletes and remakes them.
 */
export function InventoryPanel({ onNotify }: Props) {
  const canWrite = useCanWrite()
  const [inventory, setInventory] = useState<Inventory | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [templates, setTemplates] = useState<InstanceTemplate[]>([])
  const [template, setTemplate] = useState('')
  const [importing, setImporting] = useState(false)
  const [confirming, setConfirming] = useState<ForeignInstance[] | null>(null)

  const take = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      const found = await api.inventory(signal)
      setInventory(found)
      setError(null)
      // What is still there stays ticked; what was imported or deleted goes.
      setSelected((current) => new Set(found.foreign.map(keyOf).filter((k) => current.has(k))))
    } catch (cause) {
      if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    // oxlint-disable-next-line react/set-state-in-effect -- async fetch, not a sync setState
    take(controller.signal)
    api.templates(controller.signal).then(setTemplates).catch(() => {})
    return () => controller.abort()
  }, [take])

  const foreign = inventory?.foreign ?? []
  const chosen = foreign.filter((c) => selected.has(keyOf(c)))

  function request(targets: ForeignInstance[]) {
    if (template) setConfirming(targets)
    else run(targets)
  }

  async function run(targets: ForeignInstance[]) {
    setConfirming(null)
    setImporting(true)
    try {
      const result = await api.importInstances(
        targets.map(({ node, name }) => ({ node, name })), template)
      const failed = result.instances.filter((i) => !i.ok)
      const done = result.instances.length - failed.length
      onNotify(failed.length ? 'error' : 'success',
        `Imported ${done} of ${result.instances.length}`
          + (result.template ? ` into “${result.template}”` : ''),
        failed.length
          ? failed.map((i) => `${i.name} on ${i.node}: ${i.error}`).join('; ')
          : undefined)
      await take()
    } catch (cause) {
      onNotify('error', 'Could not import', (cause as Error).message)
    } finally {
      setImporting(false)
    }
  }

  function toggle(key: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <section className="card access-card">
      <header className="access-head">
        <h3>Inventory</h3>
        <span className="faint">
          Instances on any node that lemondx did not make
          {inventory && <> · checked {new Date(inventory.checked_at * 1000).toLocaleTimeString()}</>}
        </span>
        <div className="row-actions access-head-end">
          <button className="btn btn-sm" disabled={loading} onClick={() => take()}
            title="List every node now for instances made outside lemondx">
            {loading ? <span className="spinner" /> : <RefreshIcon size={13} />} Force inventory
          </button>
        </div>
      </header>

      {error && <div className="inventory-note inventory-error">{error}</div>}
      {inventory?.errors.map((e) => (
        <div key={e.node} className="inventory-note inventory-error">
          <strong>{e.node}</strong>: {e.error}
        </div>
      ))}

      {inventory && foreign.length === 0 && (
        <div className="inventory-note faint">
          Every instance on {inventory.nodes.join(', ')} was made or imported by lemondx.
        </div>
      )}

      {foreign.length > 0 && (
        <>
          <div className="banner banner-warn inventory-banner">
            <div className="banner-body">
              <h3>
                {foreign.length === 1 ? '1 instance was' : `${foreign.length} instances were`} made
                outside lemondx
              </h3>
              <p>
                Created with <span className="mono">lxc</span>, a script, or an older lemondx.
                They are already listed and monitored like any other; importing marks them as
                lemondx’s so they are no longer flagged, and can make them one template’s.
              </p>
            </div>
          </div>
          <div className="table-scroll">
            <table className="ctable inventory-table">
              <thead>
                <tr>
                  <th className="tick">
                    <input type="checkbox" aria-label="Select every one"
                      checked={chosen.length === foreign.length}
                      onChange={(e) => setSelected(e.target.checked
                        ? new Set(foreign.map(keyOf)) : new Set())} />
                  </th>
                  <th>Node</th>
                  <th>Name</th>
                  <th>Status</th>
                  <th>Image</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {foreign.map((c) => (
                  <tr key={keyOf(c)} onClick={() => toggle(keyOf(c))}
                    aria-selected={selected.has(keyOf(c))}>
                    <td className="tick">
                      <input type="checkbox" checked={selected.has(keyOf(c))}
                        aria-label={`Select ${c.name} on ${c.node}`}
                        onChange={() => toggle(keyOf(c))} onClick={(e) => e.stopPropagation()} />
                    </td>
                    <td>{c.node}</td>
                    <td>
                      <span className="cname">
                        {c.name}
                        {c.type === 'virtual-machine' && <span className="vm-tag">VM</span>}
                      </span>
                    </td>
                    <td>{c.status}</td>
                    <td className="faint">{c.image || '—'}</td>
                    <td className="faint">
                      {c.created_at ? new Date(c.created_at).toLocaleString() : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="inventory-actions">
            <select className="select" value={template} aria-label="Import into a template"
              onChange={(e) => setTemplate(e.target.value)} disabled={!canWrite || importing}>
              <option value="">Just mark them as lemondx’s</option>
              {templates.map((t) => (
                <option key={t.name} value={t.name}>Also make them “{t.name}” instances</option>
              ))}
            </select>
            <button className="btn btn-sm" disabled={!canWrite || importing || chosen.length === 0}
              onClick={() => request(chosen)}>
              Import {chosen.length || ''} selected
            </button>
            <button className="btn btn-sm btn-primary" disabled={!canWrite || importing}
              onClick={() => request(foreign)}>
              {importing && <span className="spinner" />} Import all {foreign.length}
            </button>
          </div>
          {template && (
            <p className="hint inventory-note" style={{ color: 'var(--warn)' }}>
              As “{template}” instances, its runs take them in — and its Recreate deletes them
              and makes them again from the template, losing what is on them.
            </p>
          )}
        </>
      )}

      {confirming && (
        <ConfirmDialog
          title={`Import ${confirming.length} into “${template}”?`}
          message={`${confirming.map((c) => `${c.name} on ${c.node}`).join(', ')} will become `
            + `instances of “${template}”. Recreating that template deletes them and makes `
            + 'them again from it, losing what is on them; destroying it deletes them.'}
          confirmLabel="Import"
          busy={importing}
          onConfirm={() => run(confirming)}
          onCancel={() => setConfirming(null)}
        />
      )}
    </section>
  )
}
