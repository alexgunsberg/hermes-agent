/**
 * Layered remote-connection diagnosis for Hermes Desktop.
 *
 * Runs the same two legs Test Remote exercises (HTTP /api/status, then live
 * /api/ws) and returns a ConnectionHealthReport without tokens/tickets.
 *
 * Pure orchestration: network + WS deps are injected so unit tests can drive
 * each layer without a real backend.
 */

import {
  classifyConnectionHealth,
  type ConnectionHealthLayer,
  type ConnectionHealthReport,
  sanitizeConnectionDetail
} from './connection-health'

export interface DiagnoseConnectionDeps {
  /** Public or authed GET /api/status. Throws on unreachable / non-2xx. */
  probeHttp: (baseUrl: string) => Promise<{ version?: null | string }>
  /**
   * Build the WS URL the renderer would dial (may mint an OAuth ticket).
   * Throw with a reauth-shaped error when the session cannot mint.
   */
  resolveWsUrl: (baseUrl: string) => Promise<null | string>
  /** Live WebSocket upgrade probe. */
  probeWs: (wsUrl: string) => Promise<{ ok: boolean; reason?: string }>
  /** True when an error means OAuth / session reauth is required. */
  isAuthRejected?: (error: unknown) => boolean
}

export interface DiagnoseConnectionInput {
  baseUrl: string
  /** Skip the WS leg when the runtime has no WebSocket (should be rare). */
  skipWs?: boolean
}

export interface DiagnoseConnectionResult extends ConnectionHealthReport {
  httpOk: boolean
  wsOk: boolean | null
  baseUrl: string
  version: null | string
}

function publicBaseUrl(raw: string): string {
  return String(raw || '')
    .trim()
    .replace(/\/+$/, '')
}

function errorTextOf(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }

  return String(error || '')
}

export async function diagnoseRemoteConnection(
  input: DiagnoseConnectionInput,
  deps: DiagnoseConnectionDeps
): Promise<DiagnoseConnectionResult> {
  const baseUrl = publicBaseUrl(input.baseUrl)
  const isAuthRejected = deps.isAuthRejected ?? (() => false)

  let httpOk = false
  let wsOk: boolean | null = null
  let version: null | string = null
  let authRejected = false
  let detail: null | string = null

  try {
    const status = await deps.probeHttp(baseUrl)
    httpOk = true
    version = status?.version ?? null
  } catch (error) {
    authRejected = isAuthRejected(error)
    detail = errorTextOf(error)
    const report = classifyConnectionHealth({
      httpOk: false,
      wsOk: null,
      authRejected,
      errorText: detail
    })

    return {
      ...report,
      httpOk: false,
      wsOk: null,
      baseUrl,
      version: null,
      detail: sanitizeConnectionDetail(detail)
    }
  }

  if (input.skipWs) {
    const report = classifyConnectionHealth({
      httpOk: true,
      wsOk: null,
      gatewayState: 'open'
    })

    return {
      ...report,
      layer: 'connected' satisfies ConnectionHealthLayer,
      code: 'health.connected',
      httpOk: true,
      wsOk: null,
      baseUrl,
      version,
      detail: null
    }
  }

  let wsUrl: null | string = null

  try {
    wsUrl = await deps.resolveWsUrl(baseUrl)
  } catch (error) {
    authRejected = isAuthRejected(error) || /oauth|sign-?in|session has expired/i.test(errorTextOf(error))
    detail = errorTextOf(error)
    const report = classifyConnectionHealth({
      httpOk: true,
      wsOk: false,
      authRejected,
      errorText: detail
    })

    return {
      ...report,
      httpOk: true,
      wsOk: false,
      baseUrl,
      version,
      detail: sanitizeConnectionDetail(detail)
    }
  }

  if (!wsUrl) {
    const report = classifyConnectionHealth({
      httpOk: true,
      wsOk: false,
      errorText: 'Could not build a WebSocket URL for the gateway.'
    })

    return {
      ...report,
      httpOk: true,
      wsOk: false,
      baseUrl,
      version,
      detail: report.detail
    }
  }

  // Never log/return the credentialized URL — probe uses it in-process only.
  const probe = await deps.probeWs(wsUrl)
  wsOk = probe.ok

  if (!probe.ok) {
    detail = probe.reason || 'WebSocket connection failed.'
    authRejected = /credential rejected|unauthorized|401\b|ticket/i.test(detail)
  }

  const report = classifyConnectionHealth({
    httpOk: true,
    wsOk,
    authRejected,
    errorText: detail
  })

  return {
    ...report,
    httpOk: true,
    wsOk,
    baseUrl,
    version,
    detail: sanitizeConnectionDetail(detail)
  }
}
