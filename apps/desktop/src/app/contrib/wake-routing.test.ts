import { describe, expect, it, vi } from 'vitest'

import { routeWakeDetected } from './wake-routing'

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

describe('routeWakeDetected', () => {
  it('waits for a cross-profile gateway before starting its fresh voice session', async () => {
    const profileReady = deferred<void>()
    const calls: string[] = []
    const ensureGatewayProfile = vi.fn(async () => {
      calls.push('ensure:start')
      await profileReady.promise
      calls.push('ensure:ready')
    })
    const newSessionInProfile = vi.fn(() => calls.push('session:new'))
    const requestVoiceConversationStart = vi.fn(() => calls.push('voice:start'))
    const startFreshSessionDraft = vi.fn(() => calls.push('session:fresh'))
    const recoverWakeListening = vi.fn()

    const routed = routeWakeDetected(
      { profile: 'work', start_new_session: true },
      'default',
      {
        ensureGatewayProfile,
        newSessionInProfile,
        requestVoiceConversationStart,
        startFreshSessionDraft,
        recoverWakeListening,
        isCurrent: () => true
      }
    )

    await Promise.resolve()
    expect(ensureGatewayProfile).toHaveBeenCalledWith('work')
    expect(newSessionInProfile).not.toHaveBeenCalled()
    expect(requestVoiceConversationStart).not.toHaveBeenCalled()

    profileReady.resolve()
    await routed

    expect(calls).toEqual(['ensure:start', 'ensure:ready', 'session:new', 'voice:start'])
  })

  it('starts immediately when the wake belongs to the active profile', async () => {
    const ensureGatewayProfile = vi.fn()
    const newSessionInProfile = vi.fn()
    const requestVoiceConversationStart = vi.fn()
    const startFreshSessionDraft = vi.fn()
    const recoverWakeListening = vi.fn()

    await routeWakeDetected({ profile: 'default' }, 'default', {
      ensureGatewayProfile,
      newSessionInProfile,
      requestVoiceConversationStart,
      startFreshSessionDraft,
      recoverWakeListening,
      isCurrent: () => true
    })

    expect(ensureGatewayProfile).not.toHaveBeenCalled()
    expect(newSessionInProfile).not.toHaveBeenCalled()
    expect(startFreshSessionDraft).toHaveBeenCalledOnce()
    expect(requestVoiceConversationStart).toHaveBeenCalledOnce()
  })

  it('drops a superseded cross-profile wake after its gateway await', async () => {
    const workReady = deferred<void>()
    let currentRoute = 0
    const newSessionInProfile = vi.fn()
    const requestVoiceConversationStart = vi.fn()
    const startFreshSessionDraft = vi.fn()
    const recoverWakeListening = vi.fn()
    const ensureGatewayProfile = vi.fn(async (profile: string) => {
      if (profile === 'work') {
        await workReady.promise
      }
    })

    const route = (profile: string) => {
      const routeId = ++currentRoute
      return routeWakeDetected({ profile }, 'default', {
        ensureGatewayProfile,
        newSessionInProfile,
        requestVoiceConversationStart,
        startFreshSessionDraft,
        recoverWakeListening,
        isCurrent: () => routeId === currentRoute
      })
    }

    const staleRoute = route('work')
    await Promise.resolve()
    await route('other')
    workReady.resolve()
    await staleRoute

    expect(newSessionInProfile).toHaveBeenCalledOnce()
    expect(newSessionInProfile).toHaveBeenCalledWith('other')
    expect(requestVoiceConversationStart).toHaveBeenCalledOnce()
    expect(recoverWakeListening).not.toHaveBeenCalled()
  })

  it('re-arms wake listening when the current profile route fails', async () => {
    const error = new Error('gateway unavailable')
    const ensureGatewayProfile = vi.fn(async () => {
      throw error
    })
    const newSessionInProfile = vi.fn()
    const requestVoiceConversationStart = vi.fn()
    const startFreshSessionDraft = vi.fn()
    const recoverWakeListening = vi.fn(async () => undefined)

    await expect(
      routeWakeDetected({ profile: 'work' }, 'default', {
        ensureGatewayProfile,
        newSessionInProfile,
        requestVoiceConversationStart,
        startFreshSessionDraft,
        recoverWakeListening,
        isCurrent: () => true
      })
    ).rejects.toBe(error)

    expect(recoverWakeListening).toHaveBeenCalledOnce()
    expect(newSessionInProfile).not.toHaveBeenCalled()
    expect(requestVoiceConversationStart).not.toHaveBeenCalled()
  })
})
