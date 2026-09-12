import { useState } from 'react'
import type { Status } from '../lib/types'

interface Props {
  status: Status
  onSetup: (storageDriver: string) => Promise<void>
}

// dir works everywhere with no extra setup; the others need kernel/tool support
// but can enforce a per-container disk size.
const PREFERRED_DRIVERS = ['dir', 'btrfs', 'zfs', 'lvm']
const QUOTA_CAPABLE = ['btrfs', 'zfs', 'lvm']

export function SetupBanner({ status, onSetup }: Props) {
  const [driver, setDriver] = useState('dir')
  const [busy, setBusy] = useState(false)

  const drivers = PREFERRED_DRIVERS.filter((d) => status.storage_drivers.includes(d))

  async function run() {
    setBusy(true)
    try {
      await onSetup(driver)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="banner">
      <div className="banner-body">
        <h3>{status.product} needs to be set up before you can create containers</h3>
        <p>
          lemondx can do it for you: create a storage pool, add a NAT bridge, and
          attach both to the default profile.
        </p>
        <ul>
          {status.issues.map((issue) => <li key={issue}>{issue}</li>)}
        </ul>
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <div className="field" style={{ minWidth: 160 }}>
            <label htmlFor="setup-driver">Storage driver</label>
            <select
              id="setup-driver"
              className="select"
              value={driver}
              onChange={(e) => setDriver(e.target.value)}
              disabled={busy}
            >
              {drivers.map((d) => (
                <option key={d} value={d}>
                  {d}
                  {d === 'dir' ? ' — simplest, but no disk quotas' : ''}
                  {QUOTA_CAPABLE.includes(d) ? ' — supports disk quotas' : ''}
                </option>
              ))}
            </select>
          </div>
          <button className="btn btn-primary" onClick={run} disabled={busy}>
            {busy && <span className="spinner" />}
            {busy ? 'Setting up…' : `Initialize ${status.product}`}
          </button>
        </div>
      </div>
    </div>
  )
}
