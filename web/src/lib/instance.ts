import { keyModules, missingSecrets } from './bootstrap'
import type {
  BootstrapModule, InstanceSpec, InstanceTemplate,
} from './types'

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
    network: '',
    fabric: '',
    profiles: [],
    ephemeral: false,
    start: true,
    secureboot: true,
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
    network: template.network,
    fabric: template.fabric || '',
    profiles: [...template.profiles],
    ephemeral: template.ephemeral,
    start: template.start,
    secureboot: template.secureboot,
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
    // Unticking VM hides the checkbox, so it must not keep applying.
    secureboot: spec.secureboot || spec.type !== 'virtual-machine',
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

/**
 * A row's identity across a cluster. Two nodes can each hold a `web-1` --
 * nothing stops them, unless a cluster-wide launch named them -- so anything
 * that remembers an instance has to remember its node too. Without a node (a
 * view scoped to this host) the name alone is already unique.
 */
export function keyOf(container: { name: string; node?: string }) {
  return container.node ? `${container.node}/${container.name}` : container.name
}
