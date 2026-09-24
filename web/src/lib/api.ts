import type {
  ApiToken, AuthInfo, CreatedApiToken, HealthReport, LocalUser, Role,
  ClusterContainers, ClusterInfo, ClusterNode, ClusterNodeDetail, JoinCode, JoinResult,
  AutoGroupResult, DriftReport, EvictResult, LeaveResult,
  MemberSync, NodeGroup, PendingInvite, RotateResult, SyncKind, SyncResult, Synced,
  InstanceRef, ScopedStateResult,
  BootstrapModule, BootstrapProfile, BootstrapResult, BootstrapSelection, BulkStateResult,
  AppCheckOutput, Container, ModuleSource,
  ContainerDetail, CreateProgress, CreateRequest, ExecResult, ImageBrowse, Images, NetworkDetail,
  InstanceTemplate, NetworkRequest, NetworkSummary, SubnetInUse, Resources, SetupResult, Snapshot, SshKey,
  StateAction, Status, StorageOverview, StoragePoolDetail, StoragePoolRequest,
  StorageVolume, StorageVolumeRequest, TemplateRequest, TemplateRun,
  Stack, StackInstances, StackRun, StackStage, BulkStateResult as StackStateResult,
  FabricStatus,
  FabricPlan,
  FabricApplyResult,
  FabricOverview,
  FabricCheck,
  FabricChangeResult,
  FabricDeleteResult,
  MaintenanceResult,
} from './types'

/** Error carrying the HTTP status so callers can react to 401/409 specifically. */
export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

// Set when the user pastes an API token instead of logging in. Kept in
// sessionStorage so a reload does not re-prompt, but never in localStorage: it
// is a credential. A password login uses an HttpOnly cookie instead, which
// this code never sees.
const TOKEN_KEY = 'lemondx-token'

let authToken: string | null = readStoredToken()

function readStoredToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setToken(token: string | null) {
  authToken = token
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch { /* private mode: fall back to memory only */ }
}

export function hasToken() {
  return authToken !== null
}

async function request<T>(
  path: string,
  options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = {}
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (authToken) headers['Authorization'] = `Bearer ${authToken}`

  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    })
  } catch (cause) {
    if ((cause as Error).name === 'AbortError') throw cause
    throw new ApiError('Cannot reach the lemondx server. Is it still running?', 0)
  }

  const text = await response.text()
  let payload: { data?: T; error?: string } = {}
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      throw new ApiError(`Malformed response from server (HTTP ${response.status})`,
        response.status)
    }
  }
  if (!response.ok) {
    throw new ApiError(payload.error ?? `Request failed (HTTP ${response.status})`,
      response.status)
  }
  return payload.data as T
}

/**
 * One request, described rather than made. The dialogs that change something
 * build theirs from these so the API explorer can show exactly what pressing
 * the button sends -- the same builder `api` uses, so the two cannot drift.
 * `path` is under `/api`.
 */
export interface ApiCall {
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  path: string
  body?: unknown
  /** Dotted body paths the explorer shows as placeholders, never as typed. */
  secrets?: string[]
}

function send<T>(call: ApiCall) {
  return request<T>(call.path, { method: call.method, body: call.body })
}

const seg = encodeURIComponent

/**
 * A path on one node's own API. Undefined means this node, and the plain path;
 * any other name goes through the proxy, which checks the caller's access
 * against the same endpoint's role before forwarding. That is what lets the
 * container drawer manage an instance wherever it lives.
 */
function on(node: string | undefined, path: string) {
  return node ? `/nodes/${encodeURIComponent(node)}${path}` : path
}

/** The requests behind the create and edit dialogs; see `ApiCall`. */
export const calls = {
  /**
   * The UI asks for `background` so the create outlives the page that started
   * it; a script leaving it out gets a call that answers when the create is done.
   */
  createContainer: (body: CreateRequest): ApiCall =>
    ({ method: 'POST', path: '/containers', body: { ...body, background: true } }),

  createStoragePool: (body: StoragePoolRequest): ApiCall =>
    ({ method: 'POST', path: '/storage/pools', body }),

  updateStoragePool: (name: string, body: StoragePoolRequest): ApiCall =>
    ({ method: 'PATCH', path: `/storage/pools/${seg(name)}`, body }),

  deleteStoragePool: (name: string, force = false,
                      expectedPlan?: StoragePoolDetail['delete_plan']): ApiCall =>
    ({ method: 'DELETE', path: `/storage/pools/${seg(name)}?force=${force}`,
       body: force ? { force, confirmation: name, expected_plan: expectedPlan } : undefined }),

  createStorageVolume: (pool: string, body: StorageVolumeRequest): ApiCall =>
    ({ method: 'POST', path: `/storage/pools/${seg(pool)}/volumes`, body }),

  updateStorageVolume: (pool: string, name: string, body: StorageVolumeRequest): ApiCall =>
    ({ method: 'PATCH', path: `/storage/pools/${seg(pool)}/volumes/custom/${seg(name)}`, body }),

  deleteStorageVolume: (pool: string, name: string): ApiCall =>
    ({ method: 'DELETE', path: `/storage/pools/${seg(pool)}/volumes/custom/${seg(name)}` }),

  createNetwork: (body: NetworkRequest): ApiCall =>
    ({ method: 'POST', path: '/networks', body }),

  updateNetwork: (name: string, body: NetworkRequest): ApiCall =>
    ({ method: 'PATCH', path: `/networks/${seg(name)}`, body }),

  deleteNetwork: (name: string): ApiCall =>
    ({ method: 'DELETE', path: `/networks/${seg(name)}` }),

  createFabric: (body: { name: string; prefix: string; nat: boolean }): ApiCall =>
    ({ method: 'POST', path: '/fabrics', body }),

  deleteFabric: (name: string): ApiCall =>
    ({ method: 'DELETE', path: `/fabrics/${seg(name)}` }),

  uploadModule: (name: string, content: string, overwrite = false): ApiCall =>
    ({ method: 'POST', path: '/modules', body: { name, content, overwrite } }),

  saveTemplate: (name: string, body: TemplateRequest): ApiCall =>
    ({ method: 'PUT', path: `/templates/${seg(name)}`, body }),

  launchTemplate: (name: string, body: {
    count: number; prefix?: string; params?: Record<string, string>
    nodes?: string[]; groups?: string[]
  }): ApiCall =>
    ({ method: 'POST', path: `/templates/${seg(name)}/launch`,
       body: { ...body, background: true } }),

  execTemplateInstances: (name: string, command: string,
                          instances: (string | InstanceRef)[], timeout?: number): ApiCall =>
    ({ method: 'POST', path: `/templates/${seg(name)}/exec`,
       body: { command, instances, timeout, background: true } }),

  destroyTemplateInstances: (name: string, instances: (string | InstanceRef)[]): ApiCall =>
    ({ method: 'POST', path: `/templates/${seg(name)}/destroy`,
       body: { instances, background: true } }),

  /** `stale`: `instances` are the stale ones only, and are checked as such. */
  recreateTemplateInstances: (name: string, instances: (string | InstanceRef)[],
                              params?: Record<string, string>, stale = false): ApiCall =>
    ({ method: 'POST', path: `/templates/${seg(name)}/recreate`,
       body: { instances, params, background: true, ...(stale ? { stale: true } : {}) } }),

  launchStack: (name: string, params: Record<string, string>,
                replace?: InstanceRef[]): ApiCall =>
    ({ method: 'POST', path: `/stacks/${seg(name)}/launch`,
       body: { params, replace, background: true } }),

  createApiToken: (body: { name: string; role: Role; expires_days: number | null }): ApiCall =>
    ({ method: 'POST', path: '/auth/tokens', body }),

  saveUser: (name: string, body: { password?: string; role?: Role }): ApiCall =>
    ({ method: 'PUT', path: `/auth/users/${seg(name)}`, body }),

  joinNode: (code: string, description?: string): ApiCall =>
    ({ method: 'POST', path: '/cluster/nodes', body: { code, description } }),

  createInvite: (body: { expires_minutes: number; note?: string }): ApiCall =>
    ({ method: 'POST', path: '/cluster/invites', body }),

  saveNodeGroup: (name: string, body: { members: string[]; description?: string }): ApiCall =>
    ({ method: 'PUT', path: `/cluster/groups/${seg(name)}`, body }),

  syncToNodes: (body: {
    kinds: SyncKind[]; names?: string[]; nodes?: string[]; groups?: string[]
  }): ApiCall => ({ method: 'POST', path: '/cluster/sync', body }),

  /** Asked of the node itself: only its own word counts for its record. */
  setMaintenance: (node: string | undefined, body: { enabled: boolean; reason?: string }):
    ApiCall => ({ method: 'PUT', path: on(node, '/cluster/maintenance'), body }),
}

export const api = {
  status: (signal?: AbortSignal) => request<Status>('/status', { signal }),

  setup: (options: { storage_driver: string; pool_size?: string; ipv6?: boolean }) =>
    request<SetupResult>('/setup', { method: 'POST', body: options }),

  listContainers: (signal?: AbortSignal) =>
    request<Container[]>('/containers', { signal }),

  creates: (signal?: AbortSignal) => request<CreateProgress[]>('/creates', { signal }),

  /** The latest app check run with its full output, from the node it ran on. */
  appCheck: (name: string, node?: string, signal?: AbortSignal) =>
    request<AppCheckOutput>(on(node, `/containers/${encodeURIComponent(name)}/app-check`),
      { signal }),

  getContainer: (name: string, signal?: AbortSignal, node?: string) =>
    request<ContainerDetail>(on(node, `/containers/${encodeURIComponent(name)}`), { signal }),

  /**
   * Starts a create on the server and returns straight away; follow it with
   * `creates()`. Nothing about the create depends on this page afterwards.
   */
  createContainer: (body: CreateRequest) =>
    send<CreateProgress>(calls.createContainer(body)),

  deleteContainer: (name: string, force = false) =>
    request<{ deleted: string }>(
      `/containers/${encodeURIComponent(name)}?force=${force}`, { method: 'DELETE' }),

  setState: (name: string, action: StateAction, force = false) =>
    request<ContainerDetail>(`/containers/${encodeURIComponent(name)}/state`,
      { method: 'POST', body: { action, force } }),

  /** One action over several containers; each one's outcome comes back separately. */
  setStateMany: (names: string[], action: StateAction, force = false) =>
    request<BulkStateResult>('/containers/state',
      { method: 'POST', body: { names, action, force } }),

  updateLimits: (name: string, body: { cpu?: string; memory?: string; description?: string },
                 node?: string) =>
    request<ContainerDetail>(on(node, `/containers/${encodeURIComponent(name)}`),
      { method: 'PATCH', body }),

  rename: (name: string, newName: string, node?: string) =>
    request<ContainerDetail>(on(node, `/containers/${encodeURIComponent(name)}/rename`),
      { method: 'POST', body: { name: newName } }),

  exec: (name: string, command: string, node?: string) =>
    request<ExecResult>(on(node, `/containers/${encodeURIComponent(name)}/exec`),
      { method: 'POST', body: { command } }),

  snapshots: (name: string, node?: string) =>
    request<Snapshot[]>(on(node, `/containers/${encodeURIComponent(name)}/snapshots`)),

  createSnapshot: (name: string, snapshot: string, stateful = false, node?: string) =>
    request<{ created: string }>(on(node, `/containers/${encodeURIComponent(name)}/snapshots`),
      { method: 'POST', body: { name: snapshot, stateful } }),

  deleteSnapshot: (name: string, snapshot: string, node?: string) =>
    request<{ deleted: string }>(on(node,
      `/containers/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snapshot)}`),
      { method: 'DELETE' }),

  restoreSnapshot: (name: string, snapshot: string, node?: string) =>
    request<{ restored: string }>(on(node,
      `/containers/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snapshot)}/restore`),
      { method: 'POST' }),

  images: (signal?: AbortSignal) => request<Images>('/images', { signal }),

  browseImages: (options: { remote?: string; refresh?: boolean } = {},
                 signal?: AbortSignal) => {
    const query = new URLSearchParams()
    if (options.remote) query.set('remote', options.remote)
    if (options.refresh) query.set('refresh', 'true')
    const suffix = query.toString() ? `?${query}` : ''
    return request<ImageBrowse>(`/images/browse${suffix}`, { signal })
  },

  resources: (signal?: AbortSignal) => request<Resources>('/resources', { signal }),

  storage: (signal?: AbortSignal) => request<StorageOverview>('/storage', { signal }),

  createStoragePool: (body: StoragePoolRequest) =>
    send<StoragePoolDetail>(calls.createStoragePool(body)),

  updateStoragePool: (name: string, body: StoragePoolRequest) =>
    send<StoragePoolDetail>(calls.updateStoragePool(name, body)),

  deleteStoragePool: (name: string, force = false,
                      expectedPlan?: StoragePoolDetail['delete_plan']) =>
    send<{ deleted: string; detached: boolean }>(
      calls.deleteStoragePool(name, force, expectedPlan)),

  createStorageVolume: (pool: string, body: StorageVolumeRequest) =>
    send<StorageVolume>(calls.createStorageVolume(pool, body)),

  updateStorageVolume: (pool: string, name: string, body: StorageVolumeRequest) =>
    send<StorageVolume>(calls.updateStorageVolume(pool, name, body)),

  deleteStorageVolume: (pool: string, name: string) =>
    send<{ deleted: string; pool: string }>(calls.deleteStorageVolume(pool, name)),

  networks: (signal?: AbortSignal) =>
    request<NetworkSummary[]>('/networks', { signal }),

  network: (name: string, signal?: AbortSignal) =>
    request<NetworkDetail>(`/networks/${encodeURIComponent(name)}`, { signal }),

  subnets: (signal?: AbortSignal) => request<SubnetInUse[]>('/subnets', { signal }),

  createNetwork: (body: NetworkRequest) =>
    send<NetworkDetail>(calls.createNetwork(body)),

  updateNetwork: (name: string, body: NetworkRequest) =>
    send<NetworkDetail>(calls.updateNetwork(name, body)),

  deleteNetwork: (name: string) =>
    send<{ deleted: string }>(calls.deleteNetwork(name)),

  // Fabrics. `/fabrics` is the whole cluster and asks every member; `/fabric`
  // is this node's half. Status and plan need no privilege on the host, so
  // they answer even where routes cannot be programmed.
  fabrics: (signal?: AbortSignal) => request<FabricOverview>('/fabrics', { signal }),

  fabricCheck: (name: string, prefix: string, signal?: AbortSignal) => {
    const query = new URLSearchParams({ name, prefix })
    return request<FabricCheck>(`/fabrics/check?${query}`, { signal })
  },

  createFabric: (body: { name: string; prefix: string; nat: boolean }) =>
    send<FabricChangeResult>(calls.createFabric(body)),

  extendFabric: (name: string) =>
    request<FabricChangeResult>(`/fabrics/${encodeURIComponent(name)}/extend`, { method: 'POST' }),

  deleteFabric: (name: string) =>
    send<FabricDeleteResult>(calls.deleteFabric(name)),

  fabric: (signal?: AbortSignal) => request<FabricStatus>('/fabric', { signal }),

  fabricPlan: (signal?: AbortSignal) => request<FabricPlan>('/fabric/plan', { signal }),

  fabricApply: () => request<FabricApplyResult>('/fabric/apply', { method: 'POST' }),

  fabricAttach: (name: string, fabric?: string) =>
    request<Container>(`/fabric/instances/${encodeURIComponent(name)}/attach`,
      { method: 'POST', body: { fabric } }),

  fabricDetach: (name: string, fabric?: string) =>
    request<Container>(`/fabric/instances/${encodeURIComponent(name)}/detach`,
      { method: 'POST', body: { fabric } }),

  // Bootstrapping a container runs modules on *its* node and installs keys
  // that node can see, so the pickers come from there too.
  modules: (signal?: AbortSignal, node?: string) =>
    request<BootstrapModule[]>(on(node, '/modules'), { signal }),

  uploadModule: (name: string, content: string, overwrite = false) =>
    send<Synced<BootstrapModule>>(calls.uploadModule(name, content, overwrite)),

  moduleSource: (id: string, signal?: AbortSignal) =>
    request<ModuleSource>(`/modules/${encodeURIComponent(id)}/source`, { signal }),

  updateModuleSettings: (id: string,
                         body: { params?: Record<string, string>; is_default?: boolean }) =>
    request<BootstrapModule>(`/modules/${encodeURIComponent(id)}/settings`,
      { method: 'PUT', body }),

  deleteModule: (id: string, everywhere = true) =>
    request<Synced<{ deleted: string; restored_builtin: boolean }>>(
      `/modules/${encodeURIComponent(id)}?everywhere=${everywhere}`, { method: 'DELETE' }),

  bootstrapProfiles: (signal?: AbortSignal, node?: string) =>
    request<BootstrapProfile[]>(on(node, '/bootstrap-profiles'), { signal }),

  saveBootstrapProfile: (name: string, body: {
    modules: string[]; params?: Record<string, string>; ssh_keys?: string[]
    description?: string
  }) => request<Synced<BootstrapProfile>>(`/bootstrap-profiles/${encodeURIComponent(name)}`,
    { method: 'PUT', body }),

  deleteBootstrapProfile: (name: string, everywhere = true) =>
    request<Synced<{ deleted: string }>>(
      `/bootstrap-profiles/${encodeURIComponent(name)}?everywhere=${everywhere}`,
      { method: 'DELETE' }),

  templates: (signal?: AbortSignal) =>
    request<InstanceTemplate[]>('/templates', { signal }),

  // In a cluster these are kept level automatically: saving pushes to every
  // member, and deleting removes it from them unless `everywhere` says not to.
  // The answer carries `synced` saying what each node made of it.
  saveTemplate: (name: string, body: TemplateRequest) =>
    send<Synced<InstanceTemplate>>(calls.saveTemplate(name, body)),

  deleteTemplate: (name: string, everywhere = true) =>
    request<Synced<{ deleted: string }>>(
      `/templates/${encodeURIComponent(name)}?everywhere=${everywhere}`,
      { method: 'DELETE' }),

  /**
   * Starts a launch and returns its run record at once. `nodes`/`groups` spread
   * it across a cluster; with neither it runs here, exactly as it always did.
   */
  launchTemplate: (name: string,
                   body: {
                     count: number; prefix?: string; params?: Record<string, string>
                     nodes?: string[]; groups?: string[]
                   }) =>
    send<TemplateRun>(calls.launchTemplate(name, body)),

  templateRuns: (signal?: AbortSignal) =>
    request<TemplateRun[]>('/template-runs', { signal }),

  dismissTemplateRun: (name: string) =>
    request<{ dismissed: string }>(`/template-runs/${encodeURIComponent(name)}`,
      { method: 'DELETE' }),

  // Launch, recreate and destroy start a run and return it at once; follow it
  // with templateRuns(). `instances` must be exactly the ones the user
  // confirmed, or nothing happens.
  // `instances` is plain names for this node, or {node, name} entries once the
  // view spans the cluster; the server hands each node its own share.
  execTemplateInstances: (name: string, command: string,
                          instances: (string | InstanceRef)[], timeout?: number) =>
    send<TemplateRun>(calls.execTemplateInstances(name, command, instances, timeout)),

  destroyTemplateInstances: (name: string, instances: (string | InstanceRef)[]) =>
    send<TemplateRun>(calls.destroyTemplateInstances(name, instances)),

  recreateTemplateInstances: (name: string, instances: (string | InstanceRef)[],
                              params?: Record<string, string>, stale = false) =>
    send<TemplateRun>(calls.recreateTemplateInstances(name, instances, params, stale)),

  stacks: (signal?: AbortSignal) =>
    request<Stack[]>('/stacks', { signal }),

  /** Kept level across a cluster like templates, with the templates it uses. */
  saveStack: (name: string, body: { description: string; stages: StackStage[] }) =>
    request<Synced<Stack>>(`/stacks/${encodeURIComponent(name)}`, { method: 'PUT', body }),

  deleteStack: (name: string, everywhere = true) =>
    request<Synced<{ deleted: string }>>(
      `/stacks/${encodeURIComponent(name)}?everywhere=${everywhere}`,
      { method: 'DELETE' }),

  /**
   * Starts a stack and returns its run record at once; follow it with
   * stackRuns(). `replace` makes it a relaunch: exactly those instances, as the
   * user confirmed them, are destroyed first.
   */
  launchStack: (name: string, params: Record<string, string>, replace?: InstanceRef[]) =>
    send<StackRun>(calls.launchStack(name, params, replace)),

  /** What every stack is running, by the tags on the instances, across the cluster. */
  stackInstances: (signal?: AbortSignal) =>
    request<StackInstances>('/stack-instances', { signal }),

  stackState: (name: string, action: 'start' | 'stop' | 'restart', instances: InstanceRef[]) =>
    request<StackStateResult>(`/stacks/${encodeURIComponent(name)}/state`,
      { method: 'POST', body: { action, instances } }),

  destroyStack: (name: string, instances: InstanceRef[]) =>
    request<StackRun>(`/stacks/${encodeURIComponent(name)}/destroy`,
      { method: 'POST', body: { instances, background: true } }),

  stackRuns: (signal?: AbortSignal) =>
    request<StackRun[]>('/stack-runs', { signal }),

  cancelStackRun: (name: string) =>
    request<StackRun>(`/stack-runs/${encodeURIComponent(name)}/cancel`, { method: 'POST' }),

  dismissStackRun: (name: string) =>
    request<{ dismissed: string }>(`/stack-runs/${encodeURIComponent(name)}`,
      { method: 'DELETE' }),

  sshKeys: (signal?: AbortSignal, node?: string) =>
    request<SshKey[]>(on(node, '/ssh-keys'), { signal }),

  validateSshKey: (key: string) =>
    request<SshKey>('/ssh-keys/validate', { method: 'POST', body: { key } }),

  bootstrap: (name: string, selection: BootstrapSelection, node?: string) =>
    request<BootstrapResult>(on(node, `/containers/${encodeURIComponent(name)}/bootstrap`),
      { method: 'POST', body: selection }),

  /** The server's latest health checks; they run on its own schedule, not on request. */
  health: (signal?: AbortSignal) => request<HealthReport>('/health', { signal }),

  /** Answers without credentials too: what the server accepts, and who we are. */
  authInfo: (signal?: AbortSignal) => request<AuthInfo>('/auth', { signal }),

  login: (username: string, password: string) =>
    request<AuthInfo>('/auth/login', { method: 'POST', body: { username, password } }),

  logout: () => request<{ logged_out: boolean }>('/auth/logout', { method: 'POST' }),

  apiTokens: (signal?: AbortSignal) => request<ApiToken[]>('/auth/tokens', { signal }),

  createApiToken: (body: { name: string; role: Role; expires_days: number | null }) =>
    send<CreatedApiToken>(calls.createApiToken(body)),

  revokeApiToken: (id: string) =>
    request<{ revoked: string }>(`/auth/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  cluster: (signal?: AbortSignal) => request<ClusterInfo>('/cluster', { signal }),

  /** `probe: false` lists what is registered without contacting any of it. */
  nodes: (signal?: AbortSignal, probe = true) =>
    request<ClusterNode[]>(`/cluster/nodes${probe ? '' : '?probe=false'}`, { signal }),

  node: (name: string, signal?: AbortSignal) =>
    request<ClusterNodeDetail>(`/cluster/nodes/${encodeURIComponent(name)}`, { signal }),

  /** Redeems a join code issued by the node being joined. */
  joinNode: (code: string, description?: string) =>
    send<JoinResult>(calls.joinNode(code, description)),

  /**
   * Puts a node out of the cluster: it is told to stand down, every other
   * member is told to forget it. `rotate` overrides when the shared credential
   * is replaced — by default only when the node could not be told, since
   * rotating also strands any member that happens to be switched off.
   */
  evictNode: (name: string, rotate?: boolean) =>
    request<EvictResult>(
      `/cluster/nodes/${encodeURIComponent(name)}`
      + (rotate === undefined ? '' : `?rotate=${rotate}`), { method: 'DELETE' }),

  setMaintenance: (node: string | undefined, body: { enabled: boolean; reason?: string }) =>
    send<MaintenanceResult>(calls.setMaintenance(node, body)),

  /** Pull every peer's member list and push ours, so membership converges. */
  refreshMembers: () => request<MemberSync>('/cluster/refresh', { method: 'POST' }),

  /** Replace the cluster credential everywhere — what actually cuts a node off. */
  rotateCluster: () => request<RotateResult>('/cluster/rotate', { method: 'POST' }),

  /** The other end of an eviction: this node steps out and every member is told. */
  leaveCluster: () => request<LeaveResult>('/cluster/leave', { method: 'POST' }),

  /**
   * Settle this node's shared definitions against every member's. The pull
   * half of sync: what a node missed while it was switched off, and what it
   * deleted while somebody else was.
   */
  reconcile: (apply = true) =>
    request<DriftReport>('/cluster/reconcile', { method: 'POST', body: { apply } }),

  /**
   * What the last pass found, without setting another going. Null until one
   * has run in this `serve` process — the report is held in memory like a
   * template run, so a restart forgets it and the startup pass fills it in.
   */
  drift: (signal?: AbortSignal) =>
    request<DriftReport | null>('/cluster/drift', { signal }),

  clusterContainers: (targets: { nodes?: string[]; groups?: string[]; all?: boolean } = {},
                      signal?: AbortSignal) => {
    const query = new URLSearchParams()
    if (targets.all) query.set('all', 'true')
    if (targets.nodes?.length) query.set('nodes', targets.nodes.join(','))
    if (targets.groups?.length) query.set('groups', targets.groups.join(','))
    const suffix = query.toString() ? `?${query}` : ''
    return request<ClusterContainers>(`/cluster/containers${suffix}`, { signal })
  },

  /** One state change over instances that may sit on different nodes. */
  setStateAcross: (instances: InstanceRef[], action: StateAction, force = false) =>
    request<ScopedStateResult>('/cluster/containers/state',
      { method: 'POST', body: { instances, action, force } }),

  deleteAcross: (instances: InstanceRef[], force = false) =>
    request<ScopedStateResult>('/cluster/containers/delete',
      { method: 'POST', body: { instances, force } }),

  nodeGroups: (signal?: AbortSignal) =>
    request<NodeGroup[]>('/cluster/groups', { signal }),

  /**
   * Rebuilds `large` and `small` from each node's CPU and memory. Those two
   * groups are the cluster's own reading of itself, so this is the only thing
   * that writes them — the save and delete calls below refuse them.
   */
  autoGroups: () => request<AutoGroupResult>('/cluster/groups/auto', { method: 'POST' }),

  saveNodeGroup: (name: string, body: { members: string[]; description?: string }) =>
    send<Synced<NodeGroup>>(calls.saveNodeGroup(name, body)),

  deleteNodeGroup: (name: string, everywhere = true) =>
    request<Synced<{ deleted: string }>>(
      `/cluster/groups/${encodeURIComponent(name)}?everywhere=${everywhere}`,
      { method: 'DELETE' }),

  invites: (signal?: AbortSignal) =>
    request<PendingInvite[]>('/cluster/invites', { signal }),

  /** The code comes back once and is never retrievable again. */
  createInvite: (body: { expires_minutes: number; note?: string }) =>
    send<JoinCode>(calls.createInvite(body)),

  revokeInvite: (id: string) =>
    request<{ revoked: string }>(`/cluster/invites/${encodeURIComponent(id)}`,
      { method: 'DELETE' }),

  syncToNodes: (body: {
    kinds: SyncKind[]; names?: string[]; nodes?: string[]; groups?: string[]
  }) => send<SyncResult>(calls.syncToNodes(body)),

  users: (signal?: AbortSignal) => request<LocalUser[]>('/auth/users', { signal }),

  /** Creates the user, or changes an existing one's password and/or role. */
  saveUser: (name: string, body: { password?: string; role?: Role }) =>
    send<{ name: string; role: Role; created: boolean }>(calls.saveUser(name, body)),

  removeUser: (name: string) =>
    request<{ removed: string }>(`/auth/users/${encodeURIComponent(name)}`, { method: 'DELETE' }),
}
