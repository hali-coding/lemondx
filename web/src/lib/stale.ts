import type { StaleKind } from './types'

/** What bringing an instance up to date takes, for each thing it can be behind. */
const REMEDY: Record<StaleKind, string> = {
  template: 'recreate it from the template',
  stack: 'relaunch the stack',
}

/** One sentence: which definitions moved on since the instance was made, and what to do. */
export function staleSummary(stale: StaleKind[], template?: string | null,
                             stack?: string | null) {
  const behind = stale.map((kind) => kind === 'template'
    ? `template “${template ?? '?'}”` : `stack “${stack ?? '?'}”`)
  return `Made from an older version of ${behind.join(' and ')}. To bring it up to date, ${
    stale.map((kind) => REMEDY[kind]).join(', or ')}.`
}
