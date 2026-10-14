import { useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import type { NodeGroup } from '../lib/types'
import { CloseIcon } from './Icons'

export interface Targets {
  groups: string[]
  nodes: string[]
}

interface Props {
  id: string
  /** Offered first: what a task is meant to be placed on. */
  groups: NodeGroup[]
  /** Member names, offered after the groups. */
  nodes: string[]
  value: Targets
  onChange: (next: Targets) => void
  disabled?: boolean
  placeholder?: string
  /** One node and nothing else: a choice of where, not a placement. */
  single?: boolean
  invalid?: boolean
}

type Choice = { kind: 'group' | 'node'; name: string; meaning: string }

/**
 * A combobox for node groups and nodes, typed into and completed.
 *
 * Groups lead because placement is a group's business -- a node can join a
 * group when the one it has is gone, and a task naming the node cannot follow
 * -- so a node is offered only after them, and only as an addition.
 */
export function TargetPicker({ id, groups, nodes, value, onChange, disabled, placeholder,
  single, invalid }: Props) {
  const [text, setText] = useState('')
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const field = useRef<HTMLInputElement>(null)

  const choices = useMemo((): Choice[] => {
    const wanted = text.trim().toLowerCase()
    const matches = (name: string) => !wanted || name.toLowerCase().includes(wanted)
    const groupChoices = single ? [] : groups
      .filter((g) => !value.groups.includes(g.name) && matches(g.name))
      .map((g): Choice => ({
        kind: 'group', name: g.name,
        meaning: g.members.length
          ? `${g.members.length} node${g.members.length === 1 ? '' : 's'}: ${g.members.join(', ')}`
          : 'no members yet',
      }))
    const nodeChoices = nodes
      .filter((n) => !value.nodes.includes(n) && matches(n))
      .map((n): Choice => ({
        kind: 'node', name: n,
        meaning: single ? 'node' : 'single node, beside the groups',
      }))
    // Prefix matches first: typing "pr" wants prdev1 before a group named "sprint".
    const rank = (c: Choice) => (wanted && c.name.toLowerCase().startsWith(wanted) ? 0 : 1)
    return [...groupChoices.sort((a, b) => rank(a) - rank(b)),
      ...nodeChoices.sort((a, b) => rank(a) - rank(b))]
  }, [text, groups, nodes, value, single])

  function add(choice: Choice) {
    if (choice.kind === 'group') {
      onChange({ ...value, groups: [...value.groups, choice.name] })
    } else {
      onChange({ ...value, nodes: single ? [choice.name] : [...value.nodes, choice.name] })
    }
    setText('')
    setHighlight(0)
    if (single) setOpen(false)
  }

  function remove(kind: 'group' | 'node', name: string) {
    onChange(kind === 'group'
      ? { ...value, groups: value.groups.filter((g) => g !== name) }
      : { ...value, nodes: value.nodes.filter((n) => n !== name) })
    field.current?.focus()
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setOpen(true)
      const step = event.key === 'ArrowDown' ? 1 : -1
      setHighlight((h) => (choices.length ? (h + step + choices.length) % choices.length : 0))
    } else if ((event.key === 'Enter' || event.key === 'Tab') && open && choices[highlight]
               && (text || event.key === 'Enter')) {
      event.preventDefault()
      add(choices[highlight])
    } else if (event.key === 'Escape') {
      setOpen(false)
    } else if (event.key === 'Backspace' && !text) {
      const lastNode = value.nodes[value.nodes.length - 1]
      const lastGroup = value.groups[value.groups.length - 1]
      if (lastNode) remove('node', lastNode)
      else if (lastGroup) remove('group', lastGroup)
    }
  }

  const known = new Set(groups.map((g) => g.name))
  const shown = choices.slice(0, 10)
  return (
    <div className={`target-picker${disabled ? ' is-disabled' : ''}`}
      aria-invalid={invalid || undefined} onClick={() => field.current?.focus()}>
      {value.groups.map((g) => (
        <span key={`g-${g}`} className={`target-chip${known.has(g) ? '' : ' is-unknown'}`}
          title={known.has(g) ? `group ${g}` : `no group called ${g} here`}>
          <span className="faint">group</span> {g}
          <button type="button" aria-label={`Remove group ${g}`} disabled={disabled}
            onClick={(e) => { e.stopPropagation(); remove('group', g) }}>
            <CloseIcon size={11} />
          </button>
        </span>
      ))}
      {value.nodes.map((n) => (
        <span key={`n-${n}`} className="target-chip is-node" title={`node ${n}`}>
          {!single && <span className="faint">node</span>} {n}
          <button type="button" aria-label={`Remove node ${n}`} disabled={disabled}
            onClick={(e) => { e.stopPropagation(); remove('node', n) }}>
            <CloseIcon size={11} />
          </button>
        </span>
      ))}
      <div className="stack-ref-field target-input">
        <input
          id={id}
          ref={field}
          className="input mono"
          value={text}
          disabled={disabled}
          placeholder={value.groups.length || value.nodes.length ? '' : placeholder}
          autoComplete="off"
          spellCheck={false}
          role="combobox"
          aria-expanded={open && shown.length > 0}
          aria-controls={open && shown.length > 0 ? `${id}-choices` : undefined}
          onChange={(e) => { setText(e.target.value); setOpen(true); setHighlight(0) }}
          onFocus={() => setOpen(true)}
          // A click on a choice blurs the input first; let it land.
          onBlur={() => window.setTimeout(() => setOpen(false), 120)}
          onKeyDown={onKeyDown}
        />
        {open && shown.length > 0 && (
          <ul className="stack-ref-menu" id={`${id}-choices`} role="listbox">
            {shown.map((choice, index) => (
              <li key={`${choice.kind}-${choice.name}`}>
                <button type="button" role="option" aria-selected={index === highlight}
                  className={index === highlight ? 'is-on' : undefined}
                  onMouseEnter={() => setHighlight(index)}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => add(choice)}>
                  <span>
                    {!single && <span className="faint">{choice.kind} </span>}
                    <span className="mono">{choice.name}</span>
                  </span>
                  <span className="faint truncate">{choice.meaning}</span>
                </button>
              </li>
            ))}
            <li className="stack-ref-menu-hint">↑↓ to pick · Enter to add · Esc to close</li>
          </ul>
        )}
      </div>
    </div>
  )
}
