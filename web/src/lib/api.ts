import type {
  Container, ContainerDetail, CreateRequest, ExecResult, Images,
  SetupResult, Snapshot, StateAction, Status,
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

// Set when the backend was started with --token. Kept in sessionStorage so a
// reload does not re-prompt, but never in localStorage: it is a credential.
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

  getContainer: (name: string, signal?: AbortSignal) =>
    request<ContainerDetail>(`/containers/${encodeURIComponent(name)}`, { signal }),

  createContainer: (body: CreateRequest) =>
    request<ContainerDetail>('/containers', { method: 'POST', body }),

  deleteContainer: (name: string, force = false) =>
    request<{ deleted: string }>(
      `/containers/${encodeURIComponent(name)}?force=${force}`, { method: 'DELETE' }),

  setState: (name: string, action: StateAction, force = false) =>
    request<ContainerDetail>(`/containers/${encodeURIComponent(name)}/state`,
      { method: 'POST', body: { action, force } }),

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
}
