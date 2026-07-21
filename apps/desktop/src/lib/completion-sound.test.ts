import { describe, expect, it } from 'vitest'

import { completionSoundDedupeKey } from './completion-sound'

describe('completionSoundDedupeKey', () => {
  it('distinguishes consecutive completions in the same session', () => {
    expect(completionSoundDedupeKey('session-a', 'completion-1')).not.toBe(
      completionSoundDedupeKey('session-a', 'completion-2')
    )
  })

  it('keeps a session fallback for older gateways', () => {
    expect(completionSoundDedupeKey('session-a')).toBe('session-a')
  })
})