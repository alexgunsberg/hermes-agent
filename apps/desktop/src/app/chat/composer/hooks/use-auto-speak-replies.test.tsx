import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $messages } from '@/store/session'
import { $autoSpeakReplies } from '@/store/voice-prefs'

import { useAutoSpeakReplies } from './use-auto-speak-replies'

const mocks = vi.hoisted(() => ({
  ownsAmbientCue: vi.fn(),
  playSpeechText: vi.fn()
}))

vi.mock('@/store/ambient', () => ({ ownsAmbientCue: mocks.ownsAmbientCue }))
vi.mock('@/lib/voice-playback', () => ({ playSpeechText: mocks.playSpeechText }))

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

describe('useAutoSpeakReplies', () => {
  beforeEach(() => {
    $autoSpeakReplies.set(true)
    $messages.set([])
    mocks.ownsAmbientCue.mockReset()
    mocks.playSpeechText.mockReset()
    mocks.playSpeechText.mockResolvedValue(undefined)
  })

  afterEach(() => {
    cleanup()
    $autoSpeakReplies.set(false)
    $messages.set([])
  })

  it('does not speak a stale reply after switching sessions while cue ownership is pending', async () => {
    const claim = deferred<boolean>()
    mocks.ownsAmbientCue.mockReturnValueOnce(claim.promise).mockResolvedValue(false)
    let reply: { id: string; pending: boolean; text: string } | null = null
    const markSpoken = vi.fn()

    const { rerender } = renderHook(
      ({ sessionId }) =>
        useAutoSpeakReplies({
          conversationActive: false,
          failureLabel: 'Failed',
          markSpoken,
          pendingReply: () => reply,
          sessionId
        }),
      { initialProps: { sessionId: 'session-a' } }
    )

    reply = { id: 'reply-a', pending: false, text: 'old reply' }
    act(() => $messages.set($messages.get().slice()))
    expect(mocks.ownsAmbientCue).toHaveBeenCalledWith('speak:reply-a')

    rerender({ sessionId: 'session-b' })
    await act(async () => {
      claim.resolve(true)
      await claim.promise
    })

    expect(mocks.playSpeechText).not.toHaveBeenCalled()
  })

  it('drops an older claim when a newer reply completes in the same session', async () => {
    const firstClaim = deferred<boolean>()
    const secondClaim = deferred<boolean>()
    mocks.ownsAmbientCue.mockReturnValueOnce(firstClaim.promise).mockReturnValueOnce(secondClaim.promise)
    let reply: { id: string; pending: boolean; text: string } | null = null
    const markSpoken = vi.fn()

    renderHook(() =>
      useAutoSpeakReplies({
        conversationActive: false,
        failureLabel: 'Failed',
        markSpoken,
        pendingReply: () => reply,
        sessionId: 'session-a'
      })
    )

    reply = { id: 'reply-a', pending: false, text: 'old reply' }
    act(() => $messages.set($messages.get().slice()))
    reply = { id: 'reply-b', pending: false, text: 'new reply' }
    act(() => $messages.set($messages.get().slice()))

    await act(async () => {
      firstClaim.resolve(true)
      await firstClaim.promise
    })
    expect(mocks.playSpeechText).not.toHaveBeenCalled()

    await act(async () => {
      secondClaim.resolve(true)
      await secondClaim.promise
    })
    expect(mocks.playSpeechText).toHaveBeenCalledOnce()
    expect(mocks.playSpeechText).toHaveBeenCalledWith('new reply', {
      messageId: 'reply-b',
      source: 'read-aloud'
    })
  })
})