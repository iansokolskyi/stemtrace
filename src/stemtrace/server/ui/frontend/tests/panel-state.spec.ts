import { expect, test } from '@playwright/test'

import {
  formatPanel,
  PANEL_KINDS,
  type PanelEntry,
  parsePanel,
} from '../src/components/triage/panelState'

/** The triage panel's URL value. No browser: both functions are pure. */

const TRICKY_IDS = [
  'plain',
  'a/b',
  'a:b',
  '100%',
  'a/b:c%d',
  '%2F',
  'x y&z=1',
  'celery@host-1',
  'é',
]

test.describe('panel URL state', () => {
  for (const kind of PANEL_KINDS) {
    for (const id of TRICKY_IDS) {
      test(`round-trips ${kind} with id ${JSON.stringify(id)}`, () => {
        const entries: PanelEntry[] = [{ kind, id }]
        expect(parsePanel(formatPanel(entries))).toEqual(entries)
      })
    }
  }

  test('round-trips a stack of every kind with the tricky ids in order', () => {
    const entries = PANEL_KINDS.map((kind, i) => ({ kind, id: TRICKY_IDS[i + 3] }))
    expect(parsePanel(formatPanel(entries))).toEqual(entries)
  })

  test('writes kind:id entries joined by a slash, ids encoded', () => {
    expect(
      formatPanel([
        { kind: 'exception', id: 'Meili/Timeout' },
        { kind: 'task', id: '3f2a:1' },
      ]),
    ).toBe('exception:Meili%2FTimeout/task:3f2a%3A1')
  })

  test('an empty stack has no value', () => {
    expect(formatPanel([])).toBeUndefined()
    expect(parsePanel(undefined)).toEqual([])
    expect(parsePanel('')).toEqual([])
  })

  test('drops entries with an unknown kind and keeps the others in order', () => {
    expect(parsePanel('bogus:1/runs:a/worker:b')).toEqual([
      { kind: 'runs', id: 'a' },
      { kind: 'worker', id: 'b' },
    ])
  })

  test('drops entries with no colon, an empty id or an id that does not decode', () => {
    expect(parsePanel('runs/runs:/runs:%E0%A4%A/task:ok')).toEqual([{ kind: 'task', id: 'ok' }])
  })

  test('a stray colon inside an id stays part of it', () => {
    expect(parsePanel('task:a:b')).toEqual([{ kind: 'task', id: 'a:b' }])
  })

  test('formatting skips an entry with an empty id', () => {
    expect(formatPanel([{ kind: 'runs', id: '' }])).toBeUndefined()
  })
})
