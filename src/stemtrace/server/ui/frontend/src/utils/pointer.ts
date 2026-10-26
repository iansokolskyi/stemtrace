/**
 * Whether a pointer button is down, and a way to run something once it is released.
 *
 * Nothing the user is pointing at may move between a press and its release, or the click lands
 * on whatever moved into its place. Code that would change the URL or the layout in response to
 * a press (a field losing focus, a popover re-anchoring) waits here until the release and the
 * click it ends have been handled.
 */

let down = false
const waiting: (() => void)[] = []

function pressed(): void {
  down = true
}

function released(): void {
  down = false
  // After the click this release produces: that event is dispatched in the same task, so a
  // zero-delay timer runs once its handlers are done
  window.setTimeout(() => {
    for (const run of waiting.splice(0)) run()
  }, 0)
}

if (typeof window !== 'undefined') {
  window.addEventListener('pointerdown', pressed, true)
  window.addEventListener('pointerup', released, true)
  window.addEventListener('pointercancel', released, true)
}

/** Runs `run` now if no button is down, otherwise after the next release and its click. */
export function afterPointerRelease(run: () => void): void {
  if (down) waiting.push(run)
  else run()
}
