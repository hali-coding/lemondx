import { useMemo, useRef, useState } from 'react'
import type { DragEvent, ReactNode } from 'react'
import { api, ApiError } from '../lib/api'
import type {
  BootstrapModule, ClusterNode, InstanceTemplate, NodeGroup, Stack, StackLaunchStep,
  StackStep, StackStepType, Synced,
} from '../lib/types'
import { ClockIcon, CloseIcon, GripIcon, HeartIcon, PlusIcon, ServerIcon, TrashIcon } from './Icons'

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
  onSaved: (stack: Synced<Stack>) => void
}

/** A step as the editor holds it: `key` is React's and the drag's, never saved. */
type Item = StackStep & { key: string }
type Stage = { key: string; steps: Item[] }

/** What is being dragged: a new step from the palette, or one already placed. */
type Drag = { kind: 'new'; step: StackStep } | { kind: 'move'; key: string }
/** Where it would land: a new stage before `index`, or beside the steps of one. */
type Target = { kind: 'gap'; index: number } | { kind: 'stage'; index: number }

const FIELDS = ['names', 'name', 'ips', 'ip', 'ipv6', 'nodes', 'count'] as const
const PLACEHOLDER = /\{\{\s*([^{}]*?)\s*\}\}/g
const STEP_ID = /^[a-z][a-z0-9_]{0,31}$/
const INPUT_REF = /^params\.[A-Za-z_][A-Za-z0-9_]{0,63}$/
const MAX_COUNT = 20

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

/** One thing a value may refer to: an earlier launch's field, or a launch input. */
type Suggestion = { ref: string; label: string; meaning: string; kind: 'step' | 'input' }

/**
 * Everything `{{…}}` may name at this point in the stack: each earlier launch
 * crossed with the fields it exposes, then the values entered at launch. The
 * order is the order they are offered in, so the most specific — this stack's
 * own earlier steps — comes first.
 */
function suggestionsFor(earlier: (Item & StackLaunchStep)[], inputs: string[]): Suggestion[] {
  const found: Suggestion[] = []
  for (const source of earlier) {
    for (const field of FIELDS) {
      found.push({ ref: `${source.id}.${field}`, label: `${source.id}.${field}`,
        meaning: FIELD_MEANING[field], kind: 'step' })
    }
  }
  for (const name of inputs) {
    found.push({ ref: `params.${name}`, label: `params.${name}`,
      meaning: 'entered when the stack is launched', kind: 'input' })
  }
  return found
}

/**
 * A value field that completes `{{…}}` as you type.
 *
 * The references are the one part of a stack you cannot get right from memory:
 * they name another step and a field of it, and a typo is only found on save.
 * So the list is offered at the caret the moment `{{` is typed, filtered by
 * whatever follows, and accepting one writes the closing braces too.
 */
function ReferenceInput({ value, onChange, suggestions, id, placeholder, invalid, secret }: {
  value: string
  onChange: (next: string) => void
  suggestions: Suggestion[]
  id?: string
  placeholder?: string
  invalid?: boolean
  secret?: boolean
}) {
  const field = useRef<HTMLInputElement | null>(null)
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  // Where the `{{` being completed starts, and what has been typed since.
  const [partial, setPartial] = useState<{ at: number; text: string } | null>(null)

  /** The `{{` the caret sits inside, if any: what decides the list is shown. */
  function openingAt(text: string, caret: number) {
    const before = text.slice(0, caret)
    const start = before.lastIndexOf('{{')
    if (start === -1) return null
    const typed = before.slice(start + 2)
    // Already closed, or running into another placeholder: not this one.
    if (typed.includes('}}') || typed.includes('{{')) return null
    return { at: start, text: typed.trim() }
  }

  const matches = partial === null ? [] : suggestions.filter((s) => {
    const needle = partial.text.toLowerCase()
    return !needle || s.ref.toLowerCase().includes(needle)
  })

  function sync(next: string, caret: number) {
    const found = openingAt(next, caret)
    setPartial(found)
    setOpen(found !== null)
    setHighlight(0)
  }

  function accept(choice: Suggestion) {
    if (!partial) return
    const input = field.current
    const caret = input?.selectionStart ?? value.length
    const next = `${value.slice(0, partial.at)}{{${choice.ref}}}${value.slice(caret)}`
    onChange(next)
    setOpen(false)
    setPartial(null)
    // Put the caret after the closing braces, so typing carries on after it.
    const end = partial.at + choice.ref.length + 4
    requestAnimationFrame(() => {
      input?.focus()
      input?.setSelectionRange(end, end)
    })
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (!open || matches.length === 0) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setHighlight((current) => (current + (event.key === 'ArrowDown' ? 1 : matches.length - 1))
        % matches.length)
    } else if (event.key === 'Enter' || event.key === 'Tab') {
      event.preventDefault()
      accept(matches[highlight])
    } else if (event.key === 'Escape') {
      event.preventDefault()
      setOpen(false)
    }
  }

  return (
    <div className="stack-ref-field">
      <input
        id={id}
        ref={field}
        className="input mono"
        value={value}
        placeholder={placeholder}
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
        onFocus={(event) => sync(value, event.target.selectionStart ?? value.length)}
        // A click on a suggestion blurs the input first; let it land.
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
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
                <span className="mono">{choice.label}</span>
                <span className="faint">{choice.meaning}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
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

function blank(type: StackStepType, template?: InstanceTemplate): StackStep {
  if (type === 'sleep') return { id: 'sleep', type, seconds: 30 }
  if (type === 'wait_healthy') return { id: 'healthy', type, timeout_seconds: 900 }
  return {
    id: template ? slugId(template.name_prefix || template.name) : 'launch',
    type: 'launch', template: template?.name ?? '', count: 1, prefix: '',
    wait_bootstrap: true, params: {}, nodes: [], groups: [],
  }
}

/** Everything wrong with the stack as drawn, keyed by step (`''` for the whole). */
function problems(stages: Stage[], templates: InstanceTemplate[]) {
  const found: Record<string, string[]> = {}
  const add = (key: string, message: string) => { (found[key] ??= []).push(message) }
  const ids = new Map<string, number>()
  stages.flatMap((s) => s.steps).forEach((step) => ids.set(step.id, (ids.get(step.id) ?? 0) + 1))
  const earlier = new Set<string>()
  if (stages.length === 0) add('', 'Drag a step onto the canvas to start.')
  for (const stage of stages) {
    const launched = new Set<string>()
    const templatesHere = new Set<string>()
    for (const step of stage.steps) {
      if (!STEP_ID.test(step.id)) add(step.key, 'The id must be lowercase letters, digits and _.')
      if (step.id === 'params') add(step.key, '“params” is reserved for values entered at launch.')
      if ((ids.get(step.id) ?? 0) > 1) add(step.key, `Another step is also called “${step.id}”.`)
      if (step.type === 'wait_healthy' && earlier.size === 0) {
        add(step.key, 'Nothing is launched before this, so there is nothing to wait for.')
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
      for (const [name, value] of Object.entries(step.params)) {
        for (const match of value.matchAll(PLACEHOLDER)) {
          if (INPUT_REF.test(match[1])) continue
          const [ref, field] = match[1].split('.')
          if (!earlier.has(ref) || !(FIELDS as readonly string[]).includes(field ?? '')) {
            add(step.key, `${name}: {{${match[1]}}} does not name a launch in an earlier stage.`)
          }
        }
      }
    }
    launched.forEach((id) => earlier.add(id))
  }
  return found
}

/** Designs a stack: stages run top to bottom, steps side by side run in parallel. */
export function StackEditor({ stack, templates, modules, nodes, groups, running = 0,
                              onCancel, onSaved }: Props) {
  const [name, setName] = useState(stack?.name ?? '')
  const [description, setDescription] = useState(stack?.description ?? '')
  const [stages, setStages] = useState<Stage[]>(() => (stack?.stages ?? []).map((stage) => ({
    key: newKey(), steps: stage.steps.map((step) => ({ ...step, key: newKey() })),
  })))
  const [selected, setSelected] = useState<string | null>(null)
  const [target, setTarget] = useState<Target | null>(null)
  const [saving, setSaving] = useState(false)
  const renaming = Boolean(stack && running > 0 && name.trim() && name.trim() !== stack.name)
  const [error, setError] = useState<string | null>(null)
  // dataTransfer cannot be read during dragover, which is when the drop
  // target needs to know what is coming; so the drag lives here instead.
  const drag = useRef<Drag | null>(null)

  const issues = useMemo(() => problems(stages, templates), [stages, templates])
  const flat = stages.flatMap((s) => s.steps)
  const current = flat.find((s) => s.key === selected) ?? null
  const stageOf = (key: string) => stages.findIndex((s) => s.steps.some((step) => step.key === key))
  const canSave = !!name.trim() && Object.keys(issues).length === 0 && !saving

  function withId(step: StackStep): Item {
    const taken = new Set(flat.map((s) => s.id))
    return { ...step, id: uniqueId(step.id, taken), key: newKey() }
  }

  /** Put a step at a target, taking it out of wherever it was first. */
  function place(item: Item, where: Target) {
    setStages((current) => {
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
    setSelected(item.key)
  }

  function add(step: StackStep) {
    place(withId(step), { kind: 'gap', index: stages.length })
  }

  function update(key: string, change: Partial<StackStep>) {
    setStages((current) => current.map((stage) => ({
      ...stage,
      steps: stage.steps.map((step) => (step.key === key ? { ...step, ...change } as Item : step)),
    })))
  }

  function remove(key: string) {
    setStages((current) => current
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

  async function save() {
    if (!canSave) return
    setSaving(true)
    setError(null)
    try {
      const saved = await api.saveStack(name.trim(), {
        description,
        stages: stages.map((stage) => ({
          steps: stage.steps.map(({ key: _key, ...step }) => step as StackStep),
        })),
      })
      // Renamed: the old name would otherwise linger as a second stack.
      if (stack && stack.name !== saved.name) await api.deleteStack(stack.name).catch(() => {})
      onSaved(saved)
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
    <div className="stack-editor" role="dialog" aria-modal="true"
      aria-label={stack ? `Edit stack ${stack.name}` : 'New stack'}>
      <header className="stack-editor-head">
        <div className="stack-editor-names">
          <input className="input stack-editor-title" placeholder="Stack name" value={name}
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
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
          <button type="button" className="btn btn-primary" disabled={!canSave} onClick={save}
            title={!name.trim() ? 'Give the stack a name'
              : Object.keys(issues).length ? 'Fix the steps marked in red first' : undefined}>
            {saving && <span className="spinner" />} Save stack
          </button>
        </div>
      </header>

      <div className="stack-editor-body">
        <aside className="stack-palette" aria-label="Steps to add">
          <h4>Steps</h4>
          <p className="hint">Drag onto the canvas, or click to add at the end.</p>
          <PaletteItem icon={<ServerIcon />} label="Launch a template"
            detail="one or more instances"
            onDragStart={(e) => onDragStart(e, { kind: 'new', step: blank('launch') })}
            onDragEnd={endDrag} onAdd={() => add(blank('launch'))} />
          <PaletteItem icon={<HeartIcon />} label="Wait until healthy"
            detail="everything launched so far"
            onDragStart={(e) => onDragStart(e, { kind: 'new', step: blank('wait_healthy') })}
            onDragEnd={endDrag} onAdd={() => add(blank('wait_healthy'))} />
          <PaletteItem icon={<ClockIcon />} label="Sleep" detail="a fixed pause"
            onDragStart={(e) => onDragStart(e, { kind: 'new', step: blank('sleep') })}
            onDragEnd={endDrag} onAdd={() => add(blank('sleep'))} />

          <h4>Templates</h4>
          {templates.length === 0 && <p className="hint">No templates yet; make one first.</p>}
          {templates.map((template) => (
            <PaletteItem key={template.name} icon={<ServerIcon />} label={template.name}
              detail={template.image} mono
              onDragStart={(e) => onDragStart(e, { kind: 'new', step: blank('launch', template) })}
              onDragEnd={endDrag} onAdd={() => add(blank('launch', template))} />
          ))}
        </aside>

        <main className="stack-canvas" onClick={() => setSelected(null)}
          onDragOver={(event) => over(event, { kind: 'gap', index: stages.length })}
          onDrop={(event) => drop(event, { kind: 'gap', index: stages.length })}>
          <div className="stack-flow">
            <div className="stack-terminal">Start</div>
            {gap(0)}
            {stages.map((stage, index) => (
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
                    {stage.steps.map((step) => (
                      <StepCard key={step.key} step={step} selected={step.key === selected}
                        issues={issues[step.key] ?? []} templates={templates}
                        onSelect={() => setSelected(step.key)}
                        onRemove={() => remove(step.key)}
                        onDragStart={(e) => onDragStart(e, { kind: 'move', key: step.key })}
                        onDragEnd={endDrag} />
                    ))}
                    <div className="stack-stage-drop" aria-hidden="true">
                      <PlusIcon size={13} /> in parallel
                    </div>
                  </div>
                </div>
                {gap(index + 1)}
              </div>
            ))}
            <div className="stack-terminal">Done</div>
            {stages.length === 0 && (
              <p className="stack-canvas-empty">
                Drag a template from the left to launch it. Steps in the same stage run
                side by side; each stage starts when the one above has finished.
              </p>
            )}
          </div>
        </main>

        <aside className="stack-inspector" aria-label="Step settings">
          {current ? (
            <Inspector key={current.key} step={current} stageIndex={stageOf(current.key)}
              stages={stages} templates={templates} modules={modules}
              nodes={nodes} groups={groups} issues={issues[current.key] ?? []}
              onChange={(change) => update(current.key, change)}
              onMove={(delta) => {
                const index = stageOf(current.key)
                const alone = stages[index].steps.length === 1
                // Up joins the stage above; down joins the stage below, or
                // becomes a stage of its own at the end.
                if (delta < 0 && index > 0) place(current, { kind: 'stage', index: index - 1 })
                else if (delta > 0 && index < stages.length - 1) {
                  place(current, { kind: 'stage', index: index + 1 })
                } else if (delta > 0 && !alone) {
                  place(current, { kind: 'gap', index: stages.length })
                }
              }}
              onSplit={() => place(current, { kind: 'gap', index: stageOf(current.key) + 1 })}
              onRemove={() => remove(current.key)} />
          ) : (
            <div className="stack-inspector-empty">
              <h4>How a stack runs</h4>
              <ol>
                <li>Stages run top to bottom; the steps inside one run at the same time.</li>
                <li>A launch waits for its bootstrap modules unless you turn that off, so the
                  next stage can rely on what it installed.</li>
                <li>A later launch can use what an earlier one made: <span className="mono">
                  {'{{db.ip}}'}</span> in a parameter, or <span className="mono">
                  $LEMONDX_STACK_DB_IPS</span> in any module.</li>
                <li>If a step fails, no further stage starts. Instances already launched stay.</li>
              </ol>
              {issues[''] && <p className="hint">{issues[''][0]}</p>}
            </div>
          )}
        </aside>
      </div>
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
  return step.template || 'Launch'
}

function StepCard({ step, selected, issues, templates, onSelect, onRemove, onDragStart,
                    onDragEnd }: {
  step: Item
  selected: boolean
  issues: string[]
  templates: InstanceTemplate[]
  onSelect: () => void
  onRemove: () => void
  onDragStart: (event: DragEvent) => void
  onDragEnd: () => void
}) {
  const template = step.type === 'launch' ? templates.find((t) => t.name === step.template) : null
  const icon = step.type === 'sleep' ? <ClockIcon size={14} />
    : step.type === 'wait_healthy' ? <HeartIcon size={14} /> : <ServerIcon size={14} />
  return (
    <div className={`stack-step stack-step-${step.type}${selected ? ' is-selected' : ''}${
      issues.length ? ' has-issues' : ''}`}
      draggable onDragStart={onDragStart} onDragEnd={onDragEnd}
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
            {!step.wait_bootstrap && <span className="badge badge-warn">no wait</span>}
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
      </div>
      {step.type === 'launch' && template && (
        <div className="stack-step-sub mono truncate">
          {(step.prefix || template.name_prefix)}-N · {template.image}
        </div>
      )}
      {issues.length > 0 && <div className="stack-step-issue">{issues[0]}</div>}
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
    : 'Wait until healthy'
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
        <span className="hint">What later steps call it, as in {`{{${step.id}.ip}}`}.</span>
      </div>

      {step.type === 'launch' && (
        <LaunchSettings step={step} stageIndex={stageIndex} stages={stages}
          templates={templates} modules={modules} nodes={nodes} groups={groups}
          onChange={onChange} />
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
  const earlier = stages.slice(0, stageIndex).flatMap((s) => s.steps)
    .filter((s): s is Item & StackLaunchStep => s.type === 'launch')
  // Secrets any launch in the stack asks for: the obvious things to hand on,
  // since one password typed at launch is usually wanted by several steps.
  const entered = [...new Set(stages.flatMap((s) => s.steps).flatMap((s) => (
    s.type === 'launch'
      ? modules.filter((m) => templates.find((t) => t.name === s.template)?.bootstrap.modules
        .includes(m.id)).flatMap((m) => m.params).filter((p) => p.secret).map((p) => p.name)
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
          <input id="step-count" className="input num" type="number" min={1} max={MAX_COUNT}
            value={step.count}
            onChange={(event) => onChange({
              count: Math.min(MAX_COUNT, Math.max(1, Number(event.target.value) || 1)) })} />
        </div>
        <div className="field">
          <label htmlFor="step-prefix">Name prefix</label>
          <input id="step-prefix" className="input mono" value={step.prefix}
            placeholder={template?.name_prefix ?? ''}
            onChange={(event) => onChange({ prefix: event.target.value.trim() })} />
        </div>
      </div>

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

      <LaunchParams step={step} template={template} modules={modules}
        earlier={earlier} entered={entered} onChange={onChange} />
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
function LaunchParams({ step, template, modules, earlier, entered, onChange }: {
  step: StackLaunchStep
  template?: InstanceTemplate
  modules: BootstrapModule[]
  earlier: (Item & StackLaunchStep)[]
  entered: string[]
  onChange: (change: Partial<StackLaunchStep>) => void
}) {
  const used = modules.filter((m) => template?.bootstrap.modules.includes(m.id))
  const declared = new Set(used.flatMap((m) => m.params).map((p) => p.name))
  // Names offered to `{{params.…}}`: every secret in the stack, plus whatever
  // this step already refers to, so a second step reuses the same input.
  const inputs = [...new Set([...entered, ...Object.values(step.params)
    .flatMap((value) => [...value.matchAll(PLACEHOLDER)].map((m) => m[1]))
    .filter((ref) => INPUT_REF.test(ref)).map((ref) => ref.slice('params.'.length))])].sort()
  const suggestions = suggestionsFor(earlier, inputs)
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
        template, or the module's own default. Type <span className="mono">{'{{'}</span> in any
        value to refer to an earlier launch or to something entered at launch.
      </span>

      {!template && <p className="hint">Pick a template to see its parameters.</p>}

      {used.map((module) => (
        <div className="stack-param-group" key={module.id}>
          <h5>
            {module.name}
            <span className="faint">{module.params.length} parameter(s)</span>
          </h5>
          {module.params.length === 0 && (
            <p className="hint">This module declares none.</p>
          )}
          {module.params.map((param) => {
            const overridden = param.name in step.params
            const value = step.params[param.name] ?? ''
            // What it would be without an override: the template's saved answer,
            // else the module's own effective default. A secret has neither --
            // it is never stored anywhere, so it is asked for at launch.
            const inherited = template?.bootstrap.params[param.name] ?? param.value ?? param.default
            const refs = [...value.matchAll(PLACEHOLDER)].map((m) => m[1])
            const literal = value.replace(PLACEHOLDER, '').trim()
            const badRef = refs.find((ref) => !INPUT_REF.test(ref) && !referenceOk(ref, earlier))
            const secretLiteral = param.secret && overridden
              && (literal !== '' || refs.some((ref) => !INPUT_REF.test(ref)))
            return (
              <div className={`stack-param-row${overridden ? ' is-set' : ''}`} key={param.name}>
                <div className="stack-param-head">
                  <span className="mono stack-param-name">{param.name}</span>
                  {overridden && <span className="badge badge-dim">set here</span>}
                </div>
                <ReferenceInput
                  id={`param-${step.id}-${param.name}`}
                  value={value}
                  secret={param.secret}
                  invalid={Boolean(badRef) || secretLiteral}
                  placeholder={param.secret
                    ? 'asked for when the stack is launched'
                    : inherited || 'no default'}
                  suggestions={suggestions}
                  onChange={(next) => (next === '' ? clear(param.name) : set(param.name, next))}
                />
                {param.description && <span className="hint">{param.description}</span>}
                {!overridden && !param.secret && (
                  <span className="hint faint">
                    {template?.bootstrap.params[param.name] !== undefined
                      ? 'from the template'
                      : 'the module’s default'}
                  </span>
                )}
                {param.secret && !overridden && (
                  <span className="hint faint">
                    Secret: never saved. Left alone it is asked for at launch; give it{' '}
                    <span className="mono">{'{{params.NAME}}'}</span> to share one value
                    between steps.
                  </span>
                )}
                {secretLiteral && (
                  <span className="field-error">
                    A secret cannot be saved in a stack. Use {'{{params.NAME}}'} so the value
                    is entered at launch instead.
                  </span>
                )}
                {badRef && (
                  <span className="field-error">
                    {'{{'}{badRef}{'}}'} does not name a launch in an earlier stage.
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
              onChange={(next) => set(name, next)}
            />
          </div>
        ))}
        <button type="button" className="btn btn-sm stack-param-add"
          onClick={() => set(`PARAM_${extras.length + 1}`, '')}>
          <PlusIcon size={13} /> Add a parameter
        </button>
      </div>

      {earlier.length > 0 && (
        <span className="hint">
          Every module of this launch also sees{' '}
          <span className="mono">$LEMONDX_STACK_{earlier[0].id.toUpperCase()}_IPS</span>{' '}
          and the matching _NAMES, _IPV6 and _NODES, space-separated — for a module
          written to loop over them rather than take one value.
        </span>
      )}
    </div>
  )
}

/** Whether `{{ref}}` names a launch in an earlier stage and a field it has. */
function referenceOk(ref: string, earlier: (Item & StackLaunchStep)[]) {
  const [id, field, ...rest] = ref.split('.')
  return rest.length === 0 && earlier.some((s) => s.id === id)
    && (FIELDS as readonly string[]).includes(field ?? '')
}
