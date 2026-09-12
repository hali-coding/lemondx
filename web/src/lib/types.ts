export type ContainerStatus =
  | 'Running' | 'Stopped' | 'Frozen' | 'Starting' | 'Stopping' | 'Error' | string

export interface Limits {
  cpu: string
  memory: string
}

export interface Container {
  name: string
  status: ContainerStatus
  type: 'container' | 'virtual-machine' | string
  architecture: string | null
  ephemeral: boolean
  description: string
  created_at: string
  last_used_at: string
  profiles: string[]
  image: string
  image_alias: string
  limits: Limits
  ipv4: string[]
  ipv6: string[]
  pid: number
  processes: number
  cpu_time_ns: number
  memory_usage: number
  memory_peak: number
  network_rx: number
  network_tx: number
  snapshot_count: number
}

export interface Snapshot {
  name: string
  created_at: string
  stateful: boolean
  size: string | null
}

export interface NetworkInterface {
  name: string
  type: string
  state: string
  hwaddr: string
  mtu: number
  addresses: string[]
  rx: number
  tx: number
}

export interface ContainerDetail extends Container {
  config: Record<string, string>
  devices: Record<string, Record<string, string>>
  expanded_devices: Record<string, Record<string, string>>
  snapshots: Snapshot[]
  network_detail: NetworkInterface[]
  /** Present when modules ran as part of creating this container. */
  bootstrap?: BootstrapResult
}

export interface StoragePool {
  name: string
  driver: string
  used_by: number
  /** False for drivers that cannot enforce a per-container disk size. */
  supports_quota: boolean
}

export interface ManagedNetwork {
  name: string
  type: string
  ipv4: string | null
}

export interface Status {
  connected: boolean
  ready: boolean
  issues: string[]
  /** Which daemon we are talking to. */
  flavor: 'lxd' | 'incus' | string
  /** Display name: "LXD" or "Incus". */
  product: string
  /** CLI client for this daemon: "lxc" or "incus". */
  client_binary: string
  socket: string
  /** The pool a new container would land on, or null before setup. */
  root_pool: StoragePool | null
  server_version: string | null
  kernel: string | null
  driver: string | null
  architectures: string[]
  project: string
  storage_drivers: string[]
  storage_pools: StoragePool[]
  networks: ManagedNetwork[]
}

export interface LocalImage {
  fingerprint: string
  aliases: string[]
  description: string | null
  architecture: string
  size: number
  cached: boolean
  used_by: number
}

export interface CatalogImage {
  alias: string
  label: string
  family: string
}

export interface Images {
  local: LocalImage[]
  catalog: CatalogImage[]
  remotes: string[]
}

export interface ExecResult {
  exit_code: number
  stdout: string
  stderr: string
}

export interface CreateRequest {
  name: string
  image: string
  type?: 'container' | 'virtual-machine'
  cpu?: string
  memory?: string
  disk?: string
  description?: string
  ephemeral?: boolean
  start?: boolean
  bootstrap?: BootstrapSelection
}

export interface SetupResult {
  steps: string[]
  /** Caveats worth showing the user, e.g. a driver that cannot do quotas. */
  notes: string[]
  status: Status
}

export type StateAction = 'start' | 'stop' | 'restart' | 'freeze' | 'unfreeze'

export interface ModuleParam {
  name: string
  default: string
  description: string
}

export interface BootstrapModule {
  id: string
  name: string
  description: string
  /** Distro IDs the module targets; empty means any. */
  os: string[]
  order: number
  params: ModuleParam[]
  uses_ssh_keys: boolean
}

export interface SshKey {
  type: string
  comment: string
  fingerprint: string
  line: string
  /** Filename it came from, for keys discovered in ~/.ssh. */
  source?: string
}

export interface ModuleResult {
  id: string
  name: string
  exit_code: number
  stdout: string
  stderr: string
  duration: number
}

export interface BootstrapResult {
  container: string
  modules: ModuleResult[]
  ok: boolean
}

/** What the create dialog and the drawer panel collect before running. */
export interface BootstrapSelection {
  modules: string[]
  params: Record<string, string>
  ssh_keys: string[]
}
