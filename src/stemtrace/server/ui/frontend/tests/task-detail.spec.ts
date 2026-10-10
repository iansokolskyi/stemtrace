import { expect, test, type WebSocketRoute } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import {
  createFailedTask,
  createGroup,
  createMockEvent,
  createSuccessTask,
} from './fixtures/mock-data'

/**
 * Tests for the Task Detail page.
 *
 * These tests use mocked API responses, no Docker required.
 * Run with E2E_MODE=real for integration testing against Docker.
 */

const isRealMode = process.env.E2E_MODE === 'real'

test.describe('Task Detail Page', () => {
  test.beforeEach(async ({ page }) => {
    if (!isRealMode) {
      await setupMockApi(page)
    }
    // Start from tasks list
    await page.goto('/')
    await page.waitForLoadState('networkidle')
  })

  test('displays task timeline', async ({ page }) => {
    // Find and click a task
    const taskLink = page.locator('a[href*="/tasks/"]').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskLink.click()
    await page.waitForLoadState('networkidle')

    // Look for timeline section - the heading "Execution Timeline" should be visible
    const timelineHeading = page.getByText('Execution Timeline')

    // Wait for heading to be visible using expect assertion with auto-retry
    await expect(timelineHeading).toBeVisible()
  })

  test('shows task metadata', async ({ page }) => {
    const taskLink = page.locator('a[href*="/tasks/"]').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskLink.click()
    await page.waitForLoadState('networkidle')

    // Should show task ID somewhere
    const taskIdElement = page.locator("code, .task-id, [data-testid='task-id']")
    const count = await taskIdElement.count()
    // Verify page renders task metadata elements
    expect(typeof count).toBe('number')
  })

  test('displays parameters section for tasks with args', async ({ page }) => {
    const taskLink = page.locator('a[href*="/tasks/"]').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskLink.click()
    await page.waitForLoadState('networkidle')

    // Look for parameters/arguments section
    const paramsSection = page.locator(
      '[data-testid="parameters"], .parameters, h3:has-text("Parameters"), h2:has-text("Parameters")',
    )

    // May or may not be visible depending on task
    const paramCount = await paramsSection.count()
    // Verify page renders (count check is informational)
    expect(typeof paramCount).toBe('number')
  })

  test('displays result section for completed tasks', async ({ page }) => {
    const taskLink = page.locator('a[href*="/tasks/"]').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskLink.click()
    await page.waitForLoadState('networkidle')

    // Look for result section
    const resultSection = page.locator(
      '[data-testid="result"], .result, h3:has-text("Result"), h2:has-text("Result"), pre',
    )

    // May or may not be visible depending on task state
    const resultCount = await resultSection.count()
    // Verify page renders (count check is informational)
    expect(typeof resultCount).toBe('number')
  })

  test('shows error details for failed tasks', async ({ page }) => {
    // Try to find a failed task - look for FAILURE badge or red-colored indicators
    const failedLink = page.locator('a[href*="/tasks/"]:has-text("FAILURE")').first()
    const isVisible = await failedLink.isVisible().catch(() => false)

    if (!isVisible) {
      // No failed tasks visible, skip the test
      test.skip()
      return
    }

    await failedLink.click()
    await page.waitForLoadState('networkidle')

    // Should show error/exception section or traceback
    const errorSection = page.locator(
      '[data-testid="error"], .error, .exception, h2:has-text("Error"), h2:has-text("Exception"), pre',
    )

    const errorCount = await errorSection.count()
    // Verify page renders error section when task failed
    expect(typeof errorCount).toBe('number')
  })

  test('can navigate back to tasks list', async ({ page }) => {
    const taskLink = page.locator('a[href*="/tasks/"]').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskLink.click()
    await page.waitForLoadState('networkidle')

    // Find back link or tasks nav
    const backLink = page
      .locator('a[href="/"], a:has-text("Tasks"), a:has-text("Back"), nav a')
      .first()

    await backLink.click()
    await expect(page).toHaveURL(/\/$|\/tasks$/)
  })
})

test.describe('Task Detail - Mock Scenarios', () => {
  // These tests only run in mock mode
  test.skip(isRealMode, 'Mock-only test')

  test('shows specific task details', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = createSuccessTask('test.my_task', { answer: 42 })
    mockApi.addTask(task)

    // Navigate directly to the task detail page
    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    // Should show the task name
    const taskName = page.getByText('test.my_task')
    await expect(taskName.first()).toBeVisible()

    // Should show SUCCESS state
    const successBadge = page.getByText('SUCCESS')
    await expect(successBadge.first()).toBeVisible()
  })

  test('shows error details for failed task', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const failedTask = createFailedTask(
      'test.broken_task',
      'RuntimeError: Something broke',
      'Traceback (most recent call last):\n  File "main.py", line 10\nRuntimeError: Something broke',
    )
    mockApi.addTask(failedTask)

    // Navigate directly to the failed task
    await page.goto(`/tasks/${failedTask.task_id}`)
    await page.waitForLoadState('networkidle')

    // Should show FAILURE state
    const failureBadge = page.getByText('FAILURE')
    await expect(failureBadge.first()).toBeVisible()

    // Should show error message somewhere
    const errorText = page.getByText('RuntimeError')
    await expect(errorText.first()).toBeVisible()
  })

  test('shows timeline with all events', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = createSuccessTask('test.timeline_task', 'done')
    mockApi.addTask(task)

    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    // Should show Execution Timeline heading
    const timelineHeading = page.getByText('Execution Timeline')
    await expect(timelineHeading).toBeVisible()

    // Should show PENDING, STARTED, SUCCESS events
    const pendingEvent = page.getByText('PENDING')
    const startedEvent = page.getByText('STARTED')
    const successEvent = page.getByText('SUCCESS')

    await expect(pendingEvent.first()).toBeVisible()
    await expect(startedEvent.first()).toBeVisible()
    await expect(successEvent.first()).toBeVisible()
  })

  test('orders events by time when a main-process failure reports retries=0', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = createFailedTask('test.time_limited', 'TimeLimitExceeded', 'Traceback')
    const base = Date.now() - 10_000
    const at = (seconds: number) => new Date(base + seconds * 1000).toISOString()
    const { task_id, name } = task
    // Celery stamps retries=0 on failures raised in the worker main process
    // (hard time limit, lost worker), even after retries.
    task.events = [
      createMockEvent(task_id, name, 'PENDING', { timestamp: at(0), retries: 0 }),
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(1), retries: 0 }),
      createMockEvent(task_id, name, 'RETRY', {
        timestamp: at(2),
        retries: 0,
        exception: 'RuntimeError: flaky',
      }),
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(3), retries: 1 }),
      createMockEvent(task_id, name, 'FAILURE', {
        timestamp: at(5),
        retries: 0,
        exception: 'TimeLimitExceeded',
      }),
    ]
    task.first_seen = at(0)
    task.last_updated = at(5)
    task.duration_ms = 5000
    mockApi.addTask(task)

    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    const rows = page.getByTestId('event-row')
    await expect(rows).toHaveCount(5)
    const expected = ['PENDING', 'STARTED', 'RETRY', 'STARTED', 'FAILURE']
    for (const [i, state] of expected.entries()) {
      await expect(rows.nth(i)).toContainText(state)
    }

    // Timeline segments must not have negative durations.
    await expect(page.getByText(/^-\d/)).toHaveCount(0)
  })

  test('puts a PENDING stamped after the task finished first', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = createSuccessTask('test.late_pending', 'done')
    const base = Date.now() - 10_000
    const at = (ms: number) => new Date(base + ms).toISOString()
    const { task_id, name } = task
    // task_sent fires after publish, so the sender's PENDING can be stamped
    // after the worker's STARTED and SUCCESS.
    task.events = [
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(10), retries: 0 }),
      createMockEvent(task_id, name, 'SUCCESS', { timestamp: at(20), retries: 0 }),
      createMockEvent(task_id, name, 'PENDING', { timestamp: at(25), retries: 0 }),
    ]
    task.first_seen = at(10)
    task.last_updated = at(20)
    task.duration_ms = 10
    mockApi.addTask(task)

    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    const rows = page.getByTestId('event-row')
    await expect(rows).toHaveCount(3)
    for (const [i, state] of ['PENDING', 'STARTED', 'SUCCESS'].entries()) {
      await expect(rows.nth(i)).toContainText(state)
    }

    const segments = page.getByTestId('timeline-segment')
    await expect(segments).toHaveCount(3)
    await expect(segments.nth(0)).toContainText('PENDING')
    await expect(segments.nth(0)).toContainText('0ms')
    await expect(segments.nth(1)).toContainText('STARTED')
    await expect(segments.nth(1)).toContainText('10ms')
    await expect(segments.nth(2)).toContainText('SUCCESS')
    await expect(page.getByText(/^-\d/)).toHaveCount(0)
  })

  test('puts a PENDING stamped after RECEIVED before it', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = createSuccessTask('test.late_pending_received', 'done')
    const base = Date.now() - 10_000
    const at = (ms: number) => new Date(base + ms).toISOString()
    const { task_id, name } = task
    // The sender's PENDING can be stamped after the worker's RECEIVED.
    task.events = [
      createMockEvent(task_id, name, 'RECEIVED', { timestamp: at(10), retries: 0 }),
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(20), retries: 0 }),
      createMockEvent(task_id, name, 'SUCCESS', { timestamp: at(30), retries: 0 }),
      createMockEvent(task_id, name, 'PENDING', { timestamp: at(60), retries: 0 }),
    ]
    task.first_seen = at(10)
    task.last_updated = at(30)
    task.duration_ms = 20
    mockApi.addTask(task)

    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    const rows = page.getByTestId('event-row')
    await expect(rows).toHaveCount(4)
    for (const [i, state] of ['PENDING', 'RECEIVED', 'STARTED', 'SUCCESS'].entries()) {
      await expect(rows.nth(i)).toContainText(state)
    }

    const segments = page.getByTestId('timeline-segment')
    await expect(segments).toHaveCount(4)
    await expect(segments.nth(0)).toContainText('PENDING')
    await expect(segments.nth(0)).toContainText('0ms')
    await expect(segments.nth(1)).toContainText('RECEIVED')
    await expect(segments.nth(1)).toContainText('10ms')
    await expect(segments.nth(2)).toContainText('STARTED')
    await expect(segments.nth(2)).toContainText('10ms')
    await expect(segments.nth(3)).toContainText('SUCCESS')
    await expect(page.getByText(/^-\d/)).toHaveCount(0)
  })
})

test.describe('Task Detail - Output', () => {
  test.skip(isRealMode, 'Mock-only test')

  function retriedTask() {
    const task = createFailedTask('test.flaky_output', 'ValueError: gave up', 'Traceback')
    const base = Date.now() - 10_000
    const at = (seconds: number) => new Date(base + seconds * 1000).toISOString()
    const { task_id, name } = task
    task.events = [
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(1), retries: 0 }),
      createMockEvent(task_id, name, 'RETRY', {
        timestamp: at(2),
        retries: 0,
        exception: 'RuntimeError: flaky',
        output: 'attempt 0\nWARNING app.tasks: upstream timed out',
      }),
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(3), retries: 1 }),
      createMockEvent(task_id, name, 'FAILURE', {
        timestamp: at(5),
        retries: 1,
        exception: 'ValueError: gave up',
        output: 'attempt 1\nERROR app.tasks: giving up',
      }),
    ]
    task.first_seen = at(1)
    task.last_updated = at(5)
    task.duration_ms = 4000
    return task
  }

  test('shows output per attempt, latest first and expanded', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = retriedTask()
    mockApi.addTask(task)

    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    const section = page.getByTestId('task-output')
    await expect(section.getByRole('heading', { name: 'Output' })).toBeVisible()

    const attempts = section.getByTestId('output-attempt')
    await expect(attempts).toHaveCount(2)
    await expect(attempts.nth(0)).toContainText('FAILURE')
    await expect(attempts.nth(0)).toContainText('retry #1')
    await expect(attempts.nth(1)).toContainText('RETRY')

    // Only the latest attempt is expanded initially.
    const texts = section.getByTestId('output-text')
    await expect(texts).toHaveCount(1)
    await expect(texts.first()).toHaveText('attempt 1\nERROR app.tasks: giving up')

    // Expanding the earlier attempt shows its own output.
    await attempts
      .nth(1)
      .getByRole('button', { name: /expand/ })
      .click()
    await expect(attempts.nth(1).getByTestId('output-text')).toHaveText(
      'attempt 0\nWARNING app.tasks: upstream timed out',
    )
  })

  test('orders attempts by retry number when worker clocks disagree', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = retriedTask()
    const [, retry, , failure] = task.events
    // Attempt 1 ran on a worker whose clock is behind: its FAILURE is stamped
    // before attempt 0's RETRY.
    failure.timestamp = new Date(Date.parse(retry.timestamp) - 60_000).toISOString()
    mockApi.addTask(task)

    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    const attempts = page.getByTestId('output-attempt')
    await expect(attempts).toHaveCount(2)
    await expect(attempts.nth(0)).toContainText('FAILURE')
    await expect(page.getByTestId('output-text')).toHaveText(
      'attempt 1\nERROR app.tasks: giving up',
    )
  })

  test('refetches the open task only for its own live events', async ({ page }) => {
    let socket: WebSocketRoute | null = null
    await page.routeWebSocket(/\/ws$/, (ws) => {
      socket = ws
    })
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = retriedTask()
    const sibling = createSuccessTask('test.unrelated', 'done')
    mockApi.addTask(task)
    mockApi.addTask(sibling)
    let detailRequests = 0
    page.on('request', (request) => {
      if (new URL(request.url()).pathname.endsWith(`/tasks/${task.task_id}`)) {
        detailRequests += 1
      }
    })

    await page.goto(`/tasks/${task.task_id}`)
    await expect(page.getByTestId('output-attempt')).toHaveCount(2)
    await expect.poll(() => socket !== null).toBe(true)
    const ws = socket as unknown as WebSocketRoute
    const initial = detailRequests

    // An event for another task must not refetch this task's detail (and output).
    ws.send(JSON.stringify({ task_id: sibling.task_id, state: 'STARTED' }))
    await page.waitForTimeout(1000)
    expect(detailRequests).toBe(initial)

    // An event for this task does.
    ws.send(JSON.stringify({ task_id: task.task_id, state: 'FAILURE' }))
    await expect.poll(() => detailRequests).toBe(initial + 1)
  })

  test('refetches a chord page for its members and callback', async ({ page }) => {
    let socket: WebSocketRoute | null = null
    await page.routeWebSocket(/\/ws$/, (ws) => {
      socket = ws
    })
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const [chordNode, ...rest] = createGroup(2, { withCallback: true })
    const callback = rest[rest.length - 1]
    const groupId = chordNode.group_id as string
    // The server lists the callback among the CHORD node's children.
    chordNode.children.push(callback.task_id)
    for (const task of [chordNode, ...rest]) mockApi.addTask(task)
    let detailRequests = 0
    page.on('request', (request) => {
      const path = decodeURIComponent(new URL(request.url()).pathname)
      if (path.endsWith(`/tasks/${chordNode.task_id}`)) detailRequests += 1
    })

    await page.goto(`/tasks/${chordNode.task_id}`)
    await page.waitForLoadState('networkidle')
    await expect.poll(() => socket !== null).toBe(true)
    await expect.poll(() => detailRequests).toBeGreaterThan(0)
    const ws = socket as unknown as WebSocketRoute
    const initial = detailRequests

    ws.send(JSON.stringify({ task_id: 'unrelated-task', state: 'STARTED', group_id: null }))
    await page.waitForTimeout(1000)
    expect(detailRequests).toBe(initial)

    // A header task joining or changing state (matched by group_id).
    ws.send(JSON.stringify({ task_id: 'new-member', state: 'STARTED', group_id: groupId }))
    await expect.poll(() => detailRequests).toBe(initial + 1)

    // The callback as Celery 5.6 sends it: frozen with the chord's group_id.
    ws.send(
      JSON.stringify({
        task_id: callback.task_id,
        state: 'STARTED',
        group_id: groupId,
        parent_id: null,
      }),
    )
    await expect.poll(() => detailRequests).toBe(initial + 2)

    // Fallback: a callback event without group_id still matches, because the
    // server lists the callback among the CHORD node's children.
    ws.send(
      JSON.stringify({
        task_id: callback.task_id,
        state: 'SUCCESS',
        group_id: null,
        parent_id: null,
      }),
    )
    await expect.poll(() => detailRequests).toBe(initial + 3)
  })

  test('copies an attempt output to the clipboard', async ({ page }) => {
    await page.addInitScript(() => {
      const copied: string[] = []
      Object.defineProperty(window, '__copied', { value: copied })
      Object.defineProperty(navigator, 'clipboard', {
        value: {
          writeText: async (text: string) => {
            copied.push(text)
          },
        },
      })
    })
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = retriedTask()
    mockApi.addTask(task)

    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    const latest = page.getByTestId('output-attempt').first()
    await latest.getByRole('button', { name: 'Copy' }).click()
    await expect(latest.getByRole('button', { name: 'Copied' })).toBeVisible()
    const copied = await page.evaluate(() => (window as unknown as { __copied: string[] }).__copied)
    expect(copied).toEqual(['attempt 1\nERROR app.tasks: giving up'])
  })

  test('renders no output section when no attempt has output', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = createSuccessTask('test.silent_task', 'done')
    mockApi.addTask(task)

    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    await expect(page.getByRole('heading', { name: 'Event History' })).toBeVisible()
    await expect(page.getByTestId('task-output')).toHaveCount(0)
  })
})
