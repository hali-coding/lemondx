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
  /** The template this instance was launched from, if any. */
  template: string | null
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
  /** The network the default profile's NIC joins, or null before setup. */
  default_network: string | null
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

/**
 * A create the server is running, or finished in the last few minutes. Held by
 * the server process, so it outlives the dialog or page that started it.
 */
export interface CreateProgress {
  name: string
  image: string
  type: string
  /** Set when a template launch or recreate started it. */
  template: string | null
  /** How many bootstrap modules it runs. */
  modules: number
  stage: 'creating' | 'starting' | 'bootstrapping' | 'done'
  started_at: number
  finished_at: number | null
  /** Null while running; false if it failed to create or a module failed. */
  ok: boolean | null
  error: string | null
  failed_module: string | null
  /** The last lines a failed module printed. */
  error_detail: string | null
}

export interface CreateRequest {
  name: string
  image: string
  type?: 'container' | 'virtual-machine'
  cpu?: string
  memory?: string
  disk?: string
  pool?: string
  /** Replaces the profiles' NIC; the default profile's network when omitted. */
  network?: string
  /** LXD/Incus profiles; the daemon's default when omitted. */
  profiles?: string[]
  description?: string
  ephemeral?: boolean
  start?: boolean
  /** VMs only; false boots UEFI without secure boot. */
  secureboot?: boolean
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
  /** The value declared in the module header. */
  default: string
  description: string
  /** Effective value: the saved default, else the declared one. */
  value: string
  /** True when the user has saved an override. */
  saved: boolean
  /** Never stored or pre-filled; must be entered on every run. */
  secret: boolean
  /** Declared with `# text:`: a config file or similar, edited in a text area. */
  multiline: boolean
}

export interface BootstrapModule {
  id: string
  /** Shipped with lemondx; cannot be deleted, only shadowed. */
  builtin: boolean
  /** An upload: its script can be rewritten and the module deleted. */
  editable: boolean
  /** An upload that replaces a built-in of the same id; deleting it restores that. */
  shadows_builtin: boolean
  /** Pre-selected when creating a container. */
  is_default: boolean
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

export interface RemoteImage {
  alias: string
  full_alias: string
  /** Readable name, e.g. "Debian 12 (bookworm)". */
  label: string
  remote: string
  os: string
  release: string
  release_title: string
  variant: string
  arch: string
  serial: string
  size: number
  vm_size: number
  container_fingerprint: string | null
  vm_fingerprint: string | null
  supported: boolean | null
  eol: string | null
  /** The container image is already on this host. */
  cached: boolean
  /** The VM image is already on this host. */
  cached_vm: boolean
}

export interface ImageBrowse {
  entries: RemoteImage[]
  remotes: string[]
  browsed: string[]
  architecture: string | null
  /** Remote name -> why its catalog could not be read. */
  errors: Record<string, string>
}

export interface NetworkSummary {
  name: string
  type: string
  managed: boolean
  status: string
  description: string
  ipv4_address: string
  ipv6_address: string
  used_by: number
  /** The network the default profile's NIC joins. */
  default: boolean
  /** Managed networks and host bridges; what an instance can be put on. */
  attachable: boolean
  /** A managed bridge lemondx can edit and delete. */
  manageable: boolean
  read_only_reason: string
}

/** A subnet already on the host, which a new bridge must not overlap. */
export interface SubnetInUse {
  interface: string
  subnet: string
  family: 4 | 6
}

/** Bridge settings as daemon config keys; an empty value unsets one on update. */
export interface NetworkRequest {
  name?: string
  description?: string
  config?: Record<string, string>
}

export interface NetworkLease {
  hostname: string
  address: string
  hwaddr: string
  type: string
}

export interface NetworkFamily {
  address: string
  nat: boolean
  dhcp: boolean
  dhcp_ranges: string
}

export interface NetworkDetail {
  name: string
  type: string
  managed: boolean
  status: string
  description: string
  default: boolean
  attachable: boolean
  manageable: boolean
  read_only_reason: string
  config: Record<string, string>
  ipv4: NetworkFamily
  ipv6: NetworkFamily
  dns_domain: string
  dns_mode: string
  mtu: number | string
  hwaddr: string
  state: string
  addresses: string[]
  counters: { rx: number; tx: number }
  leases: NetworkLease[]
  instances: string[]
  profiles: string[]
  forwards: unknown[]
}

/**
 * A saved module selection with every non-secret parameter its modules
 * declare and the SSH keys to install. Unrelated to LXD/Incus profiles.
 */
export interface BootstrapProfile {
  name: string
  description: string
  modules: string[]
  params: Record<string, string>
  ssh_keys: string[]
}

/** What an instance is made of, apart from its name. */
export interface InstanceSpec {
  image: string
  type: 'container' | 'virtual-machine'
  cpu: string
  memory: string
  disk: string
  /** Blank means the pool the default profile uses. */
  pool: string
  /** Blank means the network the default profile's NIC joins. */
  network: string
  /** LXD/Incus profiles, not bootstrap ones. */
  profiles: string[]
  ephemeral: boolean
  start: boolean
  /** VMs only; always true for a container. */
  secureboot: boolean
  bootstrap: BootstrapSelection
}

/** A saved InstanceSpec, launched as `<name_prefix>-1`, `-2`, … */
export interface InstanceTemplate extends InstanceSpec {
  name: string
  description: string
  name_prefix: string
}

export type TemplateRequest = Partial<InstanceSpec> & {
  image: string
  description?: string
  name_prefix?: string
}

export interface LaunchedInstance {
  name: string
  /** False if it failed to delete or create, or a module failed. */
  ok: boolean
  error: string | null
  /**
   * The new instance, as much of it as a run record keeps: module output only
   * survives, cut to its tail, for modules that failed. Null when it was
   * destroyed or never created.
   */
  container: LaunchedContainer | null
  /** What a command printed here, for a run of action `exec`. */
  exec?: TemplateExecOutput | null
}

export interface LaunchedContainer {
  name: string
  status: string
  ipv4: string[]
  bootstrap: BootstrapResult | null
}

export interface TemplateExecOutput {
  exit_code: number
  stdout: string
  stderr: string
  /** Output was cut to its last part to keep run records small. */
  truncated: boolean
}

/** What a launch, recreate or destroy did to each instance, in order. */
export interface TemplateRunResult {
  template: string
  ok: boolean
  instances: LaunchedInstance[]
}

/**
 * A launch, recreate or destroy the server is running or last ran for a
 * template. Held by the server process, so it outlives the page that started it.
 */
export interface TemplateRun {
  template: string
  action: 'launch' | 'recreate' | 'destroy' | 'exec'
  count: number
  /** The shell command, for `exec`. */
  command: string | null
  /** Unix seconds. */
  started_at: number
  /** Null while it is still running. */
  finished_at: number | null
  result: TemplateRunResult | null
  /** Set when the run as a whole failed, rather than one instance in it. */
  error: string | null
}

export interface ModuleSource {
  id: string
  builtin: boolean
  content: string
  path: string
}

/** A claim on one resource. `implicit` marks a daemon default, not a set limit. */
export interface ResourceClaim {
  limit: string
  implicit: boolean
}

export interface ResourceInstance {
  name: string
  type: string
  status: ContainerStatus
  /** Running or frozen: holding its CPU and memory right now. */
  active: boolean
  cpu_time_ns: number
  cpu: ResourceClaim & { count: number | null }
  memory: ResourceClaim & { bytes: number | null; usage: number }
  disk: {
    pool: string | null
    size: string
    bytes: number | null
    usage: number
    implicit: boolean
  }
}

export interface PoolResources {
  name: string
  driver: string
  supports_quota: boolean
  total: number
  used: number
  allocated: number
  /** Instances on this pool with no size, which can grow to fill it. */
  unlimited: string[]
}

export interface Resources {
  host: {
    architecture: string
    cpu_model: string
    cpu_sockets: number
    cpu_cores: number
    cpu_threads: number
    memory_total: number
    memory_used: number
  }
  cpu: { total: number; allocated: number; stopped: number; unlimited: string[] }
  memory: {
    total: number
    used: number
    instances_used: number
    allocated: number
    stopped: number
    unlimited: string[]
  }
  storage: PoolResources[]
  instances: ResourceInstance[]
}

export interface StorageDriverCapability {
  name: 'dir' | 'btrfs' | 'lvm' | 'zfs'
  available: boolean
  supports_quota: boolean
  supports_custom_block: boolean
}

export interface StorageVolume {
  pool: string
  name: string
  type: string
  content_type: string
  description: string
  config: Record<string, string>
  size: string
  used_by: string[]
  manageable: boolean
}

export interface StoragePoolDetail {
  name: string
  driver: string
  description: string
  config: Record<string, string>
  source: string
  used_by: string[]
  used_by_count: number
  total: number
  used: number
  root: boolean
  supports_quota: boolean
  manageable: boolean
  read_only_reason: string
  volume_count: number
  delete_plan: StoragePoolDeletePlan
  volumes?: StorageVolume[]
}

export interface StoragePoolDeletePlan {
  instances: string[]
  attached_instances: string[]
  images: string[]
  custom_volumes: string[]
  profiles: string[]
  other_references: string[]
  other_volumes: string[]
}

export interface StorageOverview {
  clustered: boolean
  local_drivers: StorageDriverCapability[]
  pools: StoragePoolDetail[]
  volumes: StorageVolume[]
}

export interface StoragePoolRequest {
  name?: string
  driver?: string
  source?: string
  size?: string
  description?: string
  config?: Record<string, string>
}

export interface StorageVolumeRequest {
  name?: string
  content_type?: 'filesystem' | 'block'
  size?: string
  description?: string
  config?: Record<string, string>
}

// -- authentication (auth.py) ------------------------------------------------

export type Role = 'read' | 'admin'

/** Who a request ran as. `via` is `none` when the server has auth off. */
export interface Principal {
  name: string
  role: Role
  via: string
}

export interface AuthInfo {
  enabled: boolean
  methods: ('local' | 'pam' | 'proxy' | 'token')[]
  password_login: boolean
  principal: Principal | null
}

export interface ApiToken {
  id: string
  name: string
  owner: string
  role: Role
  created: number
  expires: number | null
  last_used: number | null
  expired: boolean
}

/** Only the create response carries the secret, and only that once. */
export interface CreatedApiToken extends ApiToken {
  token: string
}

export interface LocalUser {
  name: string
  role: Role
  created: number
  updated: number
}

// -- health (health.py) ------------------------------------------------------

export type HealthStatus = 'healthy' | 'degraded' | 'unhealthy' | 'starting' | 'unknown' | 'paused'

/** One instance's latest check. Times are seconds since the epoch. */
export interface HealthRecord {
  name: string
  type: string
  status: HealthStatus
  reasons: string[]
  checked_at: number
  /** When the status last changed. */
  since: number
  processes: number
  cpu: { percent: number; cores_used: number; cores: number } | null
  memory: { usage: number; limit: number | null; percent: number | null }
  /**
   * `source: cgroup` is counted by lemondx from the container's cgroup: 1, 5 and
   * 15 minute averages from `serve` (`warming` for its first minute), or one
   * average over `window` seconds from a one-off check. `guest` is a VM's own;
   * `proc` is what a container reports, usually the host's (`scope: host`).
   */
  load: {
    avg: [number, number | null, number | null]
    scope: 'instance' | 'host' | 'unknown'
    source: 'cgroup' | 'guest' | 'proc'
    warming: boolean
    window: number | null
  } | null
  probe: { ok: boolean; ms: number; error: string | null } | null
  failures: number
}

export interface HealthReport {
  enabled: boolean
  interval: number | null
  thresholds: Record<string, number> | null
  checked_at: number | null
  instances: HealthRecord[]
}
