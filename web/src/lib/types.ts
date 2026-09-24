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
  /** The stack that launched it, if any; kept on the instance itself. */
  stack: string | null
  /** Which version of each it was made from; blank when made before this was kept. */
  revisions: { template: string; stack: string }
  /** Which of its template and stack have changed since it was made. */
  stale: StaleKind[]
}

/** What an instance can be behind: the template it came from, or its stack. */
export type StaleKind = 'template' | 'stack'

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
  /** Adds a second NIC on this fabric's bridge, beside the profile's one. */
  fabric?: string
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

/** What one state action did to one container, for a bulk request. */
export interface BulkStateInstance {
  name: string
  ok: boolean
  /** Why it was refused; null when it worked. */
  error: string | null
  /** The container as it is now; null when the action failed. */
  container: ContainerDetail | null
}

/** The result of applying one action to several containers at once. */
export interface BulkStateResult {
  action: StateAction
  /** True only when every container did as asked. */
  ok: boolean
  instances: BulkStateInstance[]
}

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
  /**
   * The fabric instances get a second NIC on, so they can reach those on
   * other nodes; blank for none. A node not on it launches without it and
   * says so.
   */
  fabric: string
  /** LXD/Incus profiles, not bootstrap ones. */
  profiles: string[]
  ephemeral: boolean
  start: boolean
  /** VMs only; always true for a container. */
  secureboot: boolean
  bootstrap: BootstrapSelection
}

/**
 * A script run inside each instance launched from a template, answering like a
 * Nagios plugin: exit 0 ok, 1 warning, 2 critical, 3 (or anything else) unknown.
 */
export interface AppCheck {
  script: string
  /** How often it runs under `serve`. */
  interval_seconds: number
  /** Stopped after this long, which counts as critical. */
  timeout_seconds: number
}

/** A saved InstanceSpec, launched as `<name_prefix>-1`, `-2`, … */
export interface InstanceTemplate extends InstanceSpec {
  name: string
  description: string
  name_prefix: string
  app_check: AppCheck | null
}

export type TemplateRequest = Partial<InstanceSpec> & {
  image: string
  description?: string
  name_prefix?: string
  /** Null or absent: no app check. */
  app_check?: Partial<AppCheck> | null
}

export interface LaunchedInstance {
  name: string
  /** Which node it was created on; absent for a launch that never left this one. */
  node?: string
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
  /**
   * Things a node substituted rather than failing on -- a storage pool or
   * network the template names that the node does not have. Prefixed with the
   * node they came from when the run spanned several.
   */
  notes: string[]
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
  /** Substitutions made for this run; readable while it is still going. */
  notes: string[]
  /** The nodes it is spread over; empty for a launch on this node alone. */
  nodes: string[]
}

/* -- stacks --------------------------------------------------------------- */

/** Launch a template; what it made is handed to the launches after it. */
export interface StackLaunchStep {
  /** Lowercase, unique in the stack; what `{{id.ips}}` placeholders name. */
  id: string
  type: 'launch'
  template: string
  count: number
  /** Blank: the template's own prefix. */
  prefix: string
  /** False: the next stage starts once the instances run, bootstrap or not. */
  wait_bootstrap: boolean
  /** Parameter overrides; values may hold `{{step.field}}` placeholders. */
  params: Record<string, string>
  /** Both empty: this node. */
  nodes: string[]
  groups: string[]
}

export interface StackSleepStep {
  id: string
  type: 'sleep'
  seconds: number
}

/** Wait until every instance an earlier stage launched is healthy. */
export interface StackWaitStep {
  id: string
  type: 'wait_healthy'
  timeout_seconds: number
}

export type StackStep = StackLaunchStep | StackSleepStep | StackWaitStep
export type StackStepType = StackStep['type']

/** Stages run in order; the steps inside one run side by side. */
export interface StackStage {
  steps: StackStep[]
}

export interface Stack {
  name: string
  description: string
  stages: StackStage[]
  /** Values a launch must be given, because a step says `{{params.NAME}}`. */
  inputs: string[]
}

/** An instance a stack launched, found by its tag on whichever node holds it. */
export interface StackInstance {
  node: string
  name: string
  status: string
  template: string | null
  ipv4: string[]
  stale: StaleKind[]
}

export interface StackInstances {
  stacks: Record<string, StackInstance[]>
  /** Nodes that did not answer: their instances are missing from the lists. */
  errors: { node: string; error: string }[]
}

export type StackStepState =
  | 'pending' | 'running' | 'ready' | 'done' | 'failed' | 'skipped' | 'cancelled'

/** What a finished launch hands on, one entry per instance, in name order. */
export interface StackOutputs {
  names: string[]
  ips: string[]
  ipv6: string[]
  nodes: string[]
}

export interface StackStepRun {
  /** `_teardown` for the destroy that starts a relaunch. */
  id: string
  type: StackStepType | 'destroy'
  /** `ready`: running, with bootstrap still going in the background. */
  state: StackStepState
  started_at: number | null
  finished_at: number | null
  /** When a sleep ends, or a health wait gives up; Unix seconds. */
  until: number | null
  detail: string | null
  error: string | null
  // Launch steps only.
  template?: string
  count?: number
  wait_bootstrap?: boolean
  nodes?: string[]
  /** `ipv4` is the instance's own, unlike the filtered list in `outputs`. */
  instances?: {
    name: string; node: string; ok: boolean | null; error: string | null
    ipv4?: string | null
  }[]
  outputs?: StackOutputs | null
}

/** A stack the server is running or last ran. Held by the server process. */
export interface StackRun {
  stack: string
  action: 'launch' | 'relaunch' | 'destroy'
  started_at: number
  finished_at: number | null
  ok: boolean | null
  error: string | null
  cancelling: boolean
  cancelled: boolean
  stages: { steps: StackStepRun[] }[]
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

export type Role = 'read' | 'operator' | 'admin'

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
  /** The node that judged it; set only in a listing that spans nodes. */
  node?: string
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
  /**
   * The template's app check. Every running instance has one: `ok` with
   * `configured: false` when its template defines none, `pending` until a
   * configured check first answers. Null while paused.
   */
  app: AppCheckResult | null
  failures: number
}

/** GET /api/containers/{name}/app-check: the latest run, in full. */
export interface AppCheckOutput {
  name: string
  template: string
  script: string
  interval_seconds: number
  timeout_seconds: number
  /** Health rounds in a row that did not see it running; its check is paused meanwhile. */
  missed_rounds?: number
  /** Null until the first run finishes. */
  result: (Omit<AppCheckResult, 'configured' | 'template'> & {
    stdout: string
    stderr: string
    /** Each stream is kept to its last 16 KiB. */
    truncated: boolean
  }) | null
}

export type AppCheckStatus = 'ok' | 'warning' | 'critical' | 'unknown' | 'pending'

export interface AppCheckResult {
  status: AppCheckStatus
  configured: boolean
  template: string | null
  /** The script's exit code; null when it never ran. */
  code: number | null
  /** First line of its output, performance data dropped. */
  output: string
  ms: number | null
  checked_at: number | null
  /** Criticals in a row, counted per run. */
  streak: number
  /** Seconds between runs; null for a one-off check. */
  interval: number | null
}

export interface HealthReport {
  enabled: boolean
  interval: number | null
  thresholds: Record<string, number> | null
  checked_at: number | null
  instances: HealthRecord[]
}


/* -- federation ---------------------------------------------------------- */

/** How a node is doing right now, as the node asking sees it. */
export interface NodeState {
  reachable: boolean
  error: string | null
  product: string | null
  server_version: string | null
  /** Its daemon is set up enough to hold instances. */
  ready: boolean
  containers: number
  running: number
}

/** One fabric a node is on: the prefix everyone shares and the /24 it holds. */
export interface FabricHolding {
  prefix: string
  subnet: string
  /** Whether what leaves the fabric for anywhere else is NAT'd. */
  nat: boolean
}

/** A node's fabric claims, as its member record carries them. */
export interface FabricClaim {
  /** The address peers route this node's subnets to. */
  via: string
  fabrics: Record<string, FabricHolding>
}

/** One peer's route as this node sees it. `ok` means programmed and reachable. */
export interface FabricRoute {
  node: string
  subnet: string
  via: string
  state: 'ok' | 'missing' | 'wrong' | 'unreachable'
}

export interface FabricCommand {
  command: string
  why: string
}

/** One fabric from this node's side. */
export interface FabricLocal extends FabricHolding {
  name: string
  gateway: string
  bridge_ready: boolean
  /** Instances on this node with a NIC on the fabric's bridge. */
  instances: string[]
  routes: FabricRoute[]
}

/** What `GET /api/fabric` serves: this node's half of every fabric. */
export interface FabricStatus {
  node: string
  /** On at least one fabric. */
  enabled: boolean
  via: string
  fabrics: FabricLocal[]
  /** Whether routes can be programmed here; false means the plan is advisory. */
  privileged: boolean
  /** How many route commands `apply` would run. */
  pending: number
  /** What the last apply made of the firewall; null until this server has applied once. */
  firewall: { ok: boolean; error: string; at: number } | null
  error: string
  /** One sentence on why traffic will not flow, or blank. */
  check: string
  warnings: string[]
}

/**
 * One node's part in one fabric. `absent`: not on it; `unknown`: did not
 * answer (subnet from its claim); `bridge`/`routes`: on it but not carrying
 * traffic yet; `conflict`: holds it under another prefix.
 */
export interface FabricMember {
  node: string
  state: 'ok' | 'absent' | 'unknown' | 'bridge' | 'routes' | 'conflict'
  detail: string
  subnet: string
  prefix: string
  gateway: string
  bridge_ready: boolean
  routes_ok: number
  routes_total: number
  instances: string[]
}

export interface FabricSummary {
  name: string
  prefix: string
  nat: boolean
  /** Nodes holding it under a different prefix. */
  conflict: string[]
  /** Instances attached across every node. */
  instances: number
  members: FabricMember[]
}

export interface FabricNodeState {
  node: string
  self: boolean
  /** Answered; false means its row in every fabric is from its claims. */
  ok: boolean
  error: string
  privileged: boolean
  pending: number
  check: string
  via: string
}

/** What `GET /api/fabrics` serves: every fabric in the cluster, asked of every member. */
export interface FabricOverview {
  node: string
  clustered: boolean
  local: FabricStatus
  nodes: FabricNodeState[]
  fabrics: FabricSummary[]
}

/** What `GET /api/fabrics/check` serves: would this name and prefix fit on every node? */
export interface FabricCheck {
  name: string
  suggested_name: boolean
  name_error: string
  prefix: string
  suggested_prefix: boolean
  prefix_error: string
  conflicts: { node: string; interface: string; subnet: string }[]
  unreachable: { node: string; error: string }[]
  unreachable_error: string
  /** Set when a member is in maintenance, which stops a fabric being made. */
  maintenance_error: string
  /** node -> the /24 it would be given. */
  allocation: Record<string, string>
  ok: boolean
}

export interface FabricPlan {
  node: string
  commands: FabricCommand[]
  text: string
  privileged: boolean
}

export interface FabricApplied {
  command: string
  why: string
  ok: boolean
  error: string
}

export interface FabricApplyResult {
  node: string
  ok: boolean
  applied: FabricApplied[]
  check: string
  status: FabricStatus
}

/** What creating or extending a fabric did, node by node. */
export interface FabricChangeResult {
  name: string
  prefix: string
  assigned: Record<string, string>
  nodes: { node: string; subnet: string; ok: boolean; error: string }[]
  ok: boolean
}

export interface FabricDeleteResult {
  name: string
  nodes: { node: string; ok: boolean; error: string }[]
  ok: boolean
}

export interface ClusterNode {
  name: string
  url: string
  /** SHA-256 of the TLS certificate it must present, lowercase hex. */
  fingerprint: string
  description: string
  /** Unix seconds; 0 for this node, which was never "added". */
  added: number
  /** Where this node's containers live on the fabric, blank when it is not on one. */
  fabric: FabricClaim
  self: boolean
  groups: string[]
  /** Null when the listing was asked not to contact anyone. */
  state: NodeState | null
  /** Set while the node takes no new instances and no fabric changes. */
  maintenance: Maintenance | null
}

/** A node's maintenance mark: when it began, why, and who set it. */
export interface Maintenance {
  since: number
  reason: string
  by: string
}

export interface MaintenanceResult {
  node: string
  maintenance: Maintenance | null
  /** Each member told about it; one that missed it learns at the next sync. */
  told: { node: string; ok: boolean; error: string | null }[]
}

export interface ClusterNodeDetail extends ClusterNode {
  instances: Container[]
}

export interface NodeGroup {
  name: string
  description: string
  members: string[]
  /** Members that are not nodes here -- removed, or not enrolled yet. */
  unknown_members: string[]
  /** `large` and `small`: sized by the cluster, not editable by hand. */
  managed: boolean
}

/** One node's capacity, and where the sizing put it. */
export interface SizedNode {
  node: string
  ok: boolean
  /** CPU threads and total memory, or zeroes when the node could not be asked. */
  cpu: number
  memory: number
  error: string | null
  /** The node's CPU and memory as a share of the cluster average; 1 is average. */
  score: number | null
  groups: string[]
}

export interface AutoGroupResult {
  groups: Synced<NodeGroup>[]
  nodes: SizedNode[]
  /** Nodes that could not be measured, and so are in neither group. */
  skipped: string[]
  /** Every node was measured and is the same size, so each is in both groups. */
  uniform: boolean
}

/** This node's own place in a cluster. */
export interface ClusterInfo {
  node: ClusterNode
  /** Whether this node holds a cluster credential at all. */
  in_cluster: boolean
  allow_enrollment: boolean
  auth_enabled: boolean
  /**
   * A member with authentication off still demands an API token from callers
   * on other hosts; loopback stays anonymous, so nobody is locked out locally.
   */
  remote_requires_token: boolean
  /** Why a peer could not reach this node as it advertises itself, or ''. */
  reachable_because: string
  fingerprint_pretty: string
  peers: number
  /**
   * Peer records or node groups with no credential to use them with: a cluster
   * this node is in only from its own side, where nothing it calls will work.
   * Leaving is what clears it, so the button is offered on this too.
   */
  leftovers: boolean
  /** Set only when an address was configured by hand rather than worked out. */
  configured_url: string
  settings_path: string
}

/** A one-time code another node redeems to federate with this one. */
export interface JoinCode {
  id: string
  /** Shown once, here: it is a credential. */
  code: string
  expires: number
  expires_in_minutes: number
  node: ClusterNode
  fingerprint_pretty: string
  /** How many nodes the redeemer would be joining. */
  members: number
  /** Set when peers would not actually reach this node at the address in the code. */
  warning: string
}

export interface PendingInvite {
  id: string
  created: number
  expires: number
  note: string
  expired: boolean
}

export interface JoinResult {
  /** The node whose code was redeemed. */
  node: ClusterNode
  /** Everyone this node now knows, itself included. */
  members: ClusterNode[]
  /** Members that could not be told about this node; they catch up on refresh. */
  unreachable: string[]
  /** Set when peers would not actually be able to reach this node. */
  warning: string
  /** The cluster's accounts: taken, or left alone because this node had its own. */
  users: { adopted: string[]; kept: string[]; failed: string[] }
}

export interface MemberSync {
  ok: boolean
  results: { node: string; ok: boolean; error: string | null }[]
  members: ClusterNode[]
}

export interface RotateResult {
  ok: boolean
  nodes: string[]
  /** Members that missed the new credential and are now cut off. */
  stranded: string[]
  results: { node: string; ok: boolean; error: string | null }[]
}

/** One member's answer when it is told to forget a node. */
export interface NodeTold {
  node: string
  ok: boolean
  error: string | null
}

export interface EvictResult {
  removed: string
  /** Whether the evicted node was reached and gave up its credential and members. */
  stood_down: boolean
  stand_down_error: string | null
  /** The remaining members, and whether each was told to forget it. */
  told: NodeTold[]
  /** Set when the credential was replaced as part of the eviction. */
  rotated: RotateResult | null
  still_holds_credential: boolean
}

export interface LeaveResult {
  /** The peers this node has just forgotten. */
  left: string[]
  told: NodeTold[]
  /** Members that could not be told, and so still list this node. */
  stale: string[]
  /**
   * False when this node had no cluster credential and was only clearing the
   * records left behind — nobody could be told, so `told` is empty by design.
   */
  had_credential: boolean
  note: string
}

/** What reconciliation may do to one artifact on one node. */
export type ReconcileAction = 'pull' | 'push' | 'delete'

export interface ReconcileRow {
  /** The node acted on: this one for a pull or a local delete, else the member. */
  node: string
  /** Where the copy came from, for a pull. */
  from: string | null
  kind: SyncKind
  name: string
  action: ReconcileAction
  /** False on a dry run: this is what would have happened. */
  applied: boolean
  ok: boolean
  error: string | null
}

/**
 * An artifact two nodes both hold and disagree about. Never resolved
 * automatically — there is no shared clock and no version to decide with, so
 * someone pushes whichever copy is right.
 */
export interface ReconcileConflict {
  kind: SyncKind
  name: string
  nodes: string[]
}

export interface DriftReport {
  ok: boolean
  /** The node that ran the pass; every local action is attributed to it. */
  node: string
  applied: boolean
  nodes: string[]
  unreachable: { node: string; error: string }[]
  actions: ReconcileRow[]
  conflicts: ReconcileConflict[]
  checked: number
}

export type SyncKind = 'templates' | 'modules' | 'profiles' | 'groups' | 'users' | 'stacks'

export interface SyncOutcome {
  node: string
  kind: SyncKind
  /** Not in the union below when a record kind gains one; treated as opaque. */
  name: string
  ok: boolean
  error: string | null
}

/**
 * What the rest of the cluster made of a save or a delete that was propagated
 * automatically. Null on a lemondx that is not federated, which is most.
 */
export interface AutoSync {
  ok: boolean
  nodes: string[]
  results: SyncOutcome[]
  /** True when this was a removal rather than a push. */
  deleted: boolean
}

/** Anything the cluster keeps level carries this back from a save or delete. */
export type Synced<T> = T & { synced?: AutoSync | null }

export interface SyncResult {
  ok: boolean
  nodes: string[]
  items: number
  results: SyncOutcome[]
}

/** Instances gathered from several nodes, each tagged with the one it is on. */
export interface ClusterContainers {
  nodes: string[]
  instances: (Container & { node: string })[]
  /**
   * Each node's latest health records, as that node's own monitor judged
   * them. Empty for a node with checks off or too old to report them.
   */
  health: (HealthRecord & { node: string })[]
  /** Nodes whose health monitor is running: a record from them is only a matter of time. */
  monitored: string[]
  errors: { node: string; error: string }[]
}

/**
 * Where the Containers and Templates tabs are looking. `local` is this node
 * alone, which is what an unfederated lemondx always shows; the others widen
 * it, and both tabs read the same value so a template's instances and the
 * container list never disagree about which hosts are in view.
 */
export type Scope =
  | { kind: 'local' }
  | { kind: 'cluster' }
  | { kind: 'node'; name: string }
  | { kind: 'group'; name: string }

/** An instance with the node it lives on; `node` is absent in local scope. */
export type ScopedContainer = Container & { node?: string }

/** One instance named together with its node, for an action that may cross hosts. */
export interface InstanceRef {
  node: string
  name: string
}

export interface ScopedStateResult {
  ok: boolean
  instances: { node: string; name: string; ok: boolean; error: string | null }[]
}
