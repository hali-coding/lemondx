import type { ApiCall } from './api'

/**
 * Turns an `ApiCall` into code a person can paste: the API explorer's half of
 * "the web UI is only an API client". Everything here is presentation -- the
 * request itself comes from the same builder `api` sends.
 */

export type Language = 'curl' | 'javascript' | 'python'

export const LANGUAGES: { id: Language; label: string }[] = [
  { id: 'curl', label: 'curl' },
  { id: 'javascript', label: 'JavaScript' },
  { id: 'python', label: 'Python' },
]

/** The variable every snippet reads the token from, as in docs/api.md. */
export const TOKEN_VAR = 'LEMONDX_API_TOKEN'

export interface SnippetOptions {
  /** Where the API lives, e.g. `http://localhost:8099`. */
  origin: string
  /** Whether to send a bearer token: only when the server asks for one. */
  auth: boolean
}

/**
 * The body with every value named in `call.secrets` replaced by a placeholder.
 * A secret typed into the form is still a secret: showing it in a code block
 * is one screenshot away from a chat channel.
 */
export function maskedBody(call: ApiCall): unknown {
  if (call.body === undefined || !call.secrets?.length) return call.body
  const body = structuredClone(call.body) as Record<string, unknown>
  for (const path of call.secrets) {
    const keys = path.split('.')
    let node: unknown = body
    for (const key of keys.slice(0, -1)) {
      node = node && typeof node === 'object' ? (node as Record<string, unknown>)[key] : undefined
    }
    const last = keys[keys.length - 1]
    if (node && typeof node === 'object' && last in node) {
      (node as Record<string, unknown>)[last] = `<${last}>`
    }
  }
  return body
}

export function snippet(language: Language, call: ApiCall, options: SnippetOptions): string {
  const url = `${options.origin}/api${call.path}`
  const body = maskedBody(call)
  switch (language) {
    case 'curl': return curl(call.method, url, body, options.auth)
    case 'javascript': return javascript(call.method, url, body, options.auth)
    case 'python': return python(call.method, url, body, options.auth)
  }
}

function curl(method: string, url: string, body: unknown, auth: boolean) {
  const lines = [`curl -sS${method === 'GET' ? '' : ` -X ${method}`} ${shellQuote(url)}`]
  if (auth) lines.push(`-H "Authorization: Bearer $${TOKEN_VAR}"`)
  if (body === undefined) return lines.join(' \\\n  ')
  lines.push(`-H 'Content-Type: application/json'`, '--data-binary @-')
  // A quoted heredoc rather than -d '...': bodies carry shell scripts (module
  // uploads, app checks, exec) full of quotes and dollars, and a quoted
  // delimiter passes all of it through untouched.
  return `${lines.join(' \\\n  ')} <<'JSON'\n${JSON.stringify(body, null, 2)}\nJSON`
}

function shellQuote(text: string) {
  return `'${text.replace(/'/g, `'\\''`)}'`
}

function javascript(method: string, url: string, body: unknown, auth: boolean) {
  const headers: string[] = []
  if (auth) headers.push(`    Authorization: \`Bearer \${process.env.${TOKEN_VAR}}\`,`)
  if (body !== undefined) headers.push(`    'Content-Type': 'application/json',`)
  const init = [`  method: '${method}',`]
  if (headers.length) init.push('  headers: {', ...headers, '  },')
  if (body !== undefined) {
    init.push(`  body: JSON.stringify(${indent(JSON.stringify(body, null, 2), '  ')}),`)
  }
  return [
    `const response = await fetch(${JSON.stringify(url)}, {`,
    ...init,
    '})',
    'const { data, error } = await response.json()',
    'if (!response.ok) throw new Error(error)',
    'console.log(data)',
  ].join('\n')
}

function python(method: string, url: string, body: unknown, auth: boolean) {
  const lines = ['import requests']
  if (auth) lines.unshift('import os')
  lines.push('', `response = requests.${method.toLowerCase()}(`, `    ${JSON.stringify(url)},`)
  if (auth) {
    lines.push(`    headers={"Authorization": f"Bearer {os.environ['${TOKEN_VAR}']}"},`)
  }
  if (body !== undefined) lines.push(`    json=${indent(pythonLiteral(body), '    ')},`)
  lines.push(
    ')',
    'payload = response.json()',
    'if not response.ok:',
    '    raise RuntimeError(payload["error"])',
    'print(payload["data"])',
  )
  return lines.join('\n')
}

/** JSON is nearly Python; only the three literals are spelled differently. */
function pythonLiteral(value: unknown, depth = 0): string {
  const pad = '    '.repeat(depth + 1)
  const close = '    '.repeat(depth)
  if (value === null || value === undefined) return 'None'
  if (value === true) return 'True'
  if (value === false) return 'False'
  if (typeof value === 'number' || typeof value === 'string') return JSON.stringify(value)
  if (Array.isArray(value)) {
    if (!value.length) return '[]'
    return `[\n${value.map((item) => `${pad}${pythonLiteral(item, depth + 1)},`).join('\n')}\n${close}]`
  }
  // Undefined members are what JSON.stringify drops, so the server never sees them.
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => item !== undefined)
  if (!entries.length) return '{}'
  return `{\n${entries.map(([key, item]) =>
    `${pad}${JSON.stringify(key)}: ${pythonLiteral(item, depth + 1)},`).join('\n')}\n${close}}`
}

function indent(text: string, by: string) {
  return text.split('\n').map((line, index) => (index ? by + line : line)).join('\n')
}
