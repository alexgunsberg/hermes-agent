export interface WakeDetectedPayload {
  profile?: null | string
  start_new_session?: boolean
}

export interface WakeRoutingActions {
  ensureGatewayProfile: (profile: string) => Promise<void>
  isCurrent: () => boolean
  newSessionInProfile: (profile: string) => void
  recoverWakeListening: (isCurrent: () => boolean) => Promise<void>
  requestVoiceConversationStart: () => void
  startFreshSessionDraft: () => void
}

export async function routeWakeDetected(
  payload: WakeDetectedPayload | undefined,
  activeProfile: string,
  actions: WakeRoutingActions
): Promise<void> {
  const targetProfile = payload?.profile?.trim()
  const startNewSession = payload?.start_new_session !== false

  if (!actions.isCurrent()) {
    return
  }

  if (targetProfile && targetProfile !== activeProfile) {
    try {
      await actions.ensureGatewayProfile(targetProfile)
    } catch (error) {
      if (actions.isCurrent()) {
        await actions.recoverWakeListening(actions.isCurrent)
      }
      throw error
    }

    if (!actions.isCurrent()) {
      return
    }

    if (startNewSession) {
      actions.newSessionInProfile(targetProfile)
    }
  } else if (startNewSession) {
    actions.startFreshSessionDraft()
  }

  actions.requestVoiceConversationStart()
}
