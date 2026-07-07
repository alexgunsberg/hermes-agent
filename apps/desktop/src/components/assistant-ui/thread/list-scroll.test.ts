import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  captureThreadScrollSnapshot,
  readThreadScrollSnapshot,
  restoreThreadScrollPosition,
  shouldRestoreThreadScroll
} from './list'

describe('thread scroll snapshots', () => {
  afterEach(() => {
    window.sessionStorage.clear()
    vi.useRealTimers()
  })

  it('captures distance from bottom so reconnect/remount can preserve reading position', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-07T12:00:00Z'))

    const snapshot = captureThreadScrollSnapshot(
      'session-1',
      { clientHeight: 400, scrollHeight: 1400, scrollTop: 250 } as HTMLDivElement,
      false
    )

    expect(snapshot?.distanceFromBottom).toBe(750)
    expect(shouldRestoreThreadScroll(readThreadScrollSnapshot('session-1'))).toBe(true)
  })

  it('does not restore snapshots that are already near the bottom', () => {
    const snapshot = captureThreadScrollSnapshot(
      'session-2',
      { clientHeight: 400, scrollHeight: 1400, scrollTop: 950 } as HTMLDivElement,
      true
    )

    expect(snapshot?.distanceFromBottom).toBe(50)
    expect(shouldRestoreThreadScroll(readThreadScrollSnapshot('session-2'))).toBe(false)
  })

  it('restores the same distance from bottom after content height changes', () => {
    const viewport = { clientHeight: 400, scrollHeight: 2200, scrollTop: 0 } as HTMLDivElement

    restoreThreadScrollPosition(viewport, {
      atBottom: false,
      capturedAt: Date.now(),
      distanceFromBottom: 750
    })

    expect(viewport.scrollTop).toBe(1050)
  })
})
