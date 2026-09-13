import { keyModules, missingSecrets } from './bootstrap'
import type { BootstrapModule, InstanceSpec, InstanceTemplate } from './types'

// What a blank CPU or memory field means in the UI. Only the forms apply
// these: the API and `lemondx create` still treat an omitted limit as none, so
// scripts keep the daemon's behaviour. Shown as the placeholders, so the value
// a blank field sends is the value it appears to hold.
export const DEFAULT_CPU = '4'
export const DEFAULT_MEMORY = '2GiB'

export function blankSpec(): InstanceSpec {
  return {
    image: 'ubuntu:24.04',
    type: 'container',
    cpu: '',
    memory: '',
    disk: '',
    pool: '',
    profiles: [],
    ephemeral: false,
    start: true,
    bootstrap: { modules: [], params: {}, ssh_keys: [] },
  }
}

/** A template's instance fields, copied so editing them cannot touch it. */
export function specOf(template: InstanceTemplate): InstanceSpec {
  return {
    image: template.image,
    type: template.type,
    cpu: template.cpu,
    memory: template.memory,
    disk: template.disk,
    pool: template.pool,
    profiles: [...template.profiles],
    ephemeral: template.ephemeral,
    start: template.start,
    bootstrap: {
      modules: [...template.bootstrap.modules],
      params: { ...template.bootstrap.params },
      ssh_keys: [...template.bootstrap.ssh_keys],
    },
  }
}

/**
 * The spec as the form shows it: blank limits filled the way their
 * placeholders promise, and started whenever modules need it to be.
 */
export function resolvedSpec(spec: InstanceSpec): InstanceSpec {
  return {
    ...spec,
    image: spec.image.trim(),
    cpu: spec.cpu.trim() || DEFAULT_CPU,
    memory: spec.memory.trim() || DEFAULT_MEMORY,
    disk: spec.disk.trim(),
    start: spec.start || spec.bootstrap.modules.length > 0,
  }
}

/** Why a spec cannot be used yet, for the parent's submit button. */
export function specProblems(
  spec: InstanceSpec, modules: BootstrapModule[], secretsAtLaunch: boolean,
) {
  const keysMissing = keyModules(modules, spec.bootstrap).length > 0
    && spec.bootstrap.ssh_keys.length === 0
  const secretsMissing = secretsAtLaunch ? [] : missingSecrets(modules, spec.bootstrap)
  return {
    keysMissing,
    secretsMissing,
    blocked: !spec.image.trim() || keysMissing || secretsMissing.length > 0,
  }
}
