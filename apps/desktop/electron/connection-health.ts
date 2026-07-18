/**
 * Desktop connection-health contract.
 *
 * Classifies gateway reachability into a small set of recoverable layers so the
 * UI can show architecture-neutral recovery copy (Retry / Gateway settings /
 * Sign in) without teaching users to expose ports or run destructive commands.
 *
 * Layers deliberately name the failure surface, not the transport (no "SSH" /
 * "tunnel" / "Tailscale" wording): a loopback remote URL that is down looks the
 * same as any other unreachable endpoint from Desktop's point of view.
 *
 * Keep in sync with `apps/shared/src/connection-health.ts` (renderer imports
 * the shared package; Electron's tsconfig cannot pull files from outside
 * `apps/desktop`, so diagnose uses this local twin).
 *
 * Invariants:
 * - Reports never include tokens, tickets, cookies, or Authorization material.
 * - `code` is a stable machine key for i18n / tests.
 * - Classification prefers explicit probe booleans over free-text heuristics.
 */

export type ConnectionHealthLayer =
  | 'connected'
  | 'reconnecting'
  | 'endpoint_unreachable'
  | 'http_ok_ws_rejected'
  | 'auth_rejected'
  | 'unknown'

export type ConnectionHealthCode = `health.${ConnectionHealthLayer}`

export interface ConnectionHealthInput {
  /** Live JsonRpcGatewayClient connection state when known. */
  gatewayState?: null | string
  /** True while the renderer is actively backing off a reconnect. */
  reconnecting?: boolean | null
  /** Result of GET /api/status (or equivalent HTTP liveness). */
  httpOk?: boolean | null
  /** Result of a live /api/ws probe. Null when the WS leg was not attempted. */
  wsOk?: boolean | null
  /** True when OAuth/session mint or WS auth explicitly rejected credentials. */
  authRejected?: boolean | null
  /** Free-text error from boot/reconnect/test; sanitized into `detail`. */
  errorText?: null | string
}

export interface ConnectionHealthReport {
  layer: ConnectionHealthLayer
  code: ConnectionHealthCode
  /** Sanitized optional detail — never secrets. */
  detail: null | string
}

const TOKENISH_QUERY_RE = /([?&#](?:token|ticket|access_token|refresh_token|api_key|apikey|authorization)=)[^&#\s]*/gi
const BEARER_RE = /\bBearer\s+[A-Za-z0-9._\-+=\/]+/gi
const HEADER_TOKEN_RE = /\b(?:X-Hermes-Session-Token|Authorization)\s*[:=]\s*\S+/gi
// Long opaque secrets that sometimes leak into error strings.
const LONG_SECRET_RE = /\b[A-Za-z0-9_\-]{32,}\b/g

const AUTH_ERROR_RE =
  /remote gateway session has expired|gateway sign-in required|needs oauth login|oauth.*(?:not signed in|sign in)|credential rejected|ws-ticket|unauthorized|401\b/i

const HTTP_OK_WS_FAIL_RE =
  /reached the gateway over http[\s\S]*websocket|http check can pass while the websocket|live websocket \(\/api\/ws\) connection failed/i

const UNREACHABLE_RE =
  /econnrefused|enotfound|econnreset|etimedout|network(?:\s+error)?|fetch failed|did not become ready|failed liveness|could not connect|unreachable|timed out after|socket hang up|connection refused/i

export function sanitizeConnectionDetail(text: null | string | undefined): null | string {
  if (text == null) {
    return null
  }

  let cleaned = String(text)
    .replace(TOKENISH_QUERY_RE, '$1[redacted]')
    .replace(BEARER_RE, 'Bearer [redacted]')
    .replace(HEADER_TOKEN_RE, match => `${match.split(/[:=]/)[0]}=[redacted]`)
    // Keep short ids (ports, status codes) — only scrub long opaque blobs.
    .replace(LONG_SECRET_RE, '[redacted]')
    .replace(/\s+/g, ' ')
    .trim()

  if (!cleaned) {
    return null
  }

  // Cap length so diagnostics stay glanceable in overlays / toasts.
  if (cleaned.length > 280) {
    cleaned = `${cleaned.slice(0, 277)}...`
  }

  return cleaned
}

function report(layer: ConnectionHealthLayer, detail?: null | string): ConnectionHealthReport {
  return {
    layer,
    code: `health.${layer}`,
    detail: sanitizeConnectionDetail(detail)
  }
}

function looksAuthRejected(text: string): boolean {
  return AUTH_ERROR_RE.test(text)
}

function looksHttpOkWsRejected(text: string): boolean {
  return HTTP_OK_WS_FAIL_RE.test(text)
}

function looksUnreachable(text: string): boolean {
  return UNREACHABLE_RE.test(text)
}

/**
 * Classify connection health from probe booleans and/or free-text errors.
 * Explicit booleans always win over string heuristics.
 */
export function classifyConnectionHealth(input: ConnectionHealthInput = {}): ConnectionHealthReport {
  const gatewayState = String(input.gatewayState || '').toLowerCase()
  const errorText = String(input.errorText || '').trim()
  const detailSource = errorText || null

  // Auth beats "HTTP ok / WS fail" — the WS failure is usually the auth reject.
  if (input.authRejected === true || (errorText && looksAuthRejected(errorText))) {
    return report('auth_rejected', detailSource)
  }

  if (input.httpOk === true && input.wsOk === false) {
    return report('http_ok_ws_rejected', detailSource)
  }

  if (input.httpOk === false) {
    return report('endpoint_unreachable', detailSource)
  }

  if (gatewayState === 'open' && !input.reconnecting) {
    return report('connected', detailSource)
  }

  if (input.reconnecting || gatewayState === 'connecting') {
    return report('reconnecting', detailSource)
  }

  if (errorText && looksHttpOkWsRejected(errorText)) {
    return report('http_ok_ws_rejected', detailSource)
  }

  if (errorText && looksAuthRejected(errorText)) {
    return report('auth_rejected', detailSource)
  }

  if (errorText && looksUnreachable(errorText)) {
    return report('endpoint_unreachable', detailSource)
  }

  if (input.httpOk === true && input.wsOk === true) {
    return report('connected', detailSource)
  }

  if (gatewayState === 'closed' || gatewayState === 'error') {
    return report('endpoint_unreachable', detailSource)
  }

  return report('unknown', detailSource)
}

/** True when the layer is a hard failure the recovery overlay should own. */
export function isRecoverableHealthFailure(layer: ConnectionHealthLayer): boolean {
  return layer === 'endpoint_unreachable' || layer === 'http_ok_ws_rejected' || layer === 'auth_rejected'
}
