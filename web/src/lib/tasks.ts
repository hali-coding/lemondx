/**
 * How a task hands values to its job: the one place the UI's help says it,
 * mirroring `env_name()`, `secret_env_name()` and `_job()` in tasks.py.
 */

export const inputEnv = (name: string) => `LEMONDX_INPUT_${name.toUpperCase()}`
export const secretEnv = (name: string) => `LEMONDX_SECRET_${name.toUpperCase()}`

/** What every run's job gets, whatever the task declares. */
export const TASK_ENV: [string, string][] = [
  ['LEMONDX_TASK', 'the task’s name'],
  ['LEMONDX_TASK_RUN', 'this run’s id'],
  ['LEMONDX_TASK_TRIGGER', '“schedule” or “invoke”'],
  ['LEMONDX_TASK_SLOT', 'for a scheduled run, the occurrence it is for (epoch seconds)'],
  ['LEMONDX_TASK_NODE', 'the member running it'],
  ['LEMONDX_TASK_PAYLOAD', 'the path of the payload file, when the run was given one'],
]

/** The module parameters whose value names `{{kind.name}}`, e.g. every one fed by an input. */
export function paramsReferring(params: Record<string, string>, kind: 'inputs' | 'secrets',
                                name: string) {
  const pattern = new RegExp(`\\{\\{\\s*${kind}\\.${name}\\s*\\}\\}`)
  return Object.entries(params).filter(([, value]) => pattern.test(value)).map(([key]) => key)
}
