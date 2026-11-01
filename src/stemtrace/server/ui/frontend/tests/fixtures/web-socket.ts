import type { Page } from '@playwright/test'

// The WebSocket fails at once, so the status reads "Polling" (the longest tooltip).
export async function failWebSocket(page: Page): Promise<void> {
  await page.addInitScript(() => {
    class FailingWebSocket extends EventTarget {
      static readonly CONNECTING = 0
      static readonly OPEN = 1
      static readonly CLOSING = 2
      static readonly CLOSED = 3
      readyState = 3
      onopen: (() => void) | null = null
      onmessage: (() => void) | null = null
      onerror: (() => void) | null = null
      onclose: (() => void) | null = null
      constructor() {
        super()
        setTimeout(() => {
          this.onerror?.()
          this.onclose?.()
        }, 0)
      }
      close(): void {}
      send(): void {}
    }
    Object.defineProperty(window, 'WebSocket', { value: FailingWebSocket })
  })
}
