import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { DragEvent, ReactNode } from 'react'
import { useAuthInfo, useCanWrite } from '../hooks/useAuth'
import { api, ApiError } from '../lib/api'
import { occurrenceKey, occurrences, paramName } from '../lib/bootstrap'
import { madeFrom } from '../lib/instance'
import type {
  BootstrapModule, ClusterNode, InstanceTemplate, NodeGroup, Stack, StackLaunchStep,
  SavedStack, StackScriptStep, StackStep, StackStepType,
} from '../lib/types'
import { CopyButton } from './CopyButton'
import {
  ChevronDownIcon, ChevronUpIcon, ClockIcon, CloseIcon, GaugeIcon, TerminalIcon, GripIcon, HeartIcon,
  ListOrderIcon, PlusIcon, ServerIcon, TrashIcon,
} from './Icons'

interface Props {
  stack?: Stack
  templates: InstanceTemplate[]
  modules: BootstrapModule[]
  /** Every node this one knows, itself included. One means no picker. */
  nodes: ClusterNode[]
  groups: NodeGroup[]
  /** How many instances carry this stack's name as their tag, for a rename. */
  running?: number
  onCancel: () => void
  /** `closed`: the designer is done; otherwise it was saved from the side pane and stays open. */
  onSaved: (stack: SavedStack, closed: boolean) => void
}

/** A step as the editor holds it: `key` is React's and the drag's, never saved. */
type Item = StackStep & { key: string }
type Launch = Item & StackLaunchStep
type Stage = { key: string; steps: Item[] }

/** What is being dragged: a new step from the palette, or one already placed. */
type Drag = { kind: 'new'; step: StackStep } | { kind: 'move'; key: string }
/** Where it would land: a new stage before `index`, or beside the steps of one. */
type Target = { kind: 'gap'; index: number } | { kind: 'stage'; index: number }

const FIELDS = ['names', 'name', 'ips', 'ip', 'ipv6', 'nodes', 'count'] as const
const PLACEHOLDER = /\{\{\s*([^{}]*?)\s*\}\}/g
const STEP_ID = /^[a-z][a-z0-9_]{0,31}$/
const INPUT_REF = /^params\.[A-Za-z_][A-Za-z0-9_]{0,63}$/
// The stack's own values; the step ids `vars` and `secrets` are reserved for them.
const VAR_REF = /^vars\.([A-Za-z_][A-Za-z0-9_]{0,63})$/
const SECRET_REF = /^secrets\.([A-Za-z_][A-Za-z0-9_]{0,63})$/
const VALUE_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
const RESERVED_IDS = ['params', 'vars', 'secrets']
/** A script step's parameter name: it becomes an environment variable. */
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
const MAX_COUNT = 20
/** A swarm's launch is load generators, which come in larger numbers. */
const SWARM_MAX_COUNT = 200
/** A reference being dragged; text/plain rides along for any other text box. */
const REF_MIME = 'application/x-lemondx-ref'
/** How long a dragged reference hovers over a later step before it opens. */
const SPRING_MS = 450

/** What each field of an earlier launch becomes, for the autocomplete list. */
const FIELD_MEANING: Record<(typeof FIELDS)[number], string> = {
  ip: 'first instance’s IPv4 address',
  ips: 'every instance’s IPv4, comma-separated',
  name: 'first instance’s name',
  names: 'every instance’s name, comma-separated',
  ipv6: 'first instance’s global IPv6 address',
  nodes: 'the nodes the instances are on',
  count: 'how many instances it made',
}

/**
 * One thing a value may refer to: what an earlier launch made (`field`), a
 * parameter it ran with (`param`), a value entered at launch (`input`), or one
 * of the stack's own variables (`var`) and secrets (`secret`).
 */
type Suggestion = {
  ref: string
  label: string
  meaning: string
  kind: 'field' | 'param' | 'input' | 'var' | 'secret'
  /** The step it comes from, and that step's stage (0-based); not for inputs. */
  source?: string
  stage?: number
  /**
   * For a parameter: the module that declares it, as the inspector heads it
   * ("NFS file system #2"), or "Set on this step". Shown, never part of the
   * reference -- a template's parameters are one flat set of names.
   */
  group?: string
}

/** Suggestions by `group`, in the order the groups first appear. */
function byGroup(suggestions: Suggestion[]) {
  const groups = new Map<string, Suggestion[]>()
  for (const s of suggestions) {
    const name = s.group ?? ''
    groups.set(name, [...(groups.get(name) ?? []), s])
  }
  return [...groups]
}

/**
 * Everything a launch step hands on to later stages.
 *
 * The one place the designer decides what `{{step.…}}` may name, so the
 * autocomplete, the drag chips and the checks cannot disagree. It mirrors
 * `FIELDS` and `handed_names()` in stacks.py, which is what really refuses a
 * bad reference: every parameter the template's modules declare, and every one
 * the step sets, less the secrets -- one of those handed on would reach a
 * module that never declared it secret, and nothing would redact it.
 */
function stepOutputs(step: StackLaunchStep, stage: number, templates: InstanceTemplate[],
                     modules: BootstrapModule[]): Suggestion[] {
  const found: Suggestion[] = FIELDS.map((field) => ({
    ref: `${step.id}.${field}`, label: field, meaning: FIELD_MEANING[field], kind: 'field',
    source: step.id, stage,
  }))
  const template = templates.find((t) => t.name === step.template)
  const used = occurrences(template?.bootstrap.modules ?? []).flatMap(([id, n]) => {
    const module = modules.find((m) => m.id === id)
    return module ? [{ module, n }] : []
  })
  const secrets = new Set(used.flatMap(({ module }) =>
    module.params.filter((p) => p.secret).map((p) => p.name)))
  const repeated = new Set(used.filter(({ n }) => n > 1).map(({ module }) => module.id))
  const seen = new Set<string>()
  // Labelled by the bare name: the module it sits under says which repeat.
  const param = (key: string, group: string, fallback?: string) => {
    if (seen.has(key) || secrets.has(paramName(key))) return
    seen.add(key)
    const value = step.params[key] ?? template?.bootstrap.params[key] ?? fallback ?? ''
    const shown = value.length > 32 ? `${value.slice(0, 31)}…` : value
    found.push({ ref: `${step.id}.param.${key}`, label: paramName(key), kind: 'param', group,
      meaning: shown ? `${group} · now “${shown}”` : group, source: step.id, stage })
  }
  for (const { module, n } of used) {
    const group = `${module.name}${repeated.has(module.id) ? ` #${n}` : ''}`
    for (const p of module.params) param(occurrenceKey(p.name, n), group, p.value ?? p.default)
  }
  for (const key of Object.keys(step.params).sort()) param(key, 'Set on this step')
  return found
}

/**
 * Everything `{{…}}` may name at this point in the stack: what each earlier
 * launch hands on, then the values entered at launch. The order is the order
 * they are offered in, so the most specific -- this stack's own earlier
 * steps -- comes first.
 */
function suggestionsFor(earlier: { step: Launch; stage: number }[], inputs: string[],
                        templates: InstanceTemplate[], modules: BootstrapModule[],
                        own: StackValues) {
  const found = earlier.flatMap(({ step, stage }) => stepOutputs(step, stage, templates, modules))
  found.push(...ownSuggestions(own))
  for (const name of inputs) {
    found.push({ ref: `params.${name}`, label: `params.${name}`,
      meaning: 'entered when the stack is launched', kind: 'input' })
  }
  return found
}

/** The stack's variables and secrets as references, under their own headings. */
function ownSuggestions(own: StackValues): Suggestion[] {
  const shown = (value: string) => (value.length > 32 ? `${value.slice(0, 31)}…` : value)
  return [
    ...own.variables.filter((v) => VALUE_NAME.test(v.name)).map((v): Suggestion => ({
      ref: `vars.${v.name}`, label: v.name, kind: 'var', group: 'Stack variables',
      source: 'vars',
      meaning: `stack variable${v.description ? ` · ${v.description}` : ''} · “${
        shown(v.value)}”` })),
    ...own.secrets.filter((v) => VALUE_NAME.test(v.name)).map((v): Suggestion => ({
      ref: `secrets.${v.name}`, label: v.name, kind: 'secret', group: 'Stack secrets',
      source: 'secrets',
      meaning: `stack secret${v.description ? ` · ${v.description}` : ''}${
        v.generate ? ' · generated' : ''}` })),
  ]
}

/** One row of the stack's variables or secrets, as the editor holds it. */
type VariableRow = { key: string; name: string; value: string; description: string }
type SecretRow = { key: string; name: string; description: string; generate: boolean }
type StackValues = { variables: VariableRow[]; secrets: SecretRow[] }
/** The stack's own values, for every reference list below the editor. */
const StackValuesContext = createContext<StackValues>({ variables: [], secrets: [] })

/**
 * The flow the pane is editing a step of. In a swarm, `base` is every launch
 * of the stack -- all earlier to it -- and a launch may make up to 200.
 */
type Flow = { base: { step: Launch; stage: number }[]; swarm: string | null }
const FlowContext = createContext<Flow>({ base: [], swarm: null })

/** Every launch before `stageIndex`, with the stage it is in. */
function launchesBefore(stages: Stage[], stageIndex: number) {
  return stages.slice(0, stageIndex).flatMap((stage, index) => stage.steps
    .filter((s): s is Launch => s.type === 'launch').map((step) => ({ step, stage: index })))
}

/** The references in a value, in order. */
const refsIn = (value: string) => [...value.matchAll(PLACEHOLDER)].map((m) => m[1])

let measurer: CanvasRenderingContext2D | null = null

/**
 * The character a drop at `clientX` lands before.
 *
 * An input has no API for "which character is under this point" (the
 * caretPositionFromPoint family does not reach inside one), so the text is
 * measured in the input's own font instead -- a drop that always appended
 * would make the drag pointless for anything but an empty box.
 */
function caretAtPoint(input: HTMLInputElement, clientX: number) {
  measurer ??= document.createElement('canvas').getContext('2d')
  if (!measurer) return input.value.length
  const style = getComputedStyle(input)
  // `font` itself is empty in some browsers' computed style; build it.
  measurer.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
  const x = clientX - input.getBoundingClientRect().left - parseFloat(style.paddingLeft)
    - parseFloat(style.borderLeftWidth) + input.scrollLeft
  const text = input.value
  let previous = 0
  for (let i = 1; i <= text.length; i += 1) {
    const width = measurer.measureText(text.slice(0, i)).width
    if (width >= x) return x - previous < width - x ? i - 1 : i
    previous = width
  }
  return text.length
}

/** `at`, moved past the end of any `{{…}}` it falls inside, so a drop never splits one. */
function outsidePlaceholder(value: string, at: number) {
  for (const match of value.matchAll(PLACEHOLDER)) {
    const start = match.index ?? 0
    const end = start + match[0].length
    if (at > start && at < end) return end
  }
  return at
}

/**
 * What the editor shares with every reference chip and box below it: which
 * reference is being dragged, and which is being pointed at. A context rather
 * than props, since the boxes sit four components down in the inspector.
 */
type Wiring = {
  dragging: string | null
  startDrag: (event: DragEvent, ref: string, stage: number, fromCanvas: boolean) => void
  endDrag: () => void
  pointed: Pointed | null
  point: (what: Pointed | null) => void
}
/** A source step and some of what it hands on, lit up at both ends while hovered. */
type Pointed = { source: string; refs: string[] }
const WiringContext = createContext<Wiring | null>(null)
const useWiring = () => useContext(WiringContext)

/** A draggable reference: a chip on a step card, or in the inspector's list. */
function RefChip({ suggestion, stage, fromCanvas, onClick, clickable = true }: {
  suggestion: Suggestion
  stage: number
  fromCanvas?: boolean
  onClick?: () => void
  clickable?: boolean
}) {
  const wiring = useWiring()
  const lit = wiring?.pointed?.refs.includes(suggestion.ref)
  return (
    <button type="button" draggable
      className={`stack-ref is-${suggestion.kind}${lit ? ' is-linked' : ''}`}
      title={`{{${suggestion.ref}}} — ${suggestion.meaning}.\n${
        onClick ? 'Drag onto a parameter, or click to insert it into the box last used.'
          : 'Drag onto a parameter of a later step.'}`}
      aria-disabled={onClick && !clickable ? true : undefined}
      onDragStart={(event) => {
        event.stopPropagation()
        wiring?.startDrag(event, suggestion.ref, stage, Boolean(fromCanvas))
      }}
      onDragEnd={(event) => { event.stopPropagation(); wiring?.endDrag() }}
      onMouseEnter={() => wiring?.point({ source: suggestion.source ?? 'params',
        refs: [suggestion.ref] })}
      onMouseLeave={() => wiring?.point(null)}
      onClick={(event) => {
        if (!onClick) return
        event.stopPropagation()
        if (clickable) onClick()
      }}>
      {suggestion.label}
    </button>
  )
}

/**
 * A value field that completes references and takes them dropped.
 *
 * The references are the one part of a stack you cannot get right from memory:
 * they name another step and a field of it, and a typo is only found on save.
 * So the list is offered at the caret the moment `{{` is typed, or once a bare
 * word looks like one (`db.`), and Tab completes even without the list open:
 * `db.i` then Tab is `{{db.ip}}`, as a shell would. Accepting writes the
 * braces. A chip dropped on the box lands where it was let go.
 */
function ReferenceInput({ value, onChange, suggestions, id, placeholder, invalid, secret,
                          onActivate }: {
  value: string
  onChange: (next: string) => void
  suggestions: Suggestion[]
  id?: string
  placeholder?: string
  invalid?: boolean
  secret?: boolean
  /** Focused: here is how to insert a reference at this box's caret. */
  onActivate?: (insert: (ref: string) => void) => void
}) {
  const field = useRef<HTMLInputElement | null>(null)
  const wiring = useWiring()
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const [dropping, setDropping] = useState(false)
  // Where the reference being completed starts, what has been typed since,
  // and whether it opened with `{{` or is a bare word.
  const [partial, setPartial] = useState<{ at: number; text: string; braced: boolean } | null>(
    null)
  // The insert handed to onActivate outlives this render; it must see the
  // value as it is when a chip is clicked, not as it was on focus.
  const latest = useRef(value)
  useEffect(() => { latest.current = value }, [value])

  // A secret may only be a value entered at launch or one of the stack's
  // secrets: anything else would be saved, which is what the server refuses.
  const allowed = secret
    ? suggestions.filter((s) => s.kind === 'input' || s.kind === 'secret') : suggestions
  const canDrop = wiring?.dragging != null && allowed.some((s) => s.ref === wiring.dragging)

  /** The reference the caret is in the middle of typing, if any. */
  function openingAt(text: string, caret: number) {
    const before = text.slice(0, caret)
    const start = before.lastIndexOf('{{')
    if (start !== -1) {
      const typed = before.slice(start + 2)
      // Already closed, or running into another placeholder: not this one.
      if (!typed.includes('}}') && !typed.includes('{{')) {
        return { at: start, text: typed.trim(), braced: true }
      }
    }
    const word = /[A-Za-z_][A-Za-z0-9_.@]*$/.exec(before)
    // Not straight after a placeholder's `}}`, which is a literal suffix.
    if (word && !before.slice(0, word.index).endsWith('}}')) {
      return { at: word.index, text: word[0], braced: false }
    }
    return null
  }

  function matching(found: { text: string; braced: boolean } | null) {
    if (found === null) return []
    const needle = found.text.toLowerCase()
    // Inside `{{` anything containing the text is a fair guess; a bare word
    // has to be the start of a reference, or every ordinary value would match.
    return allowed.filter((s) => (found.braced
      ? !needle || s.ref.toLowerCase().includes(needle)
      : s.ref.toLowerCase().startsWith(needle)))
  }

  const matches = matching(partial)

  function sync(next: string, caret: number) {
    const found = openingAt(next, caret)
    setPartial(found)
    // A bare word only opens the list once it looks like a reference.
    setOpen(found !== null && (found.braced || found.text.includes('.'))
      && matching(found).length > 0)
    setHighlight(0)
  }

  /** Put `{{ref}}` in place of `from`..`to` and leave the caret after it. */
  function write(ref: string, from: number, to: number) {
    const input = field.current
    const text = latest.current
    const next = `${text.slice(0, from)}{{${ref}}}${text.slice(to)}`
    onChange(next)
    setOpen(false)
    setPartial(null)
    const end = from + ref.length + 4
    requestAnimationFrame(() => {
      input?.focus()
      input?.setSelectionRange(end, end)
    })
  }

  function accept(choice: Suggestion) {
    if (!partial) return
    const caret = field.current?.selectionStart ?? value.length
    // Completing inside a placeholder that is already closed: take its rest
    // too, or the old tail and braces would be left behind the new ones.
    const rest = partial.braced ? /^[^{}]*\}\}/.exec(value.slice(caret)) : null
    write(choice.ref, partial.at, caret + (rest ? rest[0].length : 0))
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Tab' && !event.shiftKey && !open && matches.length > 0) {
      // Tab with the list shut: one candidate is taken, several are shown.
      event.preventDefault()
      if (matches.length === 1) accept(matches[0])
      else {
        setOpen(true)
        setHighlight(0)
      }
      return
    }
    if (!open || matches.length === 0) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setHighlight((current) => (current + (event.key === 'ArrowDown' ? 1 : matches.length - 1))
        % matches.length)
    } else if (event.key === 'Enter' || (event.key === 'Tab' && !event.shiftKey)) {
      event.preventDefault()
      accept(matches[highlight])
    } else if (event.key === 'Escape') {
      event.preventDefault()
      setOpen(false)
    }
  }

  return (
    <div className={`stack-ref-field${dropping ? ' is-drop-target' : ''}${
      canDrop ? ' can-drop' : ''}`}>
      <input
        id={id}
        ref={field}
        className="input mono"
        value={value}
        placeholder={canDrop ? 'drop it here' : placeholder}
        aria-invalid={invalid || undefined}
        autoComplete="off"
        spellCheck={false}
        role="combobox"
        aria-expanded={open && matches.length > 0}
        aria-controls={open && matches.length > 0 ? `${id}-refs` : undefined}
        onChange={(event) => {
          onChange(event.target.value)
          sync(event.target.value, event.target.selectionStart ?? event.target.value.length)
        }}
        onKeyUp={(event) => {
          // Arrow keys and clicks move the caret without changing the text, so
          // the list has to follow the caret as well as the typing.
          if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            sync(value, event.currentTarget.selectionStart ?? 0)
          }
        }}
        onKeyDown={onKeyDown}
        onFocus={(event) => {
          sync(value, event.target.selectionStart ?? value.length)
          onActivate?.((ref) => {
            const at = outsidePlaceholder(latest.current,
              field.current?.selectionStart ?? latest.current.length)
            write(ref, at, at)
          })
        }}
        // A click on a suggestion blurs the input first; let it land.
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
        // Every drag over the box is answered, so one it cannot take -- a step
        // being moved carries its key as text/plain -- is refused rather than
        // typed in by the browser.
        onDragOver={(event) => {
          event.preventDefault()
          event.dataTransfer.dropEffect = canDrop ? 'copy' : 'none'
          if (canDrop !== dropping) setDropping(canDrop)
        }}
        onDragLeave={() => setDropping(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDropping(false)
          const ref = event.dataTransfer.getData(REF_MIME)
          if (!ref || !allowed.some((s) => s.ref === ref)) return
          const at = outsidePlaceholder(value, caretAtPoint(event.currentTarget, event.clientX))
          write(ref, at, at)
          wiring?.endDrag()
        }}
      />
      {secret && <span className="badge badge-warn stack-param-lock">secret</span>}
      {open && matches.length > 0 && (
        <ul className="stack-ref-menu" id={`${id}-refs`} role="listbox">
          {matches.slice(0, 8).map((choice, index) => (
            <li key={choice.ref}>
              <button type="button" role="option" aria-selected={index === highlight}
                className={index === highlight ? 'is-on' : undefined}
                onMouseEnter={() => setHighlight(index)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => accept(choice)}>
                <span className="mono">{choice.ref}</span>
                <span className="faint">{choice.meaning}</span>
              </button>
            </li>
          ))}
          <li className="stack-ref-menu-hint">Tab or Enter to insert · Esc to close</li>
        </ul>
      )}
    </div>
  )
}

/**
 * Where each reference in a value comes from, in words, under the box.
 * The value itself is `{{db.ip}}`; this says "db's first IPv4, stage 1".
 */
function ComesFrom({ value, suggestions }: { value: string; suggestions: Suggestion[] }) {
  const wiring = useWiring()
  const known = refsIn(value).flatMap((ref) => {
    const found = suggestions.find((s) => s.ref === ref)
    return found ? [found] : []
  })
  if (known.length === 0) return null
  return (
    <ul className="stack-comes-from">
      {known.map((s, index) => (
        <li key={`${s.ref}-${index}`}
          onMouseEnter={() => wiring?.point({ source: s.source ?? 'params', refs: [s.ref] })}
          onMouseLeave={() => wiring?.point(null)}>
          <span aria-hidden="true">←</span>
          <span className="mono">{s.ref}</span>
          <span className="faint">
            {s.meaning}{s.stage !== undefined ? ` · stage ${s.stage + 1}` : ''}
          </span>
        </li>
      ))}
    </ul>
  )
}

let nextKey = 0
const newKey = () => `k${(nextKey += 1)}`

function slugId(text: string) {
  const slug = text.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
  return (/^[a-z]/.test(slug) ? slug : `step_${slug}`).slice(0, 28).replace(/_+$/, '') || 'step'
}

function uniqueId(base: string, taken: Set<string>) {
  if (!taken.has(base)) return base
  for (let n = 2; ; n += 1) {
    if (!taken.has(`${base}_${n}`)) return `${base}_${n}`
  }
}

/**
 * A new step. A launch goes to `group` when one is given -- the first node
 * group, alphabetically, in a cluster -- so a stack spreads over the cluster
 * unless it is told otherwise; with none it stays on this node.
 */
function blank(type: StackStepType, template?: InstanceTemplate, group?: string): StackStep {
  if (type === 'sleep') return { id: 'sleep', type, seconds: 30 }
  if (type === 'wait_healthy') return { id: 'healthy', type, timeout_seconds: 900 }
  if (type === 'script') {
    return { id: 'script', type, script: 'exit 0', timeout_seconds: 300, params: {} }
  }
  return {
    id: template ? slugId(template.name_prefix || template.name) : 'launch',
    type: 'launch', template: template?.name ?? '', count: 1, prefix: '', min_percent: 100,
    wait_bootstrap: true, params: {}, nodes: [], groups: group ? [group] : [],
  }
}

/** Everything wrong with the stack as drawn, keyed by step (`''` for the whole). */
/** Saved stages as the editor holds them, each stage and step with its own key. */
function toStages(saved: { steps: StackStep[] }[]): Stage[] {
  return saved.map((stage) => ({
    key: newKey(), steps: stage.steps.map((step) => ({ ...step, key: newKey() })) }))
}

/** A swarm as the editor holds it: `key` is React's, never saved. */
type SwarmDraft = { key: string; name: string; description: string; stages: Stage[]
  sequence: boolean }

/** The canvas's flow key for the sequence view, beside null (the stack) and swarm keys. */
const SEQUENCE = 'sequence'

/**
 * Everything wrong with the stack as drawn, keyed by step (`''` for the whole,
 * `values` for its variables and secrets, `swarms` for the swarms' own names).
 * A swarm is checked as though it were the stack's next stage: every launch of
 * the stack is an earlier launch to it, and its ids may not reuse the stack's.
 */
function problems(stages: Stage[], templates: InstanceTemplate[], modules: BootstrapModule[],
                  own: StackValues, swarms: SwarmDraft[] = []) {
  const found: Record<string, string[]> = {}
  const add = (key: string, message: string) => { (found[key] ??= []).push(message) }
  if (stages.length === 0) add('', 'Drag a step onto the canvas to start.')
  // The variables and secrets panel's own: keyed `values`, shown there.
  for (const [label, rows] of [['variable', own.variables], ['secret', own.secrets]] as const) {
    const seen = new Set<string>()
    for (const row of rows) {
      if (!VALUE_NAME.test(row.name)) {
        add('values', `The ${label} “${row.name || '(no name)'}” needs a name of letters, `
          + 'digits and _, not starting with a digit.')
      } else if (seen.has(row.name)) {
        add('values', `Two ${label}s are called “${row.name}”.`)
      }
      seen.add(row.name)
    }
  }

  /** One flow's checks; returns what its launches hand on, and their ids. */
  function checkFlow(flow: Stage[], seed: { handed: Set<string>; earlier: Set<string>;
                                            taken: Set<string> }) {
    const ids = new Map<string, number>()
    flow.flatMap((s) => s.steps).forEach((step) => ids.set(step.id, (ids.get(step.id) ?? 0) + 1))
    const earlier = new Set(seed.earlier)
    // What the launches before the current stage hand on, by reference -- and
    // the stack's own values, which every stage may use.
    const handed = new Set(seed.handed)
    const checkRefs = (key: string, name: string, value: string) => {
      for (const ref of refsIn(value)) {
        if (INPUT_REF.test(ref) || handed.has(ref)) continue
        add(key, VAR_REF.test(ref) || SECRET_REF.test(ref)
          ? `${name}: {{${ref}}} names no ${VAR_REF.test(ref) ? 'variable' : 'secret'} `
            + 'of this stack.'
          : `${name}: {{${ref}}} is not something a launch in an earlier stage hands on.`)
      }
    }
    flow.forEach((stage, stageIndex) => {
      const launched = new Set<string>()
      const templatesHere = new Set<string>()
      for (const step of stage.steps) {
        if (!STEP_ID.test(step.id)) add(step.key, 'The id must be lowercase letters, digits and _.')
        if (RESERVED_IDS.includes(step.id)) {
          add(step.key, `“${step.id}” is reserved: {{${step.id}.…}} names the stack's own values.`)
        }
        if ((ids.get(step.id) ?? 0) > 1) add(step.key, `Another step is also called “${step.id}”.`)
        if (seed.taken.has(step.id)) {
          add(step.key, `The stack has a step “${step.id}” too, which references would mean `
            + 'instead; give this one another id.')
        }
        if (step.type === 'wait_healthy' && earlier.size === 0) {
          add(step.key, 'Nothing is launched before this, so there is nothing to wait for.')
        }
        if (step.type === 'script') {
          for (const [name, value] of [['script', step.script] as const,
            ...Object.entries(step.params)]) checkRefs(step.key, name, value)
          for (const name of Object.keys(step.params)) {
            if (!ENV_NAME.test(name)) {
              add(step.key, `${name || '(no name)'} cannot be an environment variable: letters, `
                + 'digits and _, not starting with a digit.')
            }
          }
          continue
        }
        if (step.type !== 'launch') continue
        if (!templates.some((t) => t.name === step.template)) {
          add(step.key, step.template ? `No template “${step.template}”.` : 'Pick a template.')
        }
        if (templatesHere.has(step.template)) {
          add(step.key, 'This template is launched twice in one stage; move one to its own stage.')
        }
        templatesHere.add(step.template)
        launched.add(step.id)
        for (const [name, value] of Object.entries(step.params)) checkRefs(step.key, name, value)
      }
      launched.forEach((id) => earlier.add(id))
      stage.steps.forEach((step) => {
        if (step.type !== 'launch') return
        stepOutputs(step, stageIndex, templates, modules).forEach((s) => handed.add(s.ref))
      })
    })
    return { handed, earlier, ids: new Set(ids.keys()) }
  }

  const stack = checkFlow(stages, {
    handed: new Set(ownSuggestions(own).map((s) => s.ref)), earlier: new Set(), taken: new Set() })
  const names = new Set<string>()
  for (const swarm of swarms) {
    if (!VALUE_NAME.test(swarm.name)) {
      add('swarms', `The swarm “${swarm.name || '(no name)'}” needs a name of letters, digits `
        + 'and _, not starting with a digit.')
    } else if (names.has(swarm.name)) {
      add('swarms', `Two swarms are called “${swarm.name}”.`)
    }
    names.add(swarm.name)
    if (swarm.stages.length === 0) add('swarms', `The swarm “${swarm.name}” has no steps yet.`)
    checkFlow(swarm.stages, { handed: stack.handed, earlier: stack.earlier, taken: stack.ids })
  }
  return found
}

/**
 * A stack's definition as one string, to tell whether the drawing differs from
 * what was saved. Keys sorted, since an edit can reorder a step's fields
 * without changing it; secret *values* are not in it -- they are tracked as
 * pending separately, and are never in a definition.
 */
function fingerprint(name: string, description: string, stages: StackStep[][],
                     variables: { name: string; value: string; description: string }[],
                     secrets: { name: string; description: string; generate: boolean }[],
                     swarms: { name: string; description: string; stages: StackStep[][];
                               sequence: boolean }[]) {
  const sorted = (_: string, value: unknown) => (value && typeof value === 'object'
    && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value as Record<string, unknown>).sort(
      ([a], [b]) => a.localeCompare(b)))
    : value)
  return JSON.stringify({
    name, description, stages,
    variables: variables.map((v) => [v.name, v.value, v.description]),
    secrets: secrets.map((v) => [v.name, v.description, v.generate]),
    swarms,
  }, sorted)
}

/** How near a pane's top or bottom edge a drag starts it scrolling, and how fast at most. */
const EDGE_PX = 56
const EDGE_SPEED = 18

/**
 * Scroll the designer's panes while something is dragged near their edges.
 *
 * Browsers scroll the page for a drag at its edge, but not reliably a pane
 * inside it -- and the parameters a chip is dragged to are often further down
 * the inspector than it shows. So for any drag inside `root`, the scrolling
 * pane under the pointer scrolls, faster the nearer the edge. A drag that
 * ends without a dragend reaching us (its source was unmounted) stops when
 * dragover does.
 */
function useDragEdgeScroll(root: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    const element = root.current
    if (!element) return
    let point: { x: number; y: number; at: number } | null = null
    let frame = 0

    const pane = (x: number, y: number) => {
      let node = document.elementFromPoint(x, y) as HTMLElement | null
      while (node && node !== element.parentElement) {
        const style = getComputedStyle(node)
        if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight) {
          return node
        }
        node = node.parentElement
      }
      return null
    }

    const step = () => {
      frame = 0
      if (!point || performance.now() - point.at > 300) {
        point = null
        return
      }
      const target = pane(point.x, point.y)
      if (target) {
        const box = target.getBoundingClientRect()
        const fromTop = point.y - box.top
        const fromBottom = box.bottom - point.y
        if (fromTop < EDGE_PX) target.scrollTop -= EDGE_SPEED * (1 - fromTop / EDGE_PX)
        else if (fromBottom < EDGE_PX) {
          target.scrollTop += EDGE_SPEED * (1 - fromBottom / EDGE_PX)
        }
      }
      frame = requestAnimationFrame(step)
    }

    const over = (event: globalThis.DragEvent) => {
      point = { x: event.clientX, y: event.clientY, at: performance.now() }
      if (!frame) frame = requestAnimationFrame(step)
    }
    const stop = () => {
      point = null
    }
    element.addEventListener('dragover', over)
    document.addEventListener('dragend', stop, true)
    document.addEventListener('drop', stop, true)
    return () => {
      element.removeEventListener('dragover', over)
      document.removeEventListener('dragend', stop, true)
      document.removeEventListener('drop', stop, true)
      if (frame) cancelAnimationFrame(frame)
    }
  }, [root])
}

/** Designs a stack: stages run top to bottom, steps side by side run in parallel. */
export function StackEditor({ stack: opened, templates, modules, nodes, groups, running = 0,
                              onCancel, onSaved }: Props) {
  // The stack as last saved: the one opened, until a save from the side pane
  // keeps the designer open -- after which a rename, the secrets' state and
  // "unsaved changes" are all judged against what was just saved.
  const [stack, setStack] = useState<Stack | undefined>(opened)
  const [name, setName] = useState(stack?.name ?? '')
  const [description, setDescription] = useState(stack?.description ?? '')
  const [stages, setStages] = useState<Stage[]>(() => toStages(stack?.stages ?? []))
  const [swarms, setSwarms] = useState<SwarmDraft[]>(() => (stack?.swarms ?? []).map((w) => ({
    key: newKey(), name: w.name, description: w.description, stages: toStages(w.stages),
    sequence: w.sequence ?? true })))
  // Which flow the canvas shows: the stack's own (null), one swarm's, or the
  // sequence (`SEQUENCE`), which has no steps of its own -- everything that
  // edits a step acts on the stack's then.
  // Everything that edits a step -- dropping, moving, the pane -- acts on it.
  const [flow, setFlow] = useState<string | null>(null)
  const onSequence = flow === SEQUENCE
  const swarm = swarms.find((w) => w.key === flow) ?? null
  const shown = swarm ? swarm.stages : stages
  const setShown = (change: (current: Stage[]) => Stage[]) => {
    if (!swarm) {
      setStages(change)
      return
    }
    const key = swarm.key
    setSwarms((current) => current.map((w) => (w.key === key
      ? { ...w, stages: change(w.stages) } : w)))
  }
  // A swarm runs against the whole stack: all its launches came before.
  const before = useMemo(() => (swarm ? launchesBefore(stages, stages.length) : []),
    [swarm, stages])
  const flowValue = useMemo<Flow>(() => ({ base: before, swarm: swarm?.name ?? null }),
    [before, swarm])
  const [selected, setSelected] = useState<string | null>(null)
  // Where a new launch step goes: the first node group by name, when this is
  // a cluster that has any -- the "Where" picker only exists with more than
  // one node -- and this node otherwise.
  const defaultGroup = nodes.length > 1
    ? [...groups].map((g) => g.name).sort((a, b) => a.localeCompare(b))[0] : undefined
  // The variables-and-secrets panel, asked for with the header button. With
  // neither it nor a step chosen, and nothing unsaved, there is nothing for
  // the right-hand pane to say -- an empty pane reading "All changes saved"
  // beside a Save button only raised the question of what it would save --
  // so it goes, and the canvas has the room.
  const [valuesOpen, setValuesOpen] = useState(false)
  /** Choose a step for the pane; the variables panel gives way to it. */
  const select = (key: string) => {
    setSelected(key)
    setValuesOpen(false)
  }
  /** Close the pane, or what is in it. It stays only while there is something unsaved. */
  const closePane = () => {
    setSelected(null)
    setValuesOpen(false)
  }
  const [target, setTarget] = useState<Target | null>(null)
  const [saving, setSaving] = useState(false)
  const renaming = Boolean(stack && running > 0 && name.trim() && name.trim() !== stack.name)
  const [error, setError] = useState<string | null>(null)
  // dataTransfer cannot be read during dragover, which is when the drop
  // target needs to know what is coming; so the drag lives here instead.
  const drag = useRef<Drag | null>(null)
  // The same for a reference being dragged, but as state: every card and box
  // re-draws to say whether it would take it.
  const [refDrag, setRefDrag] = useState<{ ref: string; stage: number; fromCanvas: boolean }
    | null>(null)
  const [pointed, setPointed] = useState<Pointed | null>(null)
  const spring = useRef<{ key: string; timer: number } | null>(null)
  const inspector = useRef<HTMLElement | null>(null)
  const body = useRef<HTMLDivElement | null>(null)
  useDragEdgeScroll(body)
  // The stack's own values, as editable rows: a name can be typed over, so
  // the row's key, not its name, is what identifies it while editing.
  const [variables, setVariables] = useState<VariableRow[]>(() => Object.entries(
    stack?.variables ?? {}).map(([n, v]) => ({ key: newKey(), name: n, ...v })))
  const [secretRows, setSecretRows] = useState<SecretRow[]>(() => Object.entries(
    stack?.secrets ?? {}).map(([n, v]) => ({ key: newKey(), name: n, description: v.description,
    generate: v.generate })))
  // Values typed for secrets (or null: clear it), by row, sent once the stack
  // is saved -- a value is kept under the stack's name, so it needs one first.
  const [pendingSecrets, setPendingSecrets] = useState<Record<string, string | null>>({})
  const own = useMemo<StackValues>(() => ({ variables, secrets: secretRows }),
    [variables, secretRows])

  const issues = useMemo(() => problems(stages, templates, modules, own, swarms),
    [stages, templates, modules, own, swarms])
  // Per stage, every reference its steps may use: what a badge says each
  // one is, and what lights it red when it is none of them.
  const handedTo = useMemo(() => shown.map((_, index) => new Map(
    [...before, ...launchesBefore(shown, index)].flatMap(({ step, stage }) =>
      stepOutputs(step, stage, templates, modules)).concat(ownSuggestions(own))
      .map((o) => [o.ref, o] as const))),
  [shown, before, templates, modules, own])
  const flat = shown.flatMap((s) => s.steps)
  const current = flat.find((s) => s.key === selected) ?? null
  const stageOf = (key: string) => shown.findIndex((s) => s.steps.some((step) => step.key === key))
  const canSave = !!name.trim() && Object.keys(issues).length === 0 && !saving
  // Edits apply to the drawing as they are made, but nothing reaches the
  // server until a save -- which the side pane did not make obvious, so it
  // says which state it is in. Compared as the definition a save would send.
  const unkeyed = (flowStages: Stage[]) => flowStages.map((stage) => stage.steps.map(
    ({ key: _key, ...step }) => step as StackStep))
  const drawn = fingerprint(name.trim(), description, unkeyed(stages), variables, secretRows,
    swarms.map((w) => ({ name: w.name, description: w.description, stages: unkeyed(w.stages),
      sequence: w.sequence })))
  const [savedPrint, setSavedPrint] = useState(() => fingerprint(stack?.name ?? '',
    stack?.description ?? '', (stack?.stages ?? []).map((stage) => stage.steps),
    Object.entries(stack?.variables ?? {}).map(([n, v]) => ({ name: n, ...v })),
    Object.entries(stack?.secrets ?? {}).map(([n, v]) => ({ name: n, description: v.description,
      generate: v.generate })),
    (stack?.swarms ?? []).map((w) => ({ name: w.name, description: w.description,
      stages: w.stages.map((stage) => stage.steps), sequence: w.sequence ?? true }))))
  const dirty = drawn !== savedPrint || Object.keys(pendingSecrets).length > 0
  // Unsaved work keeps the pane open, since its foot is where it is saved.
  const paneOpen = current !== null || valuesOpen || dirty
  // Why a save cannot happen now, in words, for the pane's foot -- the first
  // problem itself, since the red card it belongs to may be scrolled away.
  const unsavable = !name.trim() ? 'Name the stack (top left) to save it.'
    : Object.keys(issues).length ? `Can't save yet: ${Object.values(issues)[0][0]}` : null
  // Shown once a save from the pane was asked for and refused; it clears as
  // soon as the reason does.
  const [blockedAsked, setBlocked] = useState<string | null>(null)
  const blocked = blockedAsked && unsavable ? unsavable : null
  const nameField = useRef<HTMLInputElement | null>(null)

  function cancelSpring() {
    if (spring.current) window.clearTimeout(spring.current.timer)
    spring.current = null
  }

  /** A dragged reference held over a later step opens it, so the drop has a box to land in. */
  function springTo(key: string) {
    if (spring.current?.key === key) return
    cancelSpring()
    spring.current = { key, timer: window.setTimeout(() => {
      spring.current = null
      select(key)
    }, SPRING_MS) }
  }

  const endRefDrag = () => {
    cancelSpring()
    setRefDrag(null)
  }

  // The chip a drag started from can be unmounted under it -- an inspector
  // chip, when the step it belongs to is swapped out -- and then its dragend
  // never comes. Any drop or end anywhere finishes the drag too.
  useEffect(() => {
    if (!refDrag) return
    const finish = () => endRefDrag()
    document.addEventListener('drop', finish, true)
    document.addEventListener('dragend', finish, true)
    return () => {
      document.removeEventListener('drop', finish, true)
      document.removeEventListener('dragend', finish, true)
    }
  }, [refDrag])

  const wiring: Wiring = {
    dragging: refDrag?.ref ?? null,
    startDrag: (event, ref, stage, fromCanvas) => {
      event.dataTransfer.effectAllowed = 'copy'
      event.dataTransfer.setData(REF_MIME, ref)
      event.dataTransfer.setData('text/plain', `{{${ref}}}`)
      // Changing the page inside dragstart can cancel the drag in Chromium;
      // the highlighting can wait a frame.
      window.setTimeout(() => setRefDrag({ ref, stage, fromCanvas }), 0)
    },
    endDrag: endRefDrag,
    pointed,
    point: setPointed,
  }

  /** What a step takes from earlier ones, by the step it comes from. */
  function usesOf(step: Item) {
    const bySource = new Map<string, string[]>()
    if (step.type !== 'launch' && step.type !== 'script') return bySource
    for (const value of [...Object.values(step.params),
      ...(step.type === 'script' ? [step.script] : [])]) {
      for (const ref of refsIn(value)) {
        const source = ref.split('.')[0]
        const list = bySource.get(source) ?? []
        if (!list.includes(ref)) list.push(ref)
        bySource.set(source, list)
      }
    }
    return bySource
  }

  function withId(step: StackStep): Item {
    // In a swarm, the stack's ids are taken too: a reference names a step by id.
    const taken = new Set([...flat, ...(swarm ? stages.flatMap((s) => s.steps) : [])]
      .map((s) => s.id))
    return { ...step, id: uniqueId(step.id, taken), key: newKey() }
  }

  /** Put a step at a target, taking it out of wherever it was first. */
  function place(item: Item, where: Target) {
    setShown((current) => {
      const from = current.findIndex((s) => s.steps.some((step) => step.key === item.key))
      // Dropped back onto the stage it is already in, alone: nothing moves.
      if (where.kind === 'stage' && from === where.index && current[from].steps.length === 1) {
        return current
      }
      let next = current.map((s) => ({ ...s, steps: s.steps.filter((step) => step.key !== item.key) }))
      let index = where.index
      // Emptying a stage above the target shifts the target up by one.
      if (from !== -1 && next[from].steps.length === 0 && from < index) index -= 1
      next = next.filter((s) => s.steps.length > 0)
      if (where.kind === 'stage' && next[index]) {
        next[index] = { ...next[index], steps: [...next[index].steps, item] }
      } else {
        next.splice(Math.min(index, next.length), 0, { key: newKey(), steps: [item] })
      }
      return next
    })
    select(item.key)
  }

  function add(step: StackStep) {
    // From the sequence view, a template goes to the stack, shown as it lands.
    if (onSequence) setFlow(null)
    place(withId(step), { kind: 'gap', index: shown.length })
  }

  function update(key: string, change: Partial<StackStep>) {
    setShown((current) => current.map((stage) => ({
      ...stage,
      steps: stage.steps.map((step) => (step.key === key ? { ...step, ...change } as Item : step)),
    })))
  }

  function remove(key: string) {
    setShown((current) => current
      .map((stage) => ({ ...stage, steps: stage.steps.filter((s) => s.key !== key) }))
      .filter((stage) => stage.steps.length > 0))
    setSelected((current) => (current === key ? null : current))
  }

  function onDragStart(event: DragEvent, what: Drag) {
    drag.current = what
    event.dataTransfer.effectAllowed = what.kind === 'new' ? 'copy' : 'move'
    // Firefox starts no drag without data.
    event.dataTransfer.setData('text/plain', what.kind === 'new' ? what.step.type : what.key)
  }

  function over(event: DragEvent, where: Target) {
    if (!drag.current) return
    event.preventDefault()
    event.stopPropagation()
    event.dataTransfer.dropEffect = drag.current.kind === 'new' ? 'copy' : 'move'
    setTarget((current) => (current?.kind === where.kind && current.index === where.index
      ? current : where))
  }

  function drop(event: DragEvent, where: Target) {
    event.preventDefault()
    event.stopPropagation()
    const what = drag.current
    drag.current = null
    setTarget(null)
    if (!what) return
    if (what.kind === 'new') {
      place(withId(what.step), where)
    } else {
      const item = flat.find((s) => s.key === what.key)
      if (item) place(item, where)
    }
  }

  const endDrag = () => {
    drag.current = null
    setTarget(null)
  }

  /** Save the stack; `close` ends the designer, otherwise only the side pane closes. */
  async function save(close = true) {
    if (!canSave) return
    const sent = drawn
    setSaving(true)
    setError(null)
    try {
      const saved = await api.saveStack(name.trim(), {
        description,
        stages: stages.map((stage) => ({
          steps: stage.steps.map(({ key: _key, ...step }) => step as StackStep),
        })),
        variables: Object.fromEntries(variables.map((v) => [v.name,
          { value: v.value, description: v.description }])),
        secrets: Object.fromEntries(secretRows.map((v) => [v.name,
          { description: v.description, generate: v.generate }])),
        swarms: swarms.map((w) => ({ name: w.name, description: w.description,
          stages: unkeyed(w.stages).map((steps) => ({ steps })), sequence: w.sequence })),
        ...(stack && stack.name !== name.trim() ? { previous: stack.name } : {}),
      })
      // Renamed: the old name would otherwise linger as a second stack. Its
      // secret values have already moved with the save (`previous`).
      if (stack && stack.name !== saved.name) await api.deleteStack(stack.name).catch(() => {})
      // Then the values typed in here. The stack is saved by now either way;
      // one that fails is said, and the stack can be opened to try again.
      const failed: string[] = []
      let secrets = saved.secrets
      for (const row of secretRows) {
        if (!(row.key in pendingSecrets)) continue
        const value = pendingSecrets[row.key]
        try {
          const status = value === null
            ? await api.clearStackSecret(saved.name, row.name)
            : await api.setStackSecret(saved.name, row.name, value)
          secrets = { ...secrets, [row.name]: { ...secrets[row.name], ...status } }
        } catch (cause) {
          failed.push(`${row.name}: ${(cause as Error).message}`)
        }
      }
      setPendingSecrets({})
      if (failed.length) {
        setError(`Saved, but not every secret: ${failed.join('; ')}`)
        return
      }
      if (close) {
        onSaved({ ...saved, secrets }, true)
        return
      }
      setStack({ ...saved, secrets })
      setSavedPrint(sent)
      setBlocked(null)
      closePane()
      onSaved({ ...saved, secrets }, false)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : (cause as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const gap = (index: number) => (
    <div
      className={`stack-gap${target?.kind === 'gap' && target.index === index ? ' is-target' : ''}`}
      onDragOver={(event) => over(event, { kind: 'gap', index })}
      onDragLeave={() => setTarget((t) => (t?.kind === 'gap' && t.index === index ? null : t))}
      onDrop={(event) => drop(event, { kind: 'gap', index })}
    >
      <span className="stack-gap-line" />
      <span className="stack-gap-label">Drop here to run after</span>
    </div>
  )

  return (
    <StackValuesContext.Provider value={own}>
    <FlowContext.Provider value={flowValue}>
    <WiringContext.Provider value={wiring}>
    <div className={`stack-editor${refDrag ? ' is-wiring' : ''}`} role="dialog" aria-modal="true"
      aria-label={stack ? `Edit stack ${stack.name}` : 'New stack'}>
      <header className="stack-editor-head">
        <div className="stack-editor-names">
          <input ref={nameField} className="input stack-editor-title" placeholder="Stack name"
            value={name}
            aria-label="Stack name" autoFocus={!stack}
            onChange={(event) => setName(event.target.value)} />
          <input className="input" placeholder="What it is for (optional)" value={description}
            aria-label="Description" maxLength={200}
            onChange={(event) => setDescription(event.target.value)} />
          {/* A rename saves under the new name and deletes the old, and the
              instances keep the tag they were launched with -- so they are
              left behind under a name nothing answers to any more. */}
          {renaming && (
            <div className="banner banner-warn"><div className="banner-body"><p>
              {running} instance(s) stay tagged “{stack?.name}” and leave this
              stack: start, stop and destroy will not reach them. Destroy them
              first if they should go with the name.
            </p></div></div>
          )}
        </div>
        <div className="stack-editor-actions">
          {error && <span className="field-error stack-editor-error" title={error}>{error}</span>}
          <button type="button" className={`btn${!current && paneOpen ? ' is-on' : ''}`}
            title="The stack's own variables and secrets, for any step to use"
            onClick={() => {
              if (!current && valuesOpen) {
                setValuesOpen(false)
                return
              }
              setSelected(null)
              setValuesOpen(true)
              requestAnimationFrame(() => inspector.current?.scrollTo({ top: 0 }))
            }}>
            Variables &amp; secrets
            {variables.length + secretRows.length > 0 && (
              <span className="badge badge-dim">{variables.length + secretRows.length}</span>
            )}
          </button>
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
          <button type="button" className="btn btn-primary" disabled={!canSave}
            onClick={() => save()}
            title={!name.trim() ? 'Give the stack a name'
              : Object.keys(issues).length ? 'Fix the steps marked in red first' : undefined}>
            {saving && <span className="spinner" />} Save stack
          </button>
        </div>
      </header>

      <div ref={body} className={`stack-editor-body${paneOpen ? '' : ' no-pane'}`}>
        <aside className="stack-palette" aria-label="Steps to add">
          <h4>Steps</h4>
          <p className="hint">Drag onto the canvas, or click to add at the end.</p>
          <PaletteItem icon={<ServerIcon />} label="Launch a template"
            detail="one or more instances"
            onDragStart={(e) => onDragStart(e, { kind: 'new',
              step: blank('launch', undefined, defaultGroup) })}
            onDragEnd={endDrag} onAdd={() => add(blank('launch', undefined, defaultGroup))} />
          <PaletteItem icon={<HeartIcon />} label="Wait until healthy"
            detail="everything launched so far"
            onDragStart={(e) => onDragStart(e, { kind: 'new', step: blank('wait_healthy') })}
            onDragEnd={endDrag} onAdd={() => add(blank('wait_healthy'))} />
          <PaletteItem icon={<ClockIcon />} label="Sleep" detail="a fixed pause"
            onDragStart={(e) => onDragStart(e, { kind: 'new', step: blank('sleep') })}
            onDragEnd={endDrag} onAdd={() => add(blank('sleep'))} />
          <PaletteItem icon={<TerminalIcon />} label="Run a script" detail="shell, on this host"
            onDragStart={(e) => onDragStart(e, { kind: 'new', step: blank('script') })}
            onDragEnd={endDrag} onAdd={() => add(blank('script'))} />

          <h4>Templates</h4>
          {templates.length === 0 && <p className="hint">No templates yet; make one first.</p>}
          {templates.map((template) => (
            <PaletteItem key={template.name} icon={<ServerIcon />} label={template.name}
              detail={madeFrom(template)} mono
              onDragStart={(e) => onDragStart(e, { kind: 'new',
                step: blank('launch', template, defaultGroup) })}
              onDragEnd={endDrag} onAdd={() => add(blank('launch', template, defaultGroup))} />
          ))}
        </aside>

        <main className="stack-canvas" onClick={closePane}
          onDragOver={onSequence ? undefined
            : (event) => over(event, { kind: 'gap', index: shown.length })}
          onDrop={onSequence ? undefined
            : (event) => drop(event, { kind: 'gap', index: shown.length })}>
          <FlowTabs swarms={swarms} flow={flow} onFlow={(key) => {
            closePane()
            setFlow(key)
          }} onAdd={() => {
            const key = newKey()
            const taken = new Set(swarms.map((w) => w.name))
            let n = swarms.length + 1
            while (taken.has(`swarm_${n}`)) n += 1
            setSwarms((current) => [...current, { key, name: `swarm_${n}`, description: '',
              stages: [], sequence: true }])
            closePane()
            setFlow(key)
          }} />
          {swarm && (
            <SwarmHeader swarm={swarm} issues={issues.swarms ?? []} templates={templates}
              place={swarms.filter((w) => w.sequence).findIndex((w) => w.key === swarm.key)}
              many={swarms.length > 1}
              onSequence={() => {
                closePane()
                setFlow(SEQUENCE)
              }}
              onChange={(change) => setSwarms((current) => current.map((w) => (
                w.key === swarm.key ? { ...w, ...change } : w)))}
              onRemove={() => {
                setSwarms((current) => current.filter((w) => w.key !== swarm.key))
                closePane()
                setFlow(null)
              }} />
          )}
          {onSequence ? (
            <SequenceView swarms={swarms}
              onToggle={(key, on) => setSwarms((current) => current.map((w) => (
                w.key === key ? { ...w, sequence: on } : w)))}
              onMove={(key, by) => setSwarms((current) => {
                const from = current.findIndex((w) => w.key === key)
                const to = from + by
                if (to < 0 || to >= current.length) return current
                const next = [...current]
                next.splice(to, 0, ...next.splice(from, 1))
                return next
              })}
              onOpen={(key) => {
                closePane()
                setFlow(key)
              }} />
          ) : (
          <div className="stack-flow">
            {swarm ? (
              <div className="stack-terminal stack-terminal-running"
                title="Every launch of the stack is an earlier launch to this swarm">
                Running stack: {before.map(({ step }) => step.id).join(', ') || 'nothing launched'}
              </div>
            ) : <div className="stack-terminal">Start</div>}
            {gap(0)}
            {shown.map((stage, index) => (
              <div key={stage.key} className="stack-flow-stage">
                <div
                  className={`stack-stage${target?.kind === 'stage' && target.index === index ? ' is-target' : ''}`}
                  onDragOver={(event) => over(event, { kind: 'stage', index })}
                  onDrop={(event) => drop(event, { kind: 'stage', index })}
                >
                  <div className="stack-stage-label">
                    Stage {index + 1}
                    {stage.steps.length > 1 && <span> · {stage.steps.length} in parallel</span>}
                  </div>
                  <div className="stack-stage-steps">
                    {stage.steps.map((step) => {
                      const uses = usesOf(step)
                      const linked = pointed !== null && (step.id === pointed.source
                        || [...uses.values()].flat().some((ref) => pointed.refs.includes(ref)))
                      return (
                        <StepCard key={step.key} step={step} selected={step.key === selected}
                          issues={issues[step.key] ?? []} templates={templates}
                          outputs={step.type === 'launch'
                            ? stepOutputs(step, index, templates, modules) : []}
                          stageIndex={index} uses={uses} linked={linked}
                          available={handedTo[index]}
                          receiving={refDrag === null ? null
                            : swarm && !flat.some((s) => s.id === refDrag.ref.split('.')[0])
                              ? true : index > refDrag.stage}
                          inSwarm={Boolean(swarm)}
                          onRefOver={() => {
                            // Only a drag from the canvas springs a step open: one
                            // from the inspector would swap out its own source.
                            if (refDrag?.fromCanvas && index > refDrag.stage) springTo(step.key)
                          }}
                          onRefLeave={cancelSpring}
                          onSelect={() => select(step.key)}
                          onRemove={() => remove(step.key)}
                          onDragStart={(e) => onDragStart(e, { kind: 'move', key: step.key })}
                          onDragEnd={endDrag} />
                      )
                    })}
                    <div className="stack-stage-drop" aria-hidden="true">
                      <PlusIcon size={13} /> in parallel
                    </div>
                  </div>
                </div>
                {gap(index + 1)}
              </div>
            ))}
            <div className="stack-terminal">{swarm ? 'Torn down' : 'Done'}</div>
            {shown.length === 0 && (
              <p className="stack-canvas-empty">
                {swarm
                  ? <>Drag a load generator's template here, then a script or sleep that is the
                    test. Its steps can use everything the stack's launches hand on; when the
                    last stage ends, fails or is stopped, the swarm's instances are destroyed.</>
                  : <>Drag a template from the left to launch it. Steps in the same stage run
                    side by side; each stage starts when the one above has finished.</>}
              </p>
            )}
          </div>
          )}
        </main>

        {paneOpen && (
        <aside className="stack-inspector" aria-label="Step settings" ref={inspector}>
          {current ? (
            <Inspector key={current.key} step={current} stageIndex={stageOf(current.key)}
              stages={shown} templates={templates} modules={modules}
              nodes={nodes} groups={groups} issues={issues[current.key] ?? []}
              onChange={(change) => update(current.key, change)}
              onMove={(delta) => {
                const index = stageOf(current.key)
                const alone = shown[index].steps.length === 1
                // Up joins the stage above; down joins the stage below, or
                // becomes a stage of its own at the end.
                if (delta < 0 && index > 0) place(current, { kind: 'stage', index: index - 1 })
                else if (delta > 0 && index < shown.length - 1) {
                  place(current, { kind: 'stage', index: index + 1 })
                } else if (delta > 0 && !alone) {
                  place(current, { kind: 'gap', index: shown.length })
                }
              }}
              onSplit={() => place(current, { kind: 'gap', index: stageOf(current.key) + 1 })}
              onRemove={() => remove(current.key)} />
          ) : (
            <div className="stack-inspector-empty">
              <StackValuesPanel stack={stack} savedName={stack?.name ?? null}
                renamed={Boolean(stack && name.trim() !== stack.name)}
                variables={variables} onVariables={setVariables}
                secrets={secretRows} onSecrets={setSecretRows}
                pending={pendingSecrets} onPending={setPendingSecrets}
                issues={issues.values ?? []} />
              <h4>How a stack runs</h4>
              <ol>
                <li>Stages run top to bottom; the steps inside one run at the same time.</li>
                <li>A launch waits for its bootstrap modules unless you turn that off, so the
                  next stage can rely on what it installed.</li>
                <li>A launch hands on what it made and what it ran with: the chips under
                  each launch card. Drag one onto a parameter of a later step (it becomes
                  <span className="mono"> {'{{db.ip}}'}</span>), or type it there and press
                  Tab. Modules also see <span className="mono">$LEMONDX_STACK_DB_IPS</span>.
                </li>
                <li>If a step fails, no further stage starts. Instances already launched stay.</li>
              </ol>
              {issues[''] && <p className="hint">{issues[''][0]}</p>}
            </div>
          )}
          <div className="stack-inspector-foot">
            <span className={blocked ? 'stack-blocked' : dirty ? 'stack-dirty' : 'faint'}
              title={dirty ? 'Edits show in the drawing at once, but are kept only when the '
                + 'stack is saved' : undefined}>
              {saving ? 'Saving…' : blocked ?? (dirty ? 'Unsaved changes' : 'All changes saved')}
            </span>
            {current && (
              <button type="button" className="btn btn-sm" onClick={closePane}
                title="Close this pane; the changes stay in the drawing, unsaved">
                Close
              </button>
            )}
            {!current && !dirty && (
              <button type="button" className="btn btn-sm" onClick={closePane}>Close</button>
            )}
            {/* Never greyed out: a button that is, with the reason only in a
                tooltip, read as broken. Pressed when it cannot save, it says
                why where you are looking, and takes you to the name if that
                is what is missing. Nothing to save, it only closes. */}
            <button type="button" className="btn btn-sm btn-primary" disabled={saving}
              onClick={() => {
                if (!dirty) {
                  closePane()
                  return
                }
                if (unsavable) {
                  setBlocked(unsavable)
                  if (!name.trim()) nameField.current?.focus()
                  return
                }
                save(false)
              }}>
              {saving && <span className="spinner" />}
              {current ? 'Save & close' : 'Save'}
            </button>
          </div>
        </aside>
        )}
      </div>
    </div>
    </WiringContext.Provider>
    </FlowContext.Provider>
    </StackValuesContext.Provider>
  )
}

/**
 * The stack's own values: variables, kept in the stack in the clear, and
 * secrets, of which the stack keeps only the name. A secret's value goes to
 * the server on save and is never sent back to the page, except to an admin
 * who asks for it (`SecretReveal`), confirming their password first.
 */
function StackValuesPanel({ stack, savedName, renamed, variables, onVariables, secrets,
                            onSecrets, pending, onPending, issues }: {
  stack?: Stack
  /** The stack as saved, whose values can be revealed; null for a new one. */
  savedName: string | null
  renamed: boolean
  variables: VariableRow[]
  onVariables: (rows: VariableRow[]) => void
  secrets: SecretRow[]
  onSecrets: (rows: SecretRow[]) => void
  pending: Record<string, string | null>
  onPending: (next: Record<string, string | null>) => void
  issues: string[]
}) {
  const canWrite = useCanWrite()
  const [typing, setTyping] = useState<{ key: string; value: string } | null>(null)
  const editVariable = (key: string, change: Partial<VariableRow>) =>
    onVariables(variables.map((v) => (v.key === key ? { ...v, ...change } : v)))
  const editSecret = (key: string, change: Partial<SecretRow>) =>
    onSecrets(secrets.map((v) => (v.key === key ? { ...v, ...change } : v)))
  const cleanName = (text: string) => text.replace(/[^A-Za-z0-9_]/g, '_').slice(0, 64)

  return (
    <div className="stack-values">
      <h4>Variables</h4>
      <p className="hint">
        Values every step can use as <span className="mono">{'{{vars.NAME}}'}</span> — an NFS
        server, a domain, a version. Kept in the stack and shown to anyone who can read it;
        each launch may override one.
      </p>
      {variables.map((row) => (
        <div className="stack-value-row" key={row.key}>
          <div className="stack-value-head">
            <input className="input mono" value={row.name} placeholder="NFS_SERVER"
              aria-label="Variable name" disabled={!canWrite}
              onChange={(event) => editVariable(row.key, { name: cleanName(event.target.value) })} />
            <button type="button" className="btn btn-ghost btn-icon btn-sm" disabled={!canWrite}
              aria-label={`Remove variable ${row.name}`}
              onClick={() => onVariables(variables.filter((v) => v.key !== row.key))}>
              <CloseIcon size={12} />
            </button>
          </div>
          <input className="input mono" value={row.value} placeholder="nas.lan"
            aria-label={`Value of ${row.name}`} disabled={!canWrite}
            onChange={(event) => editVariable(row.key, { value: event.target.value })} />
          <input className="input" value={row.description} placeholder="what it is (optional)"
            aria-label={`Description of ${row.name}`} maxLength={200} disabled={!canWrite}
            onChange={(event) => editVariable(row.key, { description: event.target.value })} />
        </div>
      ))}
      <button type="button" className="btn btn-sm stack-param-add" disabled={!canWrite}
        onClick={() => onVariables([...variables, { key: newKey(),
          name: `VAR_${variables.length + 1}`, value: '', description: '' }])}>
        <PlusIcon size={13} /> Add a variable
      </button>

      <h4>Secrets</h4>
      <p className="hint">
        Values no one should read off the stack — a database password, an API key — used as
        <span className="mono"> {'{{secrets.NAME}}'}</span>. The stack keeps only the name: the
        value is encrypted on each node, synced only between members, and shown again only to
        an admin who confirms their password.
      </p>
      {secrets.map((row) => {
        const saved = !renamed && savedName ? stack?.secrets[row.name] : undefined
        const waiting = row.key in pending
        const state = waiting ? (pending[row.key] === null ? 'cleared on save' : 'set on save')
          : saved?.set ? (saved.generated ? 'generated' : 'set')
            : row.generate ? 'will generate' : 'asked at launch'
        return (
          <div className="stack-value-row" key={row.key}>
            <div className="stack-value-head">
              <input className="input mono" value={row.name} placeholder="DB_PASSWORD"
                aria-label="Secret name" disabled={!canWrite}
                onChange={(event) => editSecret(row.key, { name: cleanName(event.target.value) })} />
              <span className={`badge ${saved?.set || waiting ? 'badge-info' : 'badge-dim'}`}>
                {state}
              </span>
              <button type="button" className="btn btn-ghost btn-icon btn-sm" disabled={!canWrite}
                aria-label={`Remove secret ${row.name}`}
                onClick={() => onSecrets(secrets.filter((v) => v.key !== row.key))}>
                <CloseIcon size={12} />
              </button>
            </div>
            <input className="input" value={row.description} placeholder="what it is (optional)"
              aria-label={`Description of ${row.name}`} maxLength={200} disabled={!canWrite}
              onChange={(event) => editSecret(row.key, { description: event.target.value })} />
            <label className="check">
              <input type="checkbox" checked={row.generate} disabled={!canWrite}
                onChange={(event) => editSecret(row.key, { generate: event.target.checked })} />
              <span>Generate a random value the first time a launch needs it</span>
            </label>
            {typing?.key === row.key ? (
              <form className="stack-value-set" onSubmit={(event) => {
                event.preventDefault()
                if (!typing.value) return
                onPending({ ...pending, [row.key]: typing.value })
                setTyping(null)
              }}>
                <input className="input mono" type="password" autoComplete="new-password"
                  autoFocus value={typing.value} aria-label={`New value of ${row.name}`}
                  onChange={(event) => setTyping({ key: row.key, value: event.target.value })} />
                <button type="submit" className="btn btn-sm" disabled={!typing.value}>Keep</button>
                <button type="button" className="btn btn-sm btn-ghost"
                  onClick={() => setTyping(null)}>Cancel</button>
              </form>
            ) : (
              <div className="stack-value-actions">
                <button type="button" className="btn btn-sm" disabled={!canWrite}
                  onClick={() => setTyping({ key: row.key, value: '' })}>
                  {saved?.set ? 'Replace value…' : 'Set value…'}
                </button>
                {(saved?.set || waiting) && (
                  <button type="button" className="btn btn-sm btn-ghost" disabled={!canWrite}
                    onClick={() => {
                      const next = { ...pending }
                      if (waiting && !saved?.set) delete next[row.key]
                      else next[row.key] = null
                      onPending(next)
                    }}>
                    {waiting && !saved?.set ? 'Forget' : 'Clear'}
                  </button>
                )}
                {saved?.set && !waiting && savedName && canWrite && (
                  <SecretReveal stack={savedName} secret={row.name} />
                )}
              </div>
            )}
          </div>
        )
      })}
      <button type="button" className="btn btn-sm stack-param-add" disabled={!canWrite}
        onClick={() => onSecrets([...secrets, { key: newKey(),
          name: `SECRET_${secrets.length + 1}`, description: '', generate: false }])}>
        <PlusIcon size={13} /> Add a secret
      </button>
      {issues.map((issue) => <p key={issue} className="field-error">{issue}</p>)}
    </div>
  )
}

/**
 * Said under a box that hands a stack secret to a parameter its module does
 * not declare secret: allowed, since a connection string is a fair place for
 * a password, but nothing redacts it from what that module prints.
 */
function ExposedSecret() {
  return (
    <span className="hint stack-exposed">
      This parameter is not declared secret by its module, so the module's output (shown in
      run results) will not hide the value.
    </span>
  )
}

/** How long a revealed secret stays on screen. */
const REVEAL_SECONDS = 30

/**
 * Show one stored secret, to an admin. A password session confirms the
 * password first -- the cookie alone is ambient, a token is not -- and the
 * value is dropped from the page after REVEAL_SECONDS, or when closed.
 */
function SecretReveal({ stack, secret }: { stack: string; secret: string }) {
  const auth = useAuthInfo()
  const asksPassword = auth?.principal?.via === 'session'
  const [stage, setStage] = useState<'idle' | 'asking' | 'busy'>('idle')
  const [password, setPassword] = useState('')
  const [shown, setShown] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    if (shown === null) return
    const timer = window.setTimeout(() => setShown(null), REVEAL_SECONDS * 1000)
    return () => window.clearTimeout(timer)
  }, [shown])

  async function reveal() {
    setStage('busy')
    setFailure(null)
    try {
      const result = await api.revealStackSecret(stack, secret, asksPassword ? password : undefined)
      setShown(result.value)
      setStage('idle')
    } catch (cause) {
      setFailure(cause instanceof ApiError ? cause.message : (cause as Error).message)
      setStage(asksPassword ? 'asking' : 'idle')
    } finally {
      setPassword('')
    }
  }

  if (shown !== null) {
    return (
      <div className="stack-reveal">
        <span className="mono stack-reveal-value">{shown}</span>
        <CopyButton text={shown} label={secret} />
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => setShown(null)}>
          Hide
        </button>
        <span className="hint faint">hidden again in {REVEAL_SECONDS}s</span>
      </div>
    )
  }
  if (stage === 'asking' || (stage === 'busy' && asksPassword)) {
    return (
      <form className="stack-reveal" onSubmit={(event) => {
        event.preventDefault()
        if (password) reveal()
      }}>
        <input className="input" type="password" autoComplete="current-password" autoFocus
          placeholder="your password" aria-label="Your password, to confirm" value={password}
          onChange={(event) => setPassword(event.target.value)} />
        <button type="submit" className="btn btn-sm" disabled={!password || stage === 'busy'}>
          {stage === 'busy' && <span className="spinner" />} Reveal
        </button>
        <button type="button" className="btn btn-sm btn-ghost"
          onClick={() => { setStage('idle'); setFailure(null) }}>Cancel</button>
        {failure && <span className="field-error">{failure}</span>}
      </form>
    )
  }
  return (
    <>
      <button type="button" className="btn btn-sm btn-ghost" disabled={stage === 'busy'}
        title={auth && !auth.enabled
          ? 'Sign-in is off on this node, so anyone who can open this page can reveal it'
          : 'Show the value; it is logged that you did'}
        onClick={() => (asksPassword ? setStage('asking') : reveal())}>
        Reveal…
      </button>
      {failure && <span className="field-error">{failure}</span>}
    </>
  )
}

/**
 * Which flow the canvas shows: the stack, or one of its swarms. A swarm is a
 * load test designed with the stack and run against it on demand -- never by
 * its launch -- so it gets a canvas of its own rather than stages of this one.
 */
function FlowTabs({ swarms, flow, onFlow, onAdd }: {
  swarms: SwarmDraft[]
  flow: string | null
  onFlow: (key: string | null) => void
  onAdd: () => void
}) {
  const canWrite = useCanWrite()
  return (
    <div className="stack-flow-tabs" role="tablist" aria-label="Stack and its swarms"
      onClick={(event) => event.stopPropagation()}>
      <button type="button" role="tab" aria-selected={flow === null}
        className={flow === null ? 'is-on' : undefined} onClick={() => onFlow(null)}>
        Stack
      </button>
      {swarms.map((w) => (
        <button key={w.key} type="button" role="tab" aria-selected={flow === w.key}
          className={`is-swarm${flow === w.key ? ' is-on' : ''}`} onClick={() => onFlow(w.key)}
          title="A swarm: load run against the stack on demand, torn down when it ends">
          <GaugeIcon size={13} /> {w.name || '(unnamed)'}
        </button>
      ))}
      <button type="button" className="stack-flow-add" disabled={!canWrite} onClick={onAdd}
        title="Design a load test to run against this stack while it runs">
        <PlusIcon size={13} /> Swarm
      </button>
      {swarms.length > 1 && (
        <button type="button" role="tab" aria-selected={flow === SEQUENCE}
          className={`is-sequence${flow === SEQUENCE ? ' is-on' : ''}`}
          onClick={() => onFlow(SEQUENCE)}
          title="Which swarms run one after another, and in what order">
          <ListOrderIcon /> Sequence
          <span className="stack-flow-count">{swarms.filter((w) => w.sequence).length}/{swarms.length}</span>
        </button>
      )}
    </div>
  )
}

/** What a swarm launches, in a line: `drill-load ×6 → report`. */
function draftSummary(swarm: SwarmDraft) {
  return swarm.stages.map((stage) => stage.steps.map((step) => (
    step.type === 'launch' ? `${step.template} ×${step.count}`
      : step.type === 'sleep' ? `sleep ${step.seconds}s`
        : step.type === 'script' ? step.id : 'healthy'
  )).join(' + ')).join(' → ') || 'no steps yet'
}

/**
 * The stack's sequence: its swarms top to bottom, each switched in or out.
 * Drawn like a flow rather than a list of settings, since what it decides is
 * what happens after what -- and an off swarm keeps its place, greyed, so
 * turning it back on puts it where it was.
 */
function SequenceView({ swarms, onToggle, onMove, onOpen }: {
  swarms: SwarmDraft[]
  onToggle: (key: string, on: boolean) => void
  onMove: (key: string, by: number) => void
  onOpen: (key: string) => void
}) {
  const canWrite = useCanWrite()
  const order = swarms.filter((w) => w.sequence).map((w) => w.key)
  return (
    <div className="stack-sequence" onClick={(event) => event.stopPropagation()}>
      <div className="stack-sequence-intro">
        <h3><ListOrderIcon /> Sequence</h3>
        <p>
          <strong>Run sequence</strong> on the Stacks tab runs the swarms switched on here, top to
          bottom. Each one starts only after the one above it has passed, and its instances are
          destroyed before the next one starts. If one fails or is stopped, the rest are skipped.
        </p>
        <p className="faint">
          A swarm that is off still runs on its own from the Stacks tab; it is only left out of
          the sequence.
        </p>
      </div>
      <div className="stack-flow">
        <div className="stack-terminal stack-terminal-running">Running stack</div>
        {swarms.map((w, index) => {
          const place = order.indexOf(w.key)
          return (
            <div key={w.key} className="stack-flow-stage">
              <div className="stack-gap"><div className="stack-gap-line" /></div>
              <div className={`stack-sequence-card${w.sequence ? '' : ' is-off'}`}>
                <span className="stack-sequence-place" aria-hidden="true">
                  {w.sequence ? place + 1 : '–'}
                </span>
                <button type="button" className="stack-sequence-text" onClick={() => onOpen(w.key)}
                  title="Open this swarm's steps">
                  <strong className="mono">{w.name || '(unnamed)'}</strong>
                  {w.description && <span className="faint">{w.description}</span>}
                  <span className="faint mono truncate">{draftSummary(w)}</span>
                  <span className="stack-sequence-state">
                    {w.sequence
                      ? place === 0 ? 'Runs first' : `Runs if #${place} passed`
                      : 'Off — skipped by the sequence'}
                  </span>
                </button>
                <label className="stack-sequence-switch"
                  title={w.sequence ? 'Leave this swarm out of the sequence' : 'Put this swarm in the sequence'}>
                  <input type="checkbox" checked={w.sequence} disabled={!canWrite}
                    aria-label={`${w.name} is in the sequence`}
                    onChange={(event) => onToggle(w.key, event.target.checked)} />
                  <span className="stack-toggle-track" aria-hidden="true" />
                  <span>{w.sequence ? 'On' : 'Off'}</span>
                </label>
                <span className="stack-sequence-move">
                  <button type="button" className="btn btn-ghost btn-icon btn-sm"
                    aria-label={`Move ${w.name} up`} title="Move up"
                    disabled={!canWrite || index === 0} onClick={() => onMove(w.key, -1)}>
                    <ChevronUpIcon />
                  </button>
                  <button type="button" className="btn btn-ghost btn-icon btn-sm"
                    aria-label={`Move ${w.name} down`} title="Move down"
                    disabled={!canWrite || index === swarms.length - 1}
                    onClick={() => onMove(w.key, 1)}>
                    <ChevronDownIcon />
                  </button>
                </span>
              </div>
            </div>
          )
        })}
        <div className="stack-gap"><div className="stack-gap-line" /></div>
        <div className="stack-terminal">
          {order.length === 0 ? 'Nothing in the sequence' : 'Sequence passed'}
        </div>
      </div>
    </div>
  )
}

/** A swarm's own name and description, and what it is, above its canvas. */
function SwarmHeader({ swarm, issues, templates, place, many, onSequence, onChange, onRemove }: {
  swarm: SwarmDraft
  issues: string[]
  templates: InstanceTemplate[]
  /** Its place in the stack's sequence, or -1 when it is not in it. */
  place: number
  /** Whether there is a sequence to speak of: more than one swarm. */
  many: boolean
  onSequence: () => void
  onChange: (change: Partial<SwarmDraft>) => void
  onRemove: () => void
}) {
  const canWrite = useCanWrite()
  const lasting = [...new Set(swarm.stages.flatMap((s) => s.steps)
    .filter((s): s is Launch => s.type === 'launch')
    .filter((s) => templates.find((t) => t.name === s.template)?.ephemeral === false)
    .map((s) => s.template))]
  return (
    <div className="stack-swarm-head" onClick={(event) => event.stopPropagation()}>
      <div className="stack-swarm-names">
        <input className="input mono" value={swarm.name} aria-label="Swarm name"
          placeholder="heavy" disabled={!canWrite}
          onChange={(event) => onChange({
            name: event.target.value.replace(/[^A-Za-z0-9_]/g, '_').slice(0, 64) })} />
        <input className="input" value={swarm.description} aria-label="Swarm description"
          placeholder="what this load is (optional)" maxLength={200} disabled={!canWrite}
          onChange={(event) => onChange({ description: event.target.value })} />
        {many && (
          <button type="button" className="btn btn-sm btn-ghost" onClick={onSequence}
            title="Set the order and which swarms are in the sequence">
            <ListOrderIcon />
            {place >= 0 ? `Sequence: #${place + 1}` : 'Not in the sequence'}
          </button>
        )}
        <button type="button" className="btn btn-sm btn-danger" disabled={!canWrite}
          onClick={onRemove}>
          <TrashIcon size={13} /> Remove swarm
        </button>
      </div>
      <p className="hint">
        Started from the Stacks tab while the stack runs, never by its launch. Every step of
        the stack is an earlier step here, so a load generator can be pointed at{' '}
        <span className="mono">{'{{app.ips}}'}</span>. Counts go to 200 and can be changed when
        it is started. However it ends — its last stage done, a step failed, or Stop — its
        instances are destroyed.
      </p>
      {lasting.length > 0 && (
        <p className="hint stack-exposed">
          {lasting.join(', ')} {lasting.length === 1 ? 'is' : 'are'} not ephemeral: the swarm
          still destroys those instances when it ends, but if lemondx stops mid-swarm they stay
          until the swarm is stopped.
        </p>
      )}
      {issues.map((issue) => <p key={issue} className="field-error">{issue}</p>)}
    </div>
  )
}

function PaletteItem({ icon, label, detail, mono, onDragStart, onDragEnd, onAdd }: {
  icon: ReactNode
  label: string
  detail: string
  mono?: boolean
  onDragStart: (event: DragEvent) => void
  onDragEnd: () => void
  onAdd: () => void
}) {
  return (
    <button type="button" className="stack-palette-item" draggable
      onDragStart={onDragStart} onDragEnd={onDragEnd} onClick={onAdd}
      title={`Add “${label}” as a new last stage, or drag it into place`}>
      <span className="stack-palette-icon">{icon}</span>
      <span className="stack-palette-text">
        <strong>{label}</strong>
        <span className={mono ? 'mono' : undefined}>{detail}</span>
      </span>
    </button>
  )
}

function stepTitle(step: StackStep) {
  if (step.type === 'sleep') return `Sleep ${step.seconds}s`
  if (step.type === 'wait_healthy') return 'Wait until healthy'
  if (step.type === 'script') return 'Run a script'
  return step.template || 'Launch'
}

/**
 * One step's chips: what it made on one row, then its parameters under the
 * module each belongs to -- `USERNAME` means little until it says SSH access.
 * With `limit`, at most that many parameters, then "+N"; a card has no room
 * for a template's every parameter, and the inspector lists them all.
 */
function PortGroups({ suggestions, stage, fromCanvas, limit, onClick, clickable }: {
  suggestions: Suggestion[]
  stage: number
  fromCanvas?: boolean
  limit?: number
  onClick?: (ref: string) => void
  clickable?: boolean
}) {
  const chip = (s: Suggestion) => (
    <RefChip key={s.ref} suggestion={s} stage={stage} fromCanvas={fromCanvas}
      clickable={clickable} onClick={onClick && (() => onClick(s.ref))} />
  )
  const fields = suggestions.filter((s) => s.kind === 'field')
  // Everything but the fields is grouped: a template's parameters by module,
  // the stack's own values under "Stack variables" and "Stack secrets".
  const params = suggestions.filter((s) => s.kind !== 'field')
  const cut = limit ?? params.length
  const visible = new Set(params.slice(0, cut).map((s) => s.ref))
  const hidden = params.slice(cut)
  return (
    <>
      {fields.length > 0 && <div className="stack-step-ports">{fields.map(chip)}</div>}
      {byGroup(params).map(([group, members]) => {
        const shown = members.filter((s) => visible.has(s.ref))
        if (shown.length === 0) return null
        return (
          <div className="stack-ports-group" key={group}>
            <span className="stack-ports-module" title={group}>{group}</span>
            <div className="stack-step-ports">{shown.map(chip)}</div>
          </div>
        )
      })}
      {hidden.length > 0 && (
        <span className="stack-ref-more"
          title={hidden.map((s) => `${s.group}: ${s.label}`).join('\n')}>
          +{hidden.length} more in a later step’s inspector
        </span>
      )}
    </>
  )
}

/** How many parameter chips a card shows before "+N"; the inspector lists them all. */
const CARD_PARAMS = 4

function StepCard({ step, selected, issues, templates, outputs, stageIndex, uses, available,
                    linked, inSwarm,
                    receiving, onRefOver, onRefLeave, onSelect, onRemove, onDragStart,
                    onDragEnd }: {
  /** Part of a swarm, whose instances are meant to be ephemeral. */
  inSwarm?: boolean
  step: Item
  selected: boolean
  issues: string[]
  templates: InstanceTemplate[]
  /** What it hands on: empty for anything but a launch. */
  outputs: Suggestion[]
  stageIndex: number
  /** What it takes from earlier steps, by source step id. */
  uses: Map<string, string[]>
  /** Every reference the earlier stages hand on. */
  available: Map<string, Suggestion>
  /** Pointed at from a chip or badge elsewhere: one end of a wire. */
  linked: boolean
  /** A reference is being dragged: whether this step could take it, else null. */
  receiving: boolean | null
  onRefOver: () => void
  onRefLeave: () => void
  onSelect: () => void
  onRemove: () => void
  onDragStart: (event: DragEvent) => void
  onDragEnd: () => void
}) {
  const wiring = useWiring()
  const template = step.type === 'launch' ? templates.find((t) => t.name === step.template) : null
  const icon = step.type === 'sleep' ? <ClockIcon size={14} />
    : step.type === 'wait_healthy' ? <HeartIcon size={14} />
      : step.type === 'script' ? <TerminalIcon size={14} /> : <ServerIcon size={14} />
  return (
    <div className={`stack-step stack-step-${step.type}${selected ? ' is-selected' : ''}${
      issues.length ? ' has-issues' : ''}${linked ? ' is-linked' : ''}${
      receiving === true ? ' can-receive' : receiving === false ? ' is-dimmed' : ''}`}
      draggable onDragStart={onDragStart} onDragEnd={onDragEnd}
      onDragOver={() => { if (receiving) onRefOver() }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) onRefLeave()
      }}
      onClick={(event) => { event.stopPropagation(); onSelect() }}
      onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') onSelect() }}
      role="button" tabIndex={0} aria-pressed={selected}
      title={issues.join('\n') || undefined}>
      <div className="stack-step-head">
        <span className="stack-step-grip"><GripIcon size={13} /></span>
        <span className="stack-step-icon">{icon}</span>
        <strong className="truncate">{stepTitle(step)}</strong>
        <button type="button" className="btn btn-ghost btn-icon btn-sm stack-step-remove"
          aria-label={`Remove step ${step.id}`} title="Remove"
          onClick={(event) => { event.stopPropagation(); onRemove() }}>
          <CloseIcon size={12} />
        </button>
      </div>
      <div className="stack-step-meta">
        <span className="badge badge-dim mono">{step.id}</span>
        {step.type === 'launch' && (
          <>
            <span className="badge badge-info">×{step.count}</span>
            {(step.min_percent ?? 100) < 100 && (
              <span className="badge badge-dim" title="The share that must come up">
                ≥{step.min_percent}%
              </span>
            )}
            {!step.wait_bootstrap && <span className="badge badge-warn">no wait</span>}
            {inSwarm && template && !template.ephemeral && (
              <span className="badge badge-warn" title={'The template is not ephemeral. The '
                + 'swarm still destroys its instances when it ends, but if lemondx stops '
                + 'mid-swarm they stay until the swarm is stopped.'}>
                not ephemeral
              </span>
            )}
            {(step.nodes.length > 0 || step.groups.length > 0) && (
              <span className="badge badge-dim" title={[...step.nodes, ...step.groups.map(
                (g) => `group ${g}`)].join(', ')}>
                {step.groups.length ? step.groups[0] : `${step.nodes.length} node(s)`}
              </span>
            )}
            {Object.keys(step.params).length > 0 && (
              <span className="badge badge-dim">{Object.keys(step.params).length} param(s)</span>
            )}
          </>
        )}
        {step.type === 'wait_healthy' && (
          <span className="faint">up to {Math.round(step.timeout_seconds / 60)} min</span>
        )}
        {step.type === 'script' && (
          <>
            <span className="badge badge-warn" title="Runs on the node driving the stack">
              this host
            </span>
            <span className="faint">up to {step.timeout_seconds}s</span>
            {Object.keys(step.params).length > 0 && (
              <span className="badge badge-dim">{Object.keys(step.params).length} param(s)</span>
            )}
          </>
        )}
      </div>
      {step.type === 'script' && (
        <div className="stack-step-sub mono truncate">
          {step.script.split('\n').find((line) => line.trim() && !line.startsWith('#!'))
            ?? step.script}
        </div>
      )}
      {step.type === 'launch' && template && (
        <div className="stack-step-sub mono truncate">
          {(step.prefix || template.name_prefix)}-N · {madeFrom(template)}
        </div>
      )}
      {issues.length > 0 && <div className="stack-step-issue">{issues[0]}</div>}
      {uses.size > 0 && (
        <div className="stack-step-uses" aria-label="Takes from earlier steps">
          {[...uses].map(([source, refs]) => {
            const label = source === 'params' ? 'at launch' : source === 'vars' ? 'stack variables'
              : source === 'secrets' ? 'stack secrets' : source
            // A parameter by its bare name, as its chip shows it; the
            // tooltip says which module it belongs to.
            const what = refs.map((ref) => available.get(ref)?.label
              ?? ref.slice(source.length + 1)).join(', ')
            const bad = refs.some((ref) => !INPUT_REF.test(ref) && !available.has(ref))
            return (
              <span key={source}
                className={`stack-use${bad ? ' is-bad' : ''}`}
                title={refs.map((ref) => {
                  const found = available.get(ref)
                  return `{{${ref}}}${found ? ` — ${found.meaning}` : ''}`
                }).join('\n')}
                onMouseEnter={() => wiring?.point({ source, refs })}
                onMouseLeave={() => wiring?.point(null)}>
                <span aria-hidden="true">←</span> <strong className="mono">{label}</strong>
                <span className="mono truncate">{what}</span>
              </span>
            )
          })}
        </div>
      )}
      {outputs.length > 0 && (
        <div className="stack-step-outputs" aria-label={`What ${step.id} hands on`}>
          <span className="stack-step-outputs-label">hands on →</span>
          <PortGroups suggestions={outputs} stage={stageIndex} fromCanvas limit={CARD_PARAMS} />
        </div>
      )}
    </div>
  )
}

function Inspector({ step, stageIndex, stages, templates, modules, nodes, groups, issues,
                     onChange, onMove, onSplit, onRemove }: {
  step: Item
  stageIndex: number
  stages: Stage[]
  templates: InstanceTemplate[]
  modules: BootstrapModule[]
  nodes: ClusterNode[]
  groups: NodeGroup[]
  issues: string[]
  onChange: (change: Partial<StackStep>) => void
  onMove: (delta: number) => void
  onSplit: () => void
  onRemove: () => void
}) {
  const stage = stages[stageIndex]
  const kind = step.type === 'launch' ? 'Launch' : step.type === 'sleep' ? 'Sleep'
    : step.type === 'script' ? 'Run a script' : 'Wait until healthy'
  return (
    <div className="stack-inspector-body">
      <div className="stack-inspector-head">
        <h4>{kind}</h4>
        <span className="faint">stage {stageIndex + 1}</span>
      </div>

      <div className="field">
        <label htmlFor="step-id">Step id</label>
        <input id="step-id" className="input mono" value={step.id}
          aria-invalid={!STEP_ID.test(step.id)}
          onChange={(event) => onChange({
            id: event.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_').slice(0, 32) })} />
        <span className="hint">
          {step.type === 'launch'
            ? <>What later steps call it, as in {`{{${step.id}.ip}}`}.</>
            : 'Its name in the run’s progress and in the log.'}
        </span>
      </div>

      {step.type === 'launch' && (
        <LaunchSettings step={step} stageIndex={stageIndex} stages={stages}
          templates={templates} modules={modules} nodes={nodes} groups={groups}
          onChange={onChange} />
      )}

      {step.type === 'script' && (
        <ScriptSettings step={step} stageIndex={stageIndex} stages={stages}
          templates={templates} modules={modules} onChange={onChange} />
      )}

      {step.type === 'sleep' && (
        <div className="field">
          <label htmlFor="step-seconds">Seconds</label>
          <input id="step-seconds" className="input num" type="number" min={1} max={86400}
            value={step.seconds}
            onChange={(event) => onChange({ seconds: Number(event.target.value) || 1 })} />
          <span className="hint">A fixed pause, for something that needs time to settle.</span>
        </div>
      )}

      {step.type === 'wait_healthy' && (
        <div className="field">
          <label htmlFor="step-timeout">Give up after (seconds)</label>
          <input id="step-timeout" className="input num" type="number" min={10} max={86400}
            value={step.timeout_seconds}
            onChange={(event) => onChange({ timeout_seconds: Number(event.target.value) || 10 })} />
          <span className="hint">
            Waits until every instance launched by an earlier stage has finished
            bootstrapping and its health check is green — including its template's app check,
            if it has one. Health checks must be on for each node involved.
          </span>
        </div>
      )}

      {issues.map((issue) => <p key={issue} className="field-error">{issue}</p>)}

      <div className="stack-inspector-moves">
        <button type="button" className="btn btn-sm" disabled={stageIndex === 0}
          onClick={() => onMove(-1)}>Join stage above</button>
        <button type="button" className="btn btn-sm"
          disabled={stageIndex === stages.length - 1 && stage.steps.length === 1}
          onClick={() => onMove(1)}>Join stage below</button>
        {stage.steps.length > 1 && (
          <button type="button" className="btn btn-sm" onClick={onSplit}>Run after the others</button>
        )}
        <button type="button" className="btn btn-sm btn-danger" onClick={onRemove}>
          <TrashIcon size={13} /> Remove
        </button>
      </div>
    </div>
  )
}

function LaunchSettings({ step, stageIndex, stages, templates, modules, nodes, groups,
                          onChange }: {
  step: StackLaunchStep
  stageIndex: number
  stages: Stage[]
  templates: InstanceTemplate[]
  modules: BootstrapModule[]
  nodes: ClusterNode[]
  groups: NodeGroup[]
  onChange: (change: Partial<StackLaunchStep>) => void
}) {
  const template = templates.find((t) => t.name === step.template)
  const flowCtx = useContext(FlowContext)
  const earlier = [...flowCtx.base, ...launchesBefore(stages, stageIndex)]
  const max = flowCtx.swarm ? SWARM_MAX_COUNT : MAX_COUNT
  // Secrets any launch in the stack asks for: the obvious things to hand on,
  // since one password typed at launch is usually wanted by several steps;
  // and every input some step already uses, so the next one reuses it.
  const entered = [...new Set(stages.flatMap((s) => s.steps).flatMap((s) => (
    s.type === 'launch'
      ? [...modules.filter((m) => templates.find((t) => t.name === s.template)?.bootstrap.modules
        .includes(m.id)).flatMap((m) => m.params).filter((p) => p.secret).map((p) => p.name),
      ...Object.values(s.params).flatMap(refsIn).filter((ref) => INPUT_REF.test(ref))
        .map((ref) => ref.slice('params.'.length))]
      : [])))].sort()
  const [where, setWhere] = useState<'here' | 'nodes' | 'group'>(
    step.groups.length ? 'group' : step.nodes.length ? 'nodes' : 'here')

  return (
    <>
      <div className="field">
        <label htmlFor="step-template">Template</label>
        <select id="step-template" className="select" value={step.template}
          onChange={(event) => onChange({ template: event.target.value })}>
          {!template && <option value={step.template}>{step.template || 'Pick one…'}</option>}
          {templates.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
        </select>
      </div>

      <div className="grid-2">
        <div className="field">
          <label htmlFor="step-count">Instances</label>
          <input id="step-count" className="input num" type="number" min={1} max={max}
            value={step.count}
            onChange={(event) => onChange({
              count: Math.min(max, Math.max(1, Number(event.target.value) || 1)) })} />
          {flowCtx.swarm && (
            <span className="hint">The swarm's default; it can be changed when it is started.</span>
          )}
        </div>
        <div className="field">
          <label htmlFor="step-prefix">Name prefix</label>
          <input id="step-prefix" className="input mono" value={step.prefix}
            placeholder={template?.name_prefix ?? ''}
            onChange={(event) => onChange({ prefix: event.target.value.trim() })} />
        </div>
      </div>

      {(step.count > 1 || step.min_percent < 100) && (
        <div className="field">
          <label htmlFor="step-min">Must start</label>
          <div className="stack-min">
            <input id="step-min" className="input num" type="number" min={1} max={100}
              value={step.min_percent ?? 100}
              onChange={(event) => onChange({
                min_percent: Math.min(100, Math.max(1, Number(event.target.value) || 100)) })} />
            <span>% of the instances</span>
          </div>
          <span className="hint">
            {(step.min_percent ?? 100) >= 100
              ? 'Every instance must come up, or the step fails.'
              : `The step succeeds when at least ${Math.ceil(step.count
                * (step.min_percent ?? 100) / 100)} of ${step.count} come up; the rest are noted. `
                + 'Useful for a large swarm, where one node failing should not end the test.'}
          </span>
        </div>
      )}

      <label className="stack-toggle">
        <input type="checkbox" checked={step.wait_bootstrap}
          onChange={(event) => onChange({ wait_bootstrap: event.target.checked })} />
        <span className="stack-toggle-track" aria-hidden="true" />
        <span>
          <strong>Wait for bootstrap modules</strong>
          <span className="hint">
            {step.wait_bootstrap
              ? 'The next stage starts when every module has finished on every instance.'
              : 'The next stage starts as soon as the instances are running; modules carry on in the background.'}
          </span>
        </span>
      </label>

      {nodes.length > 1 && (
        <div className="field">
          <label>Where</label>
          <div className="stack-segmented" role="radiogroup">
            {(['here', 'nodes', 'group'] as const).map((choice) => (
              <button key={choice} type="button" role="radio" aria-checked={where === choice}
                onClick={() => {
                  setWhere(choice)
                  if (choice === 'here') onChange({ nodes: [], groups: [] })
                  if (choice === 'nodes') onChange({ groups: [] })
                  if (choice === 'group') onChange({ nodes: [] })
                }}>
                {choice === 'here' ? 'This node' : choice === 'nodes' ? 'Nodes' : 'Group'}
              </button>
            ))}
          </div>
          {where === 'nodes' && (
            <div className="check-list">
              {nodes.map((node) => (
                <label key={node.name} className="check">
                  <input type="checkbox" checked={step.nodes.includes(node.name)}
                    onChange={() => onChange({ nodes: step.nodes.includes(node.name)
                      ? step.nodes.filter((n) => n !== node.name) : [...step.nodes, node.name] })} />
                  <span>{node.name}{node.self ? ' (this node)' : ''}</span>
                </label>
              ))}
            </div>
          )}
          {where === 'group' && (
            <select className="select" value={step.groups[0] ?? ''}
              onChange={(event) => onChange({ groups: event.target.value ? [event.target.value] : [] })}>
              <option value="">Pick a group…</option>
              {groups.map((g) => (
                <option key={g.name} value={g.name}>{g.name} ({g.members.length})</option>
              ))}
            </select>
          )}
          <span className="hint">Instances go round robin over the nodes chosen.</span>
        </div>
      )}

      <LaunchParams step={step} template={template} templates={templates} modules={modules}
        earlier={earlier} entered={entered} onChange={onChange} />
    </>
  )
}

/**
 * A script step: the script, its timeout, and its parameters -- which are a
 * launch's, as environment variables: any value, holding any reference a
 * launch's parameter may, from the same list of chips. The references can go
 * straight into the script too; the parameters are for a script that should
 * read `$DB_HOST` like any other, and for a secret, which as an environment
 * variable never sits in the script's text.
 */
function ScriptSettings({ step, stageIndex, stages, templates, modules, onChange }: {
  step: StackScriptStep
  stageIndex: number
  stages: Stage[]
  templates: InstanceTemplate[]
  modules: BootstrapModule[]
  onChange: (change: Partial<StackScriptStep>) => void
}) {
  const own = useContext(StackValuesContext)
  const wiring = useWiring()
  const flowCtx = useContext(FlowContext)
  const earlier = [...flowCtx.base, ...launchesBefore(stages, stageIndex)]
  // Inputs some step already uses, so this one can share them.
  const entered = [...new Set(stages.flatMap((s) => s.steps).flatMap((s) => (
    s.type === 'launch' || s.type === 'script'
      ? [...Object.values(s.params), ...(s.type === 'script' ? [s.script] : [])] : []))
    .flatMap(refsIn).filter((ref) => INPUT_REF.test(ref))
    .map((ref) => ref.slice('params.'.length)))].sort()
  const suggestions = suggestionsFor(earlier, entered, templates, modules, own)
  const [active, setActive] = useState<string | null>(null)
  const insert = useRef<((ref: string) => void) | null>(null)
  const activate = (key: string) => (into: (ref: string) => void) => {
    insert.current = into
    setActive(key)
  }
  const area = useRef<HTMLTextAreaElement | null>(null)
  const script = useRef(step.script)
  useEffect(() => { script.current = step.script }, [step.script])
  const names = Object.keys(step.params)

  /** A chip clicked into the script: at the caret, never inside another placeholder. */
  function intoScript(ref: string) {
    const at = outsidePlaceholder(script.current,
      area.current?.selectionStart ?? script.current.length)
    const text = `{{${ref}}}`
    onChange({ script: script.current.slice(0, at) + text + script.current.slice(at) })
    requestAnimationFrame(() => {
      area.current?.focus()
      area.current?.setSelectionRange(at + text.length, at + text.length)
    })
  }

  function set(name: string, value: string) {
    onChange({ params: { ...step.params, [name]: value } })
  }

  function rename(from: string, to: string) {
    const next: Record<string, string> = {}
    for (const [key, value] of Object.entries(step.params)) next[key === from ? to : key] = value
    onChange({ params: next })
  }

  function remove(name: string) {
    const next = { ...step.params }
    delete next[name]
    onChange({ params: next })
  }

  return (
    <>
      <AvailableRefs earlier={earlier} suggestions={suggestions} active={active}
        onInsert={(ref) => insert.current?.(ref)} />

      <div className="field">
        <label htmlFor="step-script">Script</label>
        <textarea id="step-script" ref={area} className="input mono stack-script" rows={8}
          spellCheck={false} value={step.script}
          onChange={(event) => onChange({ script: event.target.value })}
          onFocus={() => activate('script')(intoScript)}
          // A reference chip lands where it is let go, as the browser drops
          // text into a box; anything else dragged here -- a step being moved
          // carries its key as text -- is refused rather than typed in.
          onDragOver={(event) => {
            if (wiring?.dragging) return
            event.preventDefault()
            event.dataTransfer.dropEffect = 'none'
          }}
          onDrop={(event) => {
            if (!wiring?.dragging) event.preventDefault()
          }} />
        <ComesFrom value={step.script} suggestions={suggestions} />
        <span className="hint">
          Runs with <span className="mono">/bin/sh</span> on the node driving the stack, as the
          account lemondx runs as, in an empty directory of its own. Placeholders are filled in
          first, as text — quote them: <span className="mono">"{'{{vars.NAME}}'}"</span>. A
          non-zero exit, or running past the timeout, fails the stack.
        </span>
      </div>

      <div className="field">
        <label htmlFor="step-script-timeout">Give up after (seconds)</label>
        <input id="step-script-timeout" className="input num" type="number" min={1} max={86400}
          value={step.timeout_seconds}
          onChange={(event) => onChange({
            timeout_seconds: Math.min(86400, Math.max(1, Number(event.target.value) || 1)) })} />
        <span className="hint">Then it is stopped, with everything it started.</span>
      </div>

      <div className="field stack-params">
        <label>Parameters</label>
        <span className="hint">
          Handed to the script as environment variables, like a template's parameters to its
          modules. A secret is best passed this way: as <span className="mono">$NAME</span> it
          never sits in the script's text. The script also gets every value entered at launch
          under its own name, and <span className="mono">$LEMONDX_STACK_{'<ID>'}_IPS</span>,
          _NAMES, _IPV6 and _NODES for each earlier launch.
        </span>
        <div className="stack-param-group">
          {names.map((name) => (
            <div className="stack-param-row is-set" key={name}>
              <div className="stack-param-head">
                <input className="input mono stack-param-name-input" value={name}
                  aria-label="Variable name"
                  onChange={(event) => rename(name, event.target.value
                    .replace(/[^A-Za-z0-9_]/g, '_').slice(0, 64))} />
                <button type="button" className="btn btn-ghost btn-icon btn-sm"
                  aria-label={`Remove ${name}`} onClick={() => remove(name)}>
                  <CloseIcon size={12} />
                </button>
              </div>
              <ReferenceInput id={`param-${step.id}-env-${name}`} value={step.params[name] ?? ''}
                suggestions={suggestions} placeholder="value" onActivate={activate(name)}
                onChange={(next) => set(name, next)} />
              <ComesFrom value={step.params[name] ?? ''} suggestions={suggestions} />
            </div>
          ))}
          <button type="button" className="btn btn-sm stack-param-add"
            onClick={() => set(`VAR_${names.length + 1}`, '')}>
            <PlusIcon size={13} /> Add a parameter
          </button>
        </div>
      </div>
    </>
  )
}

/**
 * Every parameter this launch will actually run with, and what each one's value
 * comes from.
 *
 * Showing only the overrides — which is what a stack file stores — meant the
 * panel answered "what did I change?" when the question being asked is "what
 * will this launch use?". So each of the template's modules lists its declared
 * parameters, the inherited value sits in the box as a placeholder until it is
 * typed over, and an override is marked. A name no module declares can still be
 * added: modules are free to read whatever they like out of the environment.
 */
function LaunchParams({ step, template, templates, modules, earlier, entered, onChange }: {
  step: StackLaunchStep
  template?: InstanceTemplate
  templates: InstanceTemplate[]
  modules: BootstrapModule[]
  earlier: { step: Launch; stage: number }[]
  entered: string[]
  onChange: (change: Partial<StackLaunchStep>) => void
}) {
  // One group per time a module is in the template: a repeat's parameters
  // are its own, under NAME@n.
  const used = occurrences(template?.bootstrap.modules ?? []).flatMap(([id, n]) => {
    const module = modules.find((m) => m.id === id)
    return module ? [{ module, n }] : []
  })
  const repeated = new Set(used.filter(({ n }) => n > 1).map(({ module }) => module.id))
  const declared = new Set(used.flatMap(({ module, n }) =>
    module.params.map((p) => occurrenceKey(p.name, n))))
  const own = useContext(StackValuesContext)
  const suggestions = suggestionsFor(earlier, entered, templates, modules, own)
  // The box a chip click goes into: the one last focused, and how to reach it.
  const [active, setActive] = useState<string | null>(null)
  const insert = useRef<((ref: string) => void) | null>(null)
  const activate = (key: string) => (into: (ref: string) => void) => {
    insert.current = into
    setActive(key)
  }
  // Set by the step but declared by no module of its template: either a name
  // the module reads straight from the environment, or one left behind when
  // the template's modules changed. Either way it is the user's to keep.
  const extras = Object.keys(step.params).filter((name) => !declared.has(name)).sort()

  function set(name: string, value: string) {
    onChange({ params: { ...step.params, [name]: value } })
  }

  function clear(name: string) {
    const next = { ...step.params }
    delete next[name]
    onChange({ params: next })
  }

  function rename(from: string, to: string) {
    const next: Record<string, string> = {}
    // Rebuilt in order so the row does not jump around while it is being typed.
    for (const [key, value] of Object.entries(step.params)) next[key === from ? to : key] = value
    delete next['']
    onChange({ params: next })
  }

  return (
    <div className="field stack-params">
      <label>Parameters</label>
      <span className="hint">
        What this launch runs with. A box left empty uses the value shown in it — from the
        template, or the module's own default.
      </span>

      <AvailableRefs earlier={earlier} suggestions={suggestions} active={active}
        onInsert={(ref) => insert.current?.(ref)} />

      {!template && <p className="hint">Pick a template to see its parameters.</p>}

      {used.map(({ module, n }) => (
        <div className="stack-param-group" key={`${module.id}@${n}`}>
          <h5>
            {module.name}{repeated.has(module.id) ? ` #${n}` : ''}
            <span className="faint">{module.params.length} parameter(s)</span>
          </h5>
          {module.params.length === 0 && (
            <p className="hint">This module declares none.</p>
          )}
          {module.params.map((param) => {
            const key = occurrenceKey(param.name, n)
            const overridden = key in step.params
            const value = step.params[key] ?? ''
            // What it would be without an override: the template's saved answer,
            // else the module's own effective default. A secret has neither --
            // it is never stored anywhere, so it is asked for at launch.
            const inherited = template?.bootstrap.params[key] ?? param.value ?? param.default
            const refs = refsIn(value)
            const literal = value.replace(PLACEHOLDER, '').trim()
            const badRef = refs.find((ref) => !INPUT_REF.test(ref)
              && !suggestions.some((s) => s.ref === ref))
            const secretLiteral = param.secret && overridden
              && (literal !== '' || refs.some((ref) => !INPUT_REF.test(ref) && !SECRET_REF.test(ref)))
            const exposed = !param.secret && refs.some((ref) => SECRET_REF.test(ref))
            // A stack secret of the same name fills this one when it is left
            // alone, as the server does -- so say so instead of "asked for".
            const covering = param.secret && !overridden
              ? own.secrets.find((v) => v.name === param.name) : undefined
            return (
              <div className={`stack-param-row${overridden ? ' is-set' : ''}`} key={key}>
                <div className="stack-param-head">
                  <span className="mono stack-param-name">{key}</span>
                  {overridden && <span className="badge badge-dim">set here</span>}
                </div>
                <ReferenceInput
                  id={`param-${step.id}-${key}`}
                  value={value}
                  secret={param.secret}
                  invalid={Boolean(badRef) || secretLiteral}
                  placeholder={covering ? `the stack secret ${param.name}`
                    : param.secret ? 'asked for when the stack is launched'
                      : inherited || 'no default'}
                  suggestions={suggestions}
                  onActivate={activate(key)}
                  onChange={(next) => (next === '' ? clear(key) : set(key, next))}
                />
                <ComesFrom value={value} suggestions={suggestions} />
                {param.description && <span className="hint">{param.description}</span>}
                {!overridden && !param.secret && (
                  <span className="hint faint">
                    {template?.bootstrap.params[key] !== undefined
                      ? 'from the template'
                      : 'the module’s default'}
                  </span>
                )}
                {covering && (
                  <span className="hint faint">
                    Filled from the stack secret <span className="mono">{param.name}</span>
                    {covering.generate ? ', generated at the first launch' : ''}: nothing is
                    asked for. Set this box to use another value.
                  </span>
                )}
                {param.secret && !overridden && !covering && (
                  <span className="hint faint">
                    Secret: never written into the stack. Left alone it is asked for at
                    every launch — unless the stack has a secret of the same name. Give it
                    one of the stack's secrets,{' '}
                    <span className="mono">{'{{secrets.NAME}}'}</span>, to keep it, or{' '}
                    <span className="mono">{'{{params.NAME}}'}</span> to share one typed value
                    between steps.
                  </span>
                )}
                {secretLiteral && (
                  <span className="field-error">
                    A secret cannot be written into a stack. Use one of the stack's secrets,
                    {' '}{'{{secrets.NAME}}'}, or {'{{params.NAME}}'} to enter it at launch.
                  </span>
                )}
                {exposed && <ExposedSecret />}
                {badRef && (
                  <span className="field-error">
                    {'{{'}{badRef}{'}}'} is not something this step can use: not an earlier
                    launch's, nor one of the stack's variables or secrets.
                  </span>
                )}
              </div>
            )
          })}
        </div>
      ))}

      <div className="stack-param-group">
        <h5>Anything else<span className="faint">not declared by a module</span></h5>
        {extras.map((name) => (
          <div className="stack-param-row is-set" key={name}>
            <div className="stack-param-head">
              <input className="input mono stack-param-name-input" value={name}
                aria-label="Parameter name"
                onChange={(event) => rename(name, event.target.value.toUpperCase()
                  .replace(/[^A-Z0-9_]/g, '_').slice(0, 64))} />
              <button type="button" className="btn btn-ghost btn-icon btn-sm"
                aria-label={`Remove ${name}`} onClick={() => clear(name)}>
                <CloseIcon size={12} />
              </button>
            </div>
            <ReferenceInput
              id={`param-${step.id}-extra-${name}`}
              value={step.params[name] ?? ''}
              suggestions={suggestions}
              placeholder="value"
              onActivate={activate(name)}
              onChange={(next) => set(name, next)}
            />
            <ComesFrom value={step.params[name] ?? ''} suggestions={suggestions} />
            {refsIn(step.params[name] ?? '').some((ref) => SECRET_REF.test(ref)) && (
              <ExposedSecret />
            )}
          </div>
        ))}
        <button type="button" className="btn btn-sm stack-param-add"
          onClick={() => set(`PARAM_${extras.length + 1}`, '')}>
          <PlusIcon size={13} /> Add a parameter
        </button>
      </div>

    </div>
  )
}

/**
 * What this launch can take from the ones before it, as chips to drag onto a
 * parameter or click into the box last used: the canvas shows each step's
 * outputs on its card, but this is where they are within reach of the boxes.
 */
function AvailableRefs({ earlier, suggestions, active, onInsert }: {
  earlier: { step: Launch; stage: number }[]
  suggestions: Suggestion[]
  active: string | null
  onInsert: (ref: string) => void
}) {
  const flowCtx = useContext(FlowContext)
  const inputs = suggestions.filter((s) => s.kind === 'input')
  const own = suggestions.filter((s) => s.kind === 'var' || s.kind === 'secret')
  if (earlier.length === 0 && inputs.length === 0 && own.length === 0) {
    return (
      <p className="hint stack-available-none">
        Nothing to take from yet: a launch in an earlier stage hands on its addresses,
        names and parameters to this one, and the stack's own variables and secrets are
        set under <strong>Variables &amp; secrets</strong> at the top.
      </p>
    )
  }
  return (
    <div className="stack-available">
      <div className="stack-available-head">
        <strong>Available to this step</strong>
        <span className="faint">
          {active ? <>click inserts into <span className="mono">{active}</span></>
            : 'drag onto a box, or click a box first'}
        </span>
      </div>
      <p className="hint">
        Filled in when this step starts, from what the earlier launches actually made.
        Modules also see each as <span className="mono">$LEMONDX_STACK_{'<ID>'}_IPS</span>,
        _NAMES, _IPV6 and _NODES, space-separated.
      </p>
      {own.length > 0 && (
        <div className="stack-available-group">
          <div className="stack-available-source">
            <span>This stack</span>
            <span className="faint">set under Variables &amp; secrets</span>
          </div>
          <PortGroups suggestions={own} stage={-1} clickable={active !== null}
            onClick={onInsert} />
        </div>
      )}
      {earlier.map(({ step, stage }) => {
        const handed = suggestions.filter((s) => s.source === step.id)
        // In a swarm, the stack's steps and the swarm's both count stages
        // from 1; say whose.
        const whose = flowCtx.swarm ? (flowCtx.base.some((b) => b.step.key === step.key)
          ? 'running stack, ' : 'swarm, ') : ''
        return (
          <div className="stack-available-group" key={step.key}>
            <div className="stack-available-source">
              <span className="mono">{step.id}</span>
              <span className="faint">
                {step.template || 'no template'} · {whose}stage {stage + 1}
              </span>
            </div>
            <PortGroups suggestions={handed} stage={stage} clickable={active !== null}
              onClick={onInsert} />
          </div>
        )
      })}
      {inputs.length > 0 && (
        <div className="stack-available-group">
          <div className="stack-available-source">
            <span>Entered at launch</span>
          </div>
          <div className="stack-step-ports">
            {inputs.map((s) => (
              <RefChip key={s.ref} suggestion={s} stage={-1} clickable={active !== null}
                onClick={() => onInsert(s.ref)} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
