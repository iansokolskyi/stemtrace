/**
 * Mock data for Playwright E2E tests.
 *
 * This allows tests to run without Docker by mocking API responses.
 */

export interface MockTask {
  task_id: string
  name: string
  state: string
  parent_id: string | null
  root_id: string
  node_type: 'TASK' | 'GROUP' | 'CHORD'
  group_id: string | null
  chord_id: string | null
  events: MockEvent[]
  children: string[]
  first_seen: string | null
  last_updated: string | null
  duration_ms: number | null
}

export interface MockEvent {
  task_id: string
  name: string
  state: string
  timestamp: string
  parent_id: string | null
  root_id: string
  group_id: string | null
  args: unknown[] | null
  kwargs: Record<string, unknown> | null
  result: unknown | null
  exception: string | null
  traceback: string | null
  retry_count: number
}

/**
 * Generate a UUID-like task ID
 */
export function generateTaskId(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

/**
 * Create a mock event for a task
 */
export function createMockEvent(
  task_id: string,
  name: string,
  state: string,
  options: Partial<MockEvent> = {},
): MockEvent {
  return {
    task_id,
    name,
    state,
    timestamp: new Date().toISOString(),
    parent_id: null,
    root_id: task_id,
    group_id: null,
    args: null,
    kwargs: null,
    result: null,
    exception: null,
    traceback: null,
    retry_count: 0,
    ...options,
  }
}

/**
 * Create a simple successful task
 */
export function createSuccessTask(name: string, result: unknown = null): MockTask {
  const task_id = generateTaskId()
  const now = new Date()
  const startTime = new Date(now.getTime() - 1000) // 1 second ago
  return {
    task_id,
    name,
    state: 'SUCCESS',
    parent_id: null,
    root_id: task_id,
    node_type: 'TASK',
    group_id: null,
    chord_id: null,
    children: [],
    first_seen: startTime.toISOString(),
    last_updated: now.toISOString(),
    duration_ms: 1000,
    events: [
      createMockEvent(task_id, name, 'PENDING', { args: [1, 2] }),
      createMockEvent(task_id, name, 'STARTED'),
      createMockEvent(task_id, name, 'SUCCESS', { result }),
    ],
  }
}

/**
 * Create a failed task with exception
 */
export function createFailedTask(name: string, exception: string, traceback: string): MockTask {
  const task_id = generateTaskId()
  const now = new Date()
  const startTime = new Date(now.getTime() - 500)
  return {
    task_id,
    name,
    state: 'FAILURE',
    parent_id: null,
    root_id: task_id,
    node_type: 'TASK',
    group_id: null,
    chord_id: null,
    children: [],
    first_seen: startTime.toISOString(),
    last_updated: now.toISOString(),
    duration_ms: 500,
    events: [
      createMockEvent(task_id, name, 'PENDING'),
      createMockEvent(task_id, name, 'STARTED'),
      createMockEvent(task_id, name, 'FAILURE', { exception, traceback }),
    ],
  }
}

/**
 * Create a task chain: parent -> child -> grandchild
 */
export function createChain(baseName: string, length: number): MockTask[] {
  const tasks: MockTask[] = []
  let parent_id: string | null = null
  const root_id = generateTaskId()

  const now = new Date()
  for (let i = 0; i < length; i++) {
    const task_id = i === 0 ? root_id : generateTaskId()
    const startTime = new Date(now.getTime() - (length - i) * 1000)
    const task: MockTask = {
      task_id,
      name: `${baseName}.step_${i + 1}`,
      state: 'SUCCESS',
      parent_id,
      root_id,
      node_type: 'TASK',
      group_id: null,
      chord_id: null,
      children: [],
      first_seen: startTime.toISOString(),
      last_updated: now.toISOString(),
      duration_ms: 500,
      events: [
        createMockEvent(task_id, `${baseName}.step_${i + 1}`, 'PENDING', {
          parent_id,
          root_id,
        }),
        createMockEvent(task_id, `${baseName}.step_${i + 1}`, 'SUCCESS', {
          parent_id,
          root_id,
          result: { step: i + 1 },
        }),
      ],
    }

    // Link parent to child
    if (tasks.length > 0) {
      tasks[tasks.length - 1].children.push(task_id)
    }

    tasks.push(task)
    parent_id = task_id
  }

  return tasks
}

/**
 * Create a GROUP with member tasks
 */
export function createGroup(
  memberCount: number,
  options: { withCallback?: boolean } = {},
): MockTask[] {
  const group_id = generateTaskId()
  const now = new Date()
  const startTime = new Date(now.getTime() - 2000)

  const groupNode: MockTask = {
    task_id: `group:${group_id}`,
    name: `group:${group_id.slice(0, 8)}`,
    state: 'SUCCESS',
    parent_id: null,
    root_id: `group:${group_id}`,
    node_type: options.withCallback ? 'CHORD' : 'GROUP',
    group_id,
    chord_id: null,
    children: [],
    first_seen: startTime.toISOString(),
    last_updated: now.toISOString(),
    duration_ms: 2000,
    events: [], // Synthetic nodes have no events
  }

  const members: MockTask[] = []
  for (let i = 0; i < memberCount; i++) {
    const task_id = generateTaskId()
    const task: MockTask = {
      task_id,
      name: 'tasks.process_item',
      state: 'SUCCESS',
      parent_id: null, // Members have group_id, not parent_id
      root_id: `group:${group_id}`,
      node_type: 'TASK',
      group_id,
      chord_id: null,
      children: [],
      first_seen: startTime.toISOString(),
      last_updated: now.toISOString(),
      duration_ms: 500,
      events: [
        createMockEvent(task_id, 'tasks.process_item', 'PENDING', {
          group_id,
          root_id: `group:${group_id}`,
          args: [i],
        }),
        createMockEvent(task_id, 'tasks.process_item', 'SUCCESS', {
          group_id,
          root_id: `group:${group_id}`,
          result: i * 2,
        }),
      ],
    }
    members.push(task)
    groupNode.children.push(task_id)
  }

  const allTasks = [groupNode, ...members]

  // Add callback if requested
  if (options.withCallback) {
    const callback_id = generateTaskId()
    const callback: MockTask = {
      task_id: callback_id,
      name: 'tasks.aggregate',
      state: 'SUCCESS',
      parent_id: `group:${group_id}`,
      root_id: `group:${group_id}`,
      node_type: 'TASK',
      group_id: null,
      chord_id: null,
      children: [],
      first_seen: now.toISOString(),
      last_updated: now.toISOString(),
      duration_ms: 100,
      events: [
        createMockEvent(callback_id, 'tasks.aggregate', 'PENDING', {
          parent_id: `group:${group_id}`,
          root_id: `group:${group_id}`,
        }),
        createMockEvent(callback_id, 'tasks.aggregate', 'SUCCESS', {
          parent_id: `group:${group_id}`,
          root_id: `group:${group_id}`,
          result: { total: memberCount * 2 },
        }),
      ],
    }
    // Each member points to callback
    for (const member of members) {
      member.children.push(callback_id)
    }
    allTasks.push(callback)
  }

  return allTasks
}

/**
 * Create a workflow: parent task spawns a group
 */
export function createWorkflowWithGroup(memberCount: number): MockTask[] {
  const parent_id = generateTaskId()
  const group_id = generateTaskId()
  const now = new Date()
  const startTime = new Date(now.getTime() - 3000)

  const parent: MockTask = {
    task_id: parent_id,
    name: 'tasks.batch_processor',
    state: 'SUCCESS',
    parent_id: null,
    root_id: parent_id,
    node_type: 'TASK',
    group_id: null,
    chord_id: null,
    children: [`group:${group_id}`],
    first_seen: startTime.toISOString(),
    last_updated: now.toISOString(),
    duration_ms: 3000,
    events: [
      createMockEvent(parent_id, 'tasks.batch_processor', 'PENDING'),
      createMockEvent(parent_id, 'tasks.batch_processor', 'SUCCESS', {
        result: { items: memberCount },
      }),
    ],
  }

  const groupNode: MockTask = {
    task_id: `group:${group_id}`,
    name: `group:${group_id.slice(0, 8)}`,
    state: 'SUCCESS',
    parent_id: parent_id,
    root_id: parent_id,
    node_type: 'GROUP',
    group_id,
    chord_id: null,
    children: [],
    first_seen: startTime.toISOString(),
    last_updated: now.toISOString(),
    duration_ms: 2000,
    events: [],
  }

  const members: MockTask[] = []
  for (let i = 0; i < memberCount; i++) {
    const task_id = generateTaskId()
    const task: MockTask = {
      task_id,
      name: 'tasks.process_item',
      state: 'SUCCESS',
      parent_id: parent_id,
      root_id: parent_id,
      node_type: 'TASK',
      group_id,
      chord_id: null,
      children: [],
      first_seen: startTime.toISOString(),
      last_updated: now.toISOString(),
      duration_ms: 500,
      events: [
        createMockEvent(task_id, 'tasks.process_item', 'PENDING', {
          parent_id,
          root_id: parent_id,
          group_id,
          args: [i],
        }),
        createMockEvent(task_id, 'tasks.process_item', 'SUCCESS', {
          parent_id,
          root_id: parent_id,
          group_id,
          result: i * 2,
        }),
      ],
    }
    members.push(task)
    groupNode.children.push(task_id)
  }

  return [parent, groupNode, ...members]
}

/**
 * Default mock data set for tests
 */
export const DEFAULT_TASKS: MockTask[] = [
  createSuccessTask('tasks.add', 5),
  createSuccessTask('tasks.multiply', 20),
  createFailedTask(
    'tasks.always_fails',
    'ValueError: Intentional failure',
    'Traceback (most recent call last):\n  File "tasks.py", line 42\n    raise ValueError("Intentional failure")\nValueError: Intentional failure',
  ),
  ...createChain('tasks.workflow', 3),
  ...createGroup(3, { withCallback: true }),
  ...createWorkflowWithGroup(2),
]

/**
 * Registry mock data
 */
export const DEFAULT_REGISTRY = [
  {
    name: 'tasks.add',
    signature: '(x, y)',
    docstring: 'Add two numbers together.',
    module: 'tasks',
    bound: false,
  },
  {
    name: 'tasks.multiply',
    signature: '(x, y)',
    docstring: 'Multiply two numbers together.',
    module: 'tasks',
    bound: false,
  },
  {
    name: 'tasks.process_item',
    signature: '(item_id)',
    docstring: 'Process a single item.',
    module: 'tasks',
    bound: false,
  },
  {
    name: 'tasks.aggregate',
    signature: '(items)',
    docstring: 'Aggregate multiple items.',
    module: 'tasks',
    bound: false,
  },
  {
    name: 'tasks.batch_processor',
    signature: '(batch)',
    docstring: 'Process a batch of items.',
    module: 'tasks',
    bound: false,
  },
  {
    name: 'tasks.always_fails',
    signature: '()',
    docstring: 'A task that always fails.',
    module: 'tasks',
    bound: false,
  },
]
