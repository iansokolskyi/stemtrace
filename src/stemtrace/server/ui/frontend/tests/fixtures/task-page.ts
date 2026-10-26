import { expect, type Page } from '@playwright/test'

import { setupMockApi } from './mock-api'
import { createMockEvent, createSuccessTask, type MockTask } from './mock-data'

/** Helpers for the task page specs: fixed times, one successful task, and opening a task. */

const BASE = Date.parse('2026-10-17T12:00:00Z')

/** An ISO timestamp `seconds` after the fixed base time (2026-10-17T12:00:00Z). */
export const at = (seconds: number): string => new Date(BASE + seconds * 1000).toISOString()

/** Resolves `var(--token)` through a probe, so both sides of a comparison are rgb() strings. */
export async function tokenColour(page: Page, token: string): Promise<string> {
  return page.evaluate((name) => {
    const probe = document.createElement('div')
    probe.style.color = `var(--${name})`
    document.body.append(probe)
    const colour = getComputedStyle(probe).color
    probe.remove()
    return colour
  }, token)
}

/** A successful task with fixed times, plus any optional fields a test wants. */
export function successTask(overrides: Partial<MockTask> = {}): MockTask {
  const task = createSuccessTask('billing.invoices.send_receipt', { sent: true })
  const { task_id, name } = task
  task.events = [
    createMockEvent(task_id, name, 'PENDING', { timestamp: at(0), args: [7], root_id: task_id }),
    createMockEvent(task_id, name, 'STARTED', { timestamp: at(1), root_id: task_id }),
    createMockEvent(task_id, name, 'SUCCESS', {
      timestamp: at(2),
      result: { sent: true },
      root_id: task_id,
    }),
  ]
  task.first_seen = at(0)
  task.last_updated = at(2)
  task.duration_ms = 2000
  return Object.assign(task, overrides)
}

/** Serves `tasks` from the mock API and opens the page of the first one. */
export async function openTask(page: Page, ...tasks: MockTask[]): Promise<MockTask> {
  const api = await setupMockApi(page, { useDefaults: false })
  api.addTasks(tasks)
  await page.goto(`/tasks/${tasks[0].task_id}`)
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  return tasks[0]
}
