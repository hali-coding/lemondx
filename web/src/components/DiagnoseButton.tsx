import type { ModuleResult } from '../lib/types'

// Google reads at most 32 words of a query; the first line is what names the
// failure, and the rest is usually context that would stop an exact match.
const MAX_CHARS = 200

/** The part of an error worth searching for: one line, without our own prefixes. */
function phrase(text: string) {
  const line = text.split('\n').map((l) => l.trim()).find(Boolean) ?? ''
  // The module prelude's `die` and `log` markers are ours, not the tool's.
  // A double quote inside would end the quoted phrase early and split the
  // search into fragments; Google ignores the punctuation anyway.
  const bare = line.replace(/^(error:|warning:|==>|!)\s*/i, '').replace(/["\u201c\u201d]/g, '')
  if (bare.length <= MAX_CHARS) return bare
  const cut = bare.slice(0, MAX_CHARS)
  return cut.slice(0, cut.lastIndexOf(' ') > 0 ? cut.lastIndexOf(' ') : MAX_CHARS)
}

/** What a failed module said last: the line that usually names what went wrong. */
function moduleError(module: ModuleResult) {
  const lines = (module.stderr || module.stdout || '').split('\n').map((l) => l.trim())
    .filter(Boolean)
  return lines[lines.length - 1] ?? ''
}

/**
 * Search the web for an error -- or for what a failed module said last --
 * quoted so it is matched as written. Opens in a new tab, and sends only the
 * error's text, which can still name hosts, paths or addresses: so it is a
 * button someone presses, never automatic.
 */
export function DiagnoseButton({ error, module }: { error?: string | null; module?: ModuleResult }) {
  const query = phrase(error || (module ? moduleError(module) : ''))
  if (!query) return null
  const url = `https://www.google.com/search?q=${encodeURIComponent(`"${query}"`)}`
  return (
    <button type="button" className="btn btn-sm btn-ghost diagnose-btn"
      title={`Search the web for “${query}”`}
      onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}>
      Diagnose
    </button>
  )
}
