import type { BootstrapModule, BootstrapSelection } from './types'

/** How many times one module may be added to a selection; the server's MAX_OCCURRENCES. */
export const MAX_OCCURRENCES = 16

/**
 * The key occurrence `n` of a module keeps a parameter under, mirroring
 * `bootstrap.param_key()`: NAME for the first, NAME@2, NAME@3… for repeats.
 */
export function occurrenceKey(name: string, n: number) {
  return n === 1 ? name : `${name}@${n}`
}

/**
 * The parameter name a selection's key belongs to, mirroring
 * `bootstrap.split_param()`: NAME@n is NAME, anything else is itself. Compare
 * a key with module metadata through this, or a repeat's key slips past.
 */
export function paramName(key: string) {
  const match = /^([A-Z][A-Z0-9_]*)@([1-9][0-9]?)$/.exec(key)
  return match && Number(match[2]) > 1 ? match[1] : key
}

/** `[module, n]` per entry of a selection, n counting that module's entries so far. */
export function occurrences(ids: string[]): [string, number][] {
  const seen = new Map<string, number>()
  return ids.map((id) => {
    const n = (seen.get(id) ?? 0) + 1
    seen.set(id, n)
    return [id, n]
  })
}

/** Every selected module with each time it was added: the unit a run is made of. */
function selectedOccurrences(modules: BootstrapModule[], selection: BootstrapSelection) {
  const byId = new Map(modules.map((m) => [m.id, m]))
  return occurrences(selection.modules).flatMap(([id, n]) => {
    const module = byId.get(id)
    return module ? [{ module, n }] : []
  })
}

/** Module names for a summary, a repeated one once with its count: "Mount NFS ×2". */
export function moduleSummary(ids: string[], modules: BootstrapModule[]) {
  const counts = new Map<string, number>()
  for (const id of ids) counts.set(id, (counts.get(id) ?? 0) + 1)
  return [...counts].map(([id, count]) => {
    const name = modules.find((m) => m.id === id)?.name ?? id
    return count > 1 ? `${name} ×${count}` : name
  })
}

/**
 * Secret parameters of the selected modules that are still empty. Secrets have
 * no default and are never stored, so the server refuses to run without them;
 * checking here lets the UI say so before anything is created. A repeat with
 * none of its own shares the first one's, as the runner does.
 */
export function missingSecrets(
  modules: BootstrapModule[], selection: BootstrapSelection,
): string[] {
  return [...new Set(selectedOccurrences(modules, selection).flatMap(({ module, n }) =>
    module.params
      .filter((param) => param.secret && !selection.params[occurrenceKey(param.name, n)]
        && !selection.params[param.name])
      .map((param) => param.name)))]
}

/** Selected modules that install SSH keys, and so cannot run without one. */
export function keyModules(
  modules: BootstrapModule[], selection: BootstrapSelection,
): BootstrapModule[] {
  return modules.filter((m) => m.uses_ssh_keys && selection.modules.includes(m.id))
}

/** The secret params of the selected modules, which an API example must not echo. */
export function secretParamNames(
  modules: BootstrapModule[], selection: BootstrapSelection,
): string[] {
  return [...new Set(selectedOccurrences(modules, selection).flatMap(({ module, n }) =>
    module.params.filter((p) => p.secret).map((p) => occurrenceKey(p.name, n))))]
}

/**
 * A selection as it can be saved: parameters of the selected modules only, and
 * never a secret. The form keeps values for modules that were ticked and then
 * unticked, which the server would reject as undeclared.
 */
export function savableSelection(
  modules: BootstrapModule[], selection: BootstrapSelection,
): BootstrapSelection {
  const allowed = new Set(selectedOccurrences(modules, selection).flatMap(({ module, n }) =>
    module.params.filter((p) => !p.secret).map((p) => occurrenceKey(p.name, n))))
  return {
    ...selection,
    params: Object.fromEntries(
      Object.entries(selection.params).filter(([name]) => allowed.has(name))),
  }
}
