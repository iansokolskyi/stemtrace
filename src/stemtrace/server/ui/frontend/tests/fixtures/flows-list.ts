import { createSuccessTask, type MockFlowSummary, type MockTask } from './mock-data'

/** The clock the Flows list fixtures are written against. */
export const FLOWS_NOW = '2026-10-17T12:00:00Z'

/** What the list says for each fixture flow, in list order (the rows' text, top to bottom). */
export interface ExpectedFlowRow {
  taskId: string
  state: string
  stateWord: string
  title: string
  /** Empty when the row has no summary line */
  summary: string
  rootName: string
  age: string
}

interface FlowFixture {
  row: ExpectedFlowRow
  rootState: string
  flow: MockFlowSummary | null
  secondsAgo: number
}

const FIXTURES: FlowFixture[] = [
  {
    // The root itself succeeded; the flow did not: 2 failed and 1 rejected header, callback not run
    row: {
      taskId: 'flow-chord-0001',
      state: 'FAILURE',
      stateWord: 'Failure',
      title: 'close_month',
      summary: '50 tasks, 3 failed, chord callback not run',
      rootName: 'billing.invoices.close_month',
      age: '5m ago',
    },
    rootState: 'SUCCESS',
    flow: {
      task_count: 50,
      state_counts: { SUCCESS: 47, FAILURE: 2, REJECTED: 1 },
      not_run_count: 1,
      state: 'FAILURE',
    },
    secondsAgo: 5 * 60 + 10,
  },
  {
    row: {
      taskId: 'flow-chain-0002',
      state: 'SUCCESS',
      stateWord: 'Success',
      title: 'build_digest',
      summary: '4 tasks',
      rootName: 'reports.digest.build_digest',
      age: '2h ago',
    },
    rootState: 'SUCCESS',
    flow: { task_count: 4, state_counts: { SUCCESS: 4 }, not_run_count: 0, state: 'SUCCESS' },
    secondsAgo: 2 * 3600 + 60,
  },
  {
    row: {
      taskId: 'flow-group-0003',
      state: 'STARTED',
      stateWord: 'Running',
      title: 'rebuild_all',
      summary: '11 tasks',
      rootName: 'search.reindex.rebuild_all',
      age: 'just now',
    },
    rootState: 'STARTED',
    flow: {
      task_count: 11,
      state_counts: { SUCCESS: 8, STARTED: 3 },
      not_run_count: 0,
      state: 'STARTED',
    },
    secondsAgo: 20,
  },
  {
    // A server that has no summary for this flow: the chip falls back to the root's own state
    row: {
      taskId: 'flow-null-0004',
      state: 'RETRY',
      stateWord: 'Retry',
      title: 'sync_ledger',
      summary: '',
      rootName: 'billing.ledger.sync_ledger',
      age: '1h ago',
    },
    rootState: 'RETRY',
    flow: null,
    secondsAgo: 90 * 60,
  },
]

/** The rows the fixture list shows, in order. */
export const FLOW_ROWS: ExpectedFlowRow[] = FIXTURES.map((fixture) => fixture.row)

/** The root tasks of the fixture flows, newest first, against the clock `FLOWS_NOW`. */
export function flowListRoots(): MockTask[] {
  return FIXTURES.map(({ row, rootState, flow, secondsAgo }) => {
    const task = createSuccessTask(row.rootName)
    const firstSeen = new Date(Date.parse(FLOWS_NOW) - secondsAgo * 1000).toISOString()
    return Object.assign(task, {
      task_id: row.taskId,
      root_id: row.taskId,
      state: rootState,
      first_seen: firstSeen,
      last_updated: firstSeen,
      flow,
    })
  })
}
