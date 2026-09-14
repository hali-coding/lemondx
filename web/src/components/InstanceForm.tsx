import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { keyModules, savableSelection } from '../lib/bootstrap'
import { subnetStatus } from '../lib/cidr'
import { DEFAULT_CPU, DEFAULT_MEMORY, specProblems } from '../lib/instance'
import type {
  BootstrapModule, BootstrapProfile, BootstrapSelection, Images, InstanceSpec, NetworkSummary,
  SshKey, Status, SubnetInUse,
} from '../lib/types'
import { BootstrapPicker } from './BootstrapPicker'
import { SubnetField } from './SubnetField'
import { ImageBrowser } from './ImageBrowser'
import { SshKeyPicker } from './SshKeyPicker'

interface Props {
  value: InstanceSpec
  onChange: (next: InstanceSpec) => void
  modules: BootstrapModule[]
  profiles: BootstrapProfile[]
  hostKeys: SshKey[]
  reloadProfiles: () => void
  onError: (message: string) => void
  disabled?: boolean
  /**
   * Templates cannot hold secrets, so their fields become a note that the
   * value is asked for on launch, and a missing one blocks nothing here.
   */
  secretsAtLaunch?: boolean
}

/**
 * Everything that describes an instance apart from its name: image, pool,
 * limits, flags, bootstrap modules and keys. Shared by the create dialog and
 * the template editor so the two cannot drift.
 */
export function InstanceForm({
  value, onChange, modules, profiles, hostKeys, reloadProfiles, onError, disabled,
  secretsAtLaunch = false,
}: Props) {
  const [images, setImages] = useState<Images | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const [customImage, setCustomImage] = useState(false)
  const [profileName, setProfileName] = useState('')
  const [savingProfile, setSavingProfile] = useState(false)
  // Aliases of images already on this host, so the quick-pick grid can mark
  // them. Best effort: the grid still works if the catalog is unreachable.
  const [cachedAliases, setCachedAliases] = useState<Set<string>>(new Set())
  const [networks, setNetworks] = useState<NetworkSummary[] | null>(null)
  const [newNetwork, setNewNetwork] = useState<{ name: string; ipv4: string; nat: boolean } | null>(null)
  const [creatingNetwork, setCreatingNetwork] = useState(false)
  const [networkError, setNetworkError] = useState('')
  const [subnets, setSubnets] = useState<SubnetInUse[] | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    api.images(controller.signal).then(setImages).catch(() => {})
    api.status(controller.signal).then(setStatus).catch(() => {})
    api.networks(controller.signal).then(setNetworks).catch(() => {})
    api.browseImages({}, controller.signal)
      .then((browse) => setCachedAliases(
        new Set(browse.entries.filter((e) => e.cached).map((e) => e.full_alias))))
      .catch(() => {})
    return () => controller.abort()
  }, [])

  const selection = value.bootstrap
  const set = (patch: Partial<InstanceSpec>) => onChange({ ...value, ...patch })
  const setSelection = (next: BootstrapSelection) => set({ bootstrap: next })

  function applyProfile(name: string) {
    setProfileName(name)
    const profile = profiles.find((p) => p.name === name)
    if (!profile) return
    setSelection({
      modules: profile.modules,
      params: { ...profile.params },
      // A profile saved before keys were kept has none; keep what is picked.
      ssh_keys: profile.ssh_keys.length > 0 ? profile.ssh_keys : selection.ssh_keys,
    })
  }

  async function saveAsProfile() {
    const name = window.prompt('Save these modules, settings and keys as:', profileName || '')
    if (!name?.trim()) return
    setSavingProfile(true)
    try {
      const saved = savableSelection(modules, selection)
      await api.saveBootstrapProfile(name.trim(), {
        modules: saved.modules, params: saved.params, ssh_keys: saved.ssh_keys,
      })
      setProfileName(name.trim())
      reloadProfiles()
    } catch (cause) {
      onError((cause as Error).message)
    } finally {
      setSavingProfile(false)
    }
  }

  // Some drivers (notably dir, without filesystem project quotas) accept a
  // disk size and then ignore it. The server tells us which pool an instance
  // lands on and whether it can enforce one.
  const defaultPoolName = status?.root_pool?.name || status?.storage_pools[0]?.name || ''
  const selectedPoolName = value.pool || defaultPoolName
  const selectedPool = status?.storage_pools.find(
    (candidate) => candidate.name === selectedPoolName) ?? null
  const quotaless = selectedPool !== null && !selectedPool.supports_quota

  // Same rule as the pool: the default is stored as blank, so a template
  // follows whatever network the default profile uses where it launches.
  const attachable = (networks ?? []).filter((n) => n.attachable)
  const defaultNetworkName = networks?.find((n) => n.default)?.name ?? ''
  const selectedNetworkName = value.network || defaultNetworkName
  const selectedNetwork = networks?.find((n) => n.name === selectedNetworkName) ?? null

  const newSubnet = newNetwork ? subnetStatus(newNetwork.ipv4, 4, subnets) : null

  function openNewNetwork() {
    setNewNetwork({ name: '', ipv4: 'auto', nat: true })
    setNetworkError('')
    api.subnets().then(setSubnets).catch(() => setSubnets([]))
  }

  async function createNetwork() {
    if (!newNetwork?.name.trim() || !newSubnet?.address) return
    setCreatingNetwork(true)
    setNetworkError('')
    try {
      const created = await api.createNetwork({
        name: newNetwork.name.trim(),
        config: {
          'ipv4.address': newSubnet.address,
          'ipv4.nat': String(newNetwork.nat),
        },
      })
      setNetworks(await api.networks())
      set({ network: created.name === defaultNetworkName ? '' : created.name })
      setNewNetwork(null)
    } catch (cause) {
      setNetworkError((cause as Error).message)
    } finally {
      setCreatingNetwork(false)
    }
  }

  const installsKeys = keyModules(modules, selection)
  const { keysMissing, secretsMissing } = specProblems(value, modules, secretsAtLaunch)
  const isVm = value.type === 'virtual-machine'

  return (
    <>
      <div className="field">
        <label>Image</label>
        {!customImage ? (
          <>
            <div className="image-choices">
              {(images?.catalog ?? []).map((choice) => (
                <button
                  type="button"
                  key={choice.alias}
                  className="image-choice"
                  aria-pressed={value.image === choice.alias}
                  onClick={() => set({ image: choice.alias })}
                  disabled={disabled}
                >
                  <strong>{choice.label}</strong>
                  <span>{choice.alias}</span>
                  {cachedAliases.has(choice.alias) && (
                    <span className="badge badge-ok choice-cached">Downloaded</span>
                  )}
                </button>
              ))}
            </div>
            {images && !images.catalog.some((c) => c.alias === value.image) && value.image && (
              <span className="hint">Selected: <span className="mono">{value.image}</span></span>
            )}
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              style={{ alignSelf: 'flex-start', marginTop: 4 }}
              onClick={() => setCustomImage(true)}
              disabled={disabled}
            >
              Browse all images…
            </button>
          </>
        ) : (
          <ImageBrowser
            value={value.image}
            forVm={isVm}
            disabled={disabled}
            onPick={(image) => set({ image })}
            onBack={() => setCustomImage(false)}
          />
        )}
      </div>

      <div className="grid-2">
        <div className="field">
          <label htmlFor="c-pool">Storage pool</label>
          <select id="c-pool" className="select" value={selectedPoolName}
            disabled={disabled || !status?.storage_pools.length}
            // Choosing the default pool stores nothing, so a template made here
            // follows whichever pool the daemon it launches on defaults to.
            onChange={(event) => set({
              pool: event.target.value === defaultPoolName ? '' : event.target.value })}>
            {value.pool && !status?.storage_pools.some((p) => p.name === value.pool) && (
              <option value={value.pool}>{value.pool} (not on this host)</option>
            )}
            {(status?.storage_pools ?? []).map((candidate) => (
              <option key={candidate.name} value={candidate.name}>
                {candidate.name} ({candidate.driver})
                {candidate.name === defaultPoolName ? ' (default)' : ''}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="c-network">Network</label>
          <select id="c-network" className="select" value={selectedNetworkName}
            disabled={disabled || attachable.length === 0}
            onChange={(event) => set({
              network: event.target.value === defaultNetworkName ? '' : event.target.value })}>
            {value.network && networks && !attachable.some((n) => n.name === value.network) && (
              <option value={value.network}>{value.network} (not on this host)</option>
            )}
            {attachable.length === 0 && <option value="">{networks ? 'No networks' : 'Loading…'}</option>}
            <optgroup label="Managed networks">
              {attachable.filter((n) => n.managed).map((n) => (
                <option key={n.name} value={n.name}>
                  {n.name}{n.ipv4_address && n.ipv4_address !== 'none' ? ` (${n.ipv4_address})` : ''}
                  {n.default ? ' (default)' : ''}
                </option>
              ))}
            </optgroup>
            {attachable.some((n) => !n.managed) && (
              <optgroup label="Host bridges">
                {attachable.filter((n) => !n.managed).map((n) => (
                  <option key={n.name} value={n.name}>{n.name}{n.default ? ' (default)' : ''}</option>
                ))}
              </optgroup>
            )}
          </select>
        </div>
      </div>
      {selectedNetwork && !selectedNetwork.managed && (
        <span className="hint" style={{ marginTop: -8, color: 'var(--warn)' }}>
          <span className="mono">{selectedNetwork.name}</span> is a host bridge: the instance
          gets its address from whatever serves that network, not from the daemon.
        </span>
      )}
      {newNetwork === null && (
        <button type="button" className="btn btn-ghost btn-sm"
          style={{ alignSelf: 'flex-start', marginTop: -8 }} disabled={disabled}
          onClick={openNewNetwork}>
          New network…
        </button>
      )}
      {newNetwork && (
        <div className="net-inline">
          <div className="field">
            <label htmlFor="c-net-name">Network name</label>
            <input id="c-net-name" className="input mono" value={newNetwork.name}
              maxLength={15} placeholder="lxdbr1" autoComplete="off"
              disabled={disabled || creatingNetwork} autoFocus
              // Enter would otherwise submit the whole create form around this.
              onKeyDown={(event) => {
                if (event.key === 'Enter') { event.preventDefault(); createNetwork() }
              }}
              onChange={(event) => setNewNetwork({ ...newNetwork, name: event.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="c-net-ipv4">IPv4 subnet</label>
            <SubnetField id="c-net-ipv4" family={4} value={newNetwork.ipv4} used={subnets}
              allowNone={false} disabled={disabled || creatingNetwork} onEnter={createNetwork}
              onChange={(ipv4) => setNewNetwork({ ...newNetwork, ipv4 })} />
          </div>
          <label className="checkbox">
            <input type="checkbox" checked={newNetwork.nat} disabled={disabled || creatingNetwork}
              onChange={(event) => setNewNetwork({ ...newNetwork, nat: event.target.checked })} />
            NAT behind the host, so instances can reach the internet
          </label>
          {networkError && <span className="field-error">{networkError}</span>}
          <div className="net-inline-actions">
            <button type="button" className="btn btn-primary btn-sm"
              disabled={disabled || creatingNetwork || !newNetwork.name.trim() || !newSubnet?.address}
              onClick={createNetwork}>
              {creatingNetwork && <span className="spinner" />}
              Create network
            </button>
            <button type="button" className="btn btn-sm" disabled={creatingNetwork}
              onClick={() => setNewNetwork(null)}>
              Cancel
            </button>
            <span className="hint">A managed bridge with DHCP; more options in the Network tab.</span>
          </div>
        </div>
      )}

      <div className="grid-3">
        <div className="field">
          <label htmlFor="c-cpu">CPU limit</label>
          <input id="c-cpu" className="input" value={value.cpu} disabled={disabled}
            onChange={(e) => set({ cpu: e.target.value })} placeholder={DEFAULT_CPU}
            autoComplete="off" />
        </div>
        <div className="field">
          <label htmlFor="c-mem">Memory</label>
          <input id="c-mem" className="input" value={value.memory} disabled={disabled}
            onChange={(e) => set({ memory: e.target.value })} placeholder={DEFAULT_MEMORY}
            autoComplete="off" />
        </div>
        <div className="field">
          <label htmlFor="c-disk">Disk</label>
          <input id="c-disk" className="input" value={value.disk} disabled={disabled}
            onChange={(e) => set({ disk: e.target.value })} placeholder="10GiB"
            autoComplete="off" />
        </div>
      </div>
      <span className="hint" style={{ marginTop: -8 }}>
        Blank CPU and memory default to {DEFAULT_CPU} cores and{' '}
        <span className="mono">{DEFAULT_MEMORY}</span>; blank disk means no size.
        CPU is a core count; memory and disk take a
        unit (<span className="mono">4GiB</span>, <span className="mono">512MiB</span>)
        — a bare number is read as GiB.
        {quotaless && value.disk.trim() && (
          <>
            {' '}
            <span style={{ color: 'var(--warn)' }}>
              Pool <span className="mono">{selectedPool?.name}</span> uses the{' '}
              <span className="mono">{selectedPool?.driver}</span> driver, which cannot
              enforce a disk size unless the filesystem has project quotas
              enabled — the value will be recorded but not applied.
            </span>
          </>
        )}
      </span>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
        <label className="checkbox">
          <input type="checkbox" checked={value.start || selection.modules.length > 0}
            disabled={disabled || selection.modules.length > 0}
            onChange={(e) => set({ start: e.target.checked })} />
          Start immediately after creating
          {selection.modules.length > 0 && (
            <span className="hint">— required by the selected modules</span>
          )}
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={isVm} disabled={disabled}
            onChange={(e) => set({ type: e.target.checked ? 'virtual-machine' : 'container' })} />
          Create a virtual machine instead of a container
        </label>
        {isVm && (
          <label className="checkbox">
            <input type="checkbox" checked={!value.secureboot} disabled={disabled}
              onChange={(e) => set({ secureboot: !e.target.checked })} />
            The image is incompatible with secure boot
            <span className="hint">
              — sets <span className="mono">{status?.flavor === 'incus'
                ? 'security.secureboot=false' : 'boot.mode=uefi-nosecureboot'}</span>
            </span>
          </label>
        )}
        <label className="checkbox">
          <input type="checkbox" checked={value.ephemeral} disabled={disabled}
            onChange={(e) => set({ ephemeral: e.target.checked })} />
          Ephemeral — delete automatically when stopped
        </label>
      </div>

      <details className="bootstrap-section" open={selection.modules.length > 0}>
        <summary>
          Bootstrap
          {selection.modules.length > 0 && (
            <span className="badge badge-info">{selection.modules.length} selected</span>
          )}
        </summary>
        <p className="hint" style={{ marginBottom: 10 }}>
          Shell modules run inside the instance once it is up, in order.
        </p>

        <div className="profile-bar">
          <select
            className="select"
            value={profileName}
            aria-label="Start from a saved profile"
            disabled={disabled || profiles.length === 0}
            onChange={(event) => applyProfile(event.target.value)}
          >
            <option value="">
              {profiles.length === 0 ? 'No saved profiles' : 'Start from a profile…'}
            </option>
            {profiles.map((profile) => (
              <option key={profile.name} value={profile.name}>{profile.name}</option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn-sm"
            disabled={disabled || savingProfile || selection.modules.length === 0}
            onClick={saveAsProfile}
          >
            Save as profile
          </button>
        </div>
        <BootstrapPicker
          modules={modules}
          value={selection}
          onChange={setSelection}
          disabled={disabled}
          secretsAtLaunch={secretsAtLaunch}
        />
      </details>

      {installsKeys.length > 0 && (
        <SshKeyPicker
          hostKeys={hostKeys}
          value={selection.ssh_keys}
          onChange={(keys) => setSelection({ ...selection, ssh_keys: keys })}
          disabled={disabled}
        />
      )}

      {secretsMissing.length > 0 && (
        <span className="hint" style={{ color: 'var(--warn)', marginTop: -8 }}>
          Enter {secretsMissing.join(' and ')} in the Bootstrap section — secrets
          have no default and are never saved.
        </span>
      )}

      {keysMissing && (
        <span className="hint" style={{ color: 'var(--warn)', marginTop: -8 }}>
          Select at least one key — {installsKeys.map((m) => m.name).join(' and ')}{' '}
          installs keys.
        </span>
      )}
    </>
  )
}
