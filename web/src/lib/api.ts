import type {
  ApiToken, AuthInfo, CreatedApiToken, HealthReport, LocalUser, Role,
  BootstrapModule, BootstrapProfile, BootstrapResult, BootstrapSelection, BulkStateResult,
  Container, ModuleSource,
  ContainerDetail, CreateProgress, CreateRequest, ExecResult, ImageBrowse, Images, NetworkDetail,
  InstanceTemplate, NetworkRequest, NetworkSummary, SubnetInUse, Resources, SetupResult, Snapshot, SshKey,
  StateAction, Status, StorageOverview, StoragePoolDetail, StoragePoolRequest,
  StorageVolume, StorageVolumeRequest, TemplateRequest, TemplateRun,
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

export const api = {
  status: (signal?: AbortSignal) => request<Status>('/status', { signal }),

  setup: (options: { storage_driver: string; pool_size?: string; ipv6?: boolean }) =>
    request<SetupResult>('/setup', { method: 'POST', body: options }),

  listContainers: (signal?: AbortSignal) =>
    request<Container[]>('/containers', { signal }),

  creates: (signal?: AbortSignal) => request<CreateProgress[]>('/creates', { signal }),

  getContainer: (name: string, signal?: AbortSignal) =>
    request<ContainerDetail>(`/containers/${encodeURIComponent(name)}`, { signal }),

  /**
   * Starts a create on the server and returns straight away; follow it with
   * `creates()`. Nothing about the create depends on this page afterwards.
   */
  createContainer: (body: CreateRequest) =>
    request<CreateProgress>('/containers', { method: 'POST', body: { ...body, background: true } }),

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

  updateLimits: (name: string, body: { cpu?: string; memory?: string; description?: string }) =>
    request<ContainerDetail>(`/containers/${encodeURIComponent(name)}`,
      { method: 'PATCH', body }),

  rename: (name: string, newName: string) =>
    request<ContainerDetail>(`/containers/${encodeURIComponent(name)}/rename`,
      { method: 'POST', body: { name: newName } }),

  exec: (name: string, command: string) =>
    request<ExecResult>(`/containers/${encodeURIComponent(name)}/exec`,
      { method: 'POST', body: { command } }),

  snapshots: (name: string) =>
    request<Snapshot[]>(`/containers/${encodeURIComponent(name)}/snapshots`),

  createSnapshot: (name: string, snapshot: string, stateful = false) =>
    request<{ created: string }>(`/containers/${encodeURIComponent(name)}/snapshots`,
      { method: 'POST', body: { name: snapshot, stateful } }),

  deleteSnapshot: (name: string, snapshot: string) =>
    request<{ deleted: string }>(
      `/containers/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snapshot)}`,
      { method: 'DELETE' }),

  restoreSnapshot: (name: string, snapshot: string) =>
    request<{ restored: string }>(
      `/containers/${encodeURIComponent(name)}/snapshots/${encodeURIComponent(snapshot)}/restore`,
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
    request<StoragePoolDetail>('/storage/pools', { method: 'POST', body }),

  updateStoragePool: (name: string, body: StoragePoolRequest) =>
    request<StoragePoolDetail>(`/storage/pools/${encodeURIComponent(name)}`,
      { method: 'PATCH', body }),

  deleteStoragePool: (name: string, force = false,
                      expectedPlan?: StoragePoolDetail['delete_plan']) =>
    request<{ deleted: string; detached: boolean }>(
      `/storage/pools/${encodeURIComponent(name)}?force=${force}`,
      { method: 'DELETE', body: force
        ? { force, confirmation: name, expected_plan: expectedPlan }
        : undefined }),

  createStorageVolume: (pool: string, body: StorageVolumeRequest) =>
    request<StorageVolume>(`/storage/pools/${encodeURIComponent(pool)}/volumes`,
      { method: 'POST', body }),

  updateStorageVolume: (pool: string, name: string, body: StorageVolumeRequest) =>
    request<StorageVolume>(
      `/storage/pools/${encodeURIComponent(pool)}/volumes/custom/${encodeURIComponent(name)}`,
      { method: 'PATCH', body }),

  deleteStorageVolume: (pool: string, name: string) =>
    request<{ deleted: string; pool: string }>(
      `/storage/pools/${encodeURIComponent(pool)}/volumes/custom/${encodeURIComponent(name)}`,
      { method: 'DELETE' }),

  networks: (signal?: AbortSignal) =>
    request<NetworkSummary[]>('/networks', { signal }),

  network: (name: string, signal?: AbortSignal) =>
    request<NetworkDetail>(`/networks/${encodeURIComponent(name)}`, { signal }),

  subnets: (signal?: AbortSignal) => request<SubnetInUse[]>('/subnets', { signal }),

  createNetwork: (body: NetworkRequest) =>
    request<NetworkDetail>('/networks', { method: 'POST', body }),

  updateNetwork: (name: string, body: NetworkRequest) =>
    request<NetworkDetail>(`/networks/${encodeURIComponent(name)}`, { method: 'PATCH', body }),

  deleteNetwork: (name: string) =>
    request<{ deleted: string }>(`/networks/${encodeURIComponent(name)}`, { method: 'DELETE' }),

  modules: (signal?: AbortSignal) => request<BootstrapModule[]>('/modules', { signal }),

  uploadModule: (name: string, content: string, overwrite = false) =>
    request<BootstrapModule>('/modules',
      { method: 'POST', body: { name, content, overwrite } }),

  moduleSource: (id: string, signal?: AbortSignal) =>
    request<ModuleSource>(`/modules/${encodeURIComponent(id)}/source`, { signal }),

  updateModuleSettings: (id: string,
                         body: { params?: Record<string, string>; is_default?: boolean }) =>
    request<BootstrapModule>(`/modules/${encodeURIComponent(id)}/settings`,
      { method: 'PUT', body }),

  deleteModule: (id: string) =>
    request<{ deleted: string; restored_builtin: boolean }>(`/modules/${encodeURIComponent(id)}`,
      { method: 'DELETE' }),

  bootstrapProfiles: (signal?: AbortSignal) =>
    request<BootstrapProfile[]>('/bootstrap-profiles', { signal }),

  saveBootstrapProfile: (name: string, body: {
    modules: string[]; params?: Record<string, string>; ssh_keys?: string[]
    description?: string
  }) => request<BootstrapProfile>(`/bootstrap-profiles/${encodeURIComponent(name)}`,
    { method: 'PUT', body }),

  deleteBootstrapProfile: (name: string) =>
    request<{ deleted: string }>(`/bootstrap-profiles/${encodeURIComponent(name)}`,
      { method: 'DELETE' }),

  templates: (signal?: AbortSignal) =>
    request<InstanceTemplate[]>('/templates', { signal }),

  saveTemplate: (name: string, body: TemplateRequest) =>
    request<InstanceTemplate>(`/templates/${encodeURIComponent(name)}`,
      { method: 'PUT', body }),

  deleteTemplate: (name: string) =>
    request<{ deleted: string }>(`/templates/${encodeURIComponent(name)}`,
      { method: 'DELETE' }),

  launchTemplate: (name: string,
                   body: { count: number; prefix?: string; params?: Record<string, string> }) =>
    request<TemplateRun>(`/templates/${encodeURIComponent(name)}/launch`,
      { method: 'POST', body: { ...body, background: true } }),

  templateRuns: (signal?: AbortSignal) =>
    request<TemplateRun[]>('/template-runs', { signal }),

  dismissTemplateRun: (name: string) =>
    request<{ dismissed: string }>(`/template-runs/${encodeURIComponent(name)}`,
      { method: 'DELETE' }),

  // Launch, recreate and destroy start a run and return it at once; follow it
  // with templateRuns(). `instances` must be exactly the ones the user
  // confirmed, or nothing happens.
  execTemplateInstances: (name: string, command: string, instances: string[],
                          timeout?: number) =>
    request<TemplateRun>(`/templates/${encodeURIComponent(name)}/exec`,
      { method: 'POST', body: { command, instances, timeout, background: true } }),

  destroyTemplateInstances: (name: string, instances: string[]) =>
    request<TemplateRun>(`/templates/${encodeURIComponent(name)}/destroy`,
      { method: 'POST', body: { instances, background: true } }),

  recreateTemplateInstances: (name: string, instances: string[],
                              params?: Record<string, string>) =>
    request<TemplateRun>(`/templates/${encodeURIComponent(name)}/recreate`,
      { method: 'POST', body: { instances, params, background: true } }),

  sshKeys: (signal?: AbortSignal) => request<SshKey[]>('/ssh-keys', { signal }),

  validateSshKey: (key: string) =>
    request<SshKey>('/ssh-keys/validate', { method: 'POST', body: { key } }),

  bootstrap: (name: string, selection: BootstrapSelection) =>
    request<BootstrapResult>(`/containers/${encodeURIComponent(name)}/bootstrap`,
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
    request<CreatedApiToken>('/auth/tokens', { method: 'POST', body }),

  revokeApiToken: (id: string) =>
    request<{ revoked: string }>(`/auth/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  users: (signal?: AbortSignal) => request<LocalUser[]>('/auth/users', { signal }),

  /** Creates the user, or changes an existing one's password and/or role. */
  saveUser: (name: string, body: { password?: string; role?: Role }) =>
    request<{ name: string; role: Role; created: boolean }>(
      `/auth/users/${encodeURIComponent(name)}`, { method: 'PUT', body }),

  removeUser: (name: string) =>
    request<{ removed: string }>(`/auth/users/${encodeURIComponent(name)}`, { method: 'DELETE' }),
}
