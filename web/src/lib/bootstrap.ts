import type { BootstrapModule, BootstrapSelection } from './types'

/**
 * Secret parameters of the selected modules that are still empty. Secrets have
 * no default and are never stored, so the server refuses to run without them;
 * checking here lets the UI say so before anything is created.
 */
export function missingSecrets(
  modules: BootstrapModule[], selection: BootstrapSelection,
): string[] {
  return modules
    .filter((module) => selection.modules.includes(module.id))
    .flatMap((module) => module.params)
    .filter((param) => param.secret && !selection.params[param.name])
    .map((param) => param.name)
}

/** Selected modules that install SSH keys, and so cannot run without one. */
export function keyModules(
  modules: BootstrapModule[], selection: BootstrapSelection,
): BootstrapModule[] {
  return modules.filter((m) => m.uses_ssh_keys && selection.modules.includes(m.id))
}

/**
 * A selection as it can be saved: parameters of the selected modules only, and
 * never a secret. The form keeps values for modules that were ticked and then
 * unticked, which the server would reject as undeclared.
 */
export function savableSelection(
  modules: BootstrapModule[], selection: BootstrapSelection,
): BootstrapSelection {
  const allowed = new Set(modules
    .filter((m) => selection.modules.includes(m.id))
    .flatMap((m) => m.params)
    .filter((p) => !p.secret)
    .map((p) => p.name))
  return {
    ...selection,
    params: Object.fromEntries(
      Object.entries(selection.params).filter(([name]) => allowed.has(name))),
  }
}
