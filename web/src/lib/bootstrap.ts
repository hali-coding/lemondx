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
