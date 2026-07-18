import { describe, expect, it } from 'vitest'

import {
  classifyConnectionHealth,
  isRecoverableHealthFailure,
  sanitizeConnectionDetail
} from '@hermes/shared'

describe('connection health contract', () => {
  it('distinguishes endpoint unreachable from HTTP-ok / WS-rejected', () => {
    expect(
      classifyConnectionHealth({
        httpOk: false,
        errorText: 'fetch failed: ECONNREFUSED 127.0.0.1:9119'
      }).layer
    ).toBe('endpoint_unreachable')

    expect(
      classifyConnectionHealth({
        httpOk: true,
        wsOk: false,
        errorText:
          'Reached the gateway over HTTP, but the live WebSocket (/api/ws) connection failed: closed'
      }).layer
    ).toBe('http_ok_ws_rejected')
  })

  it('classifies auth rejection separately from a plain WS transport failure', () => {
    expect(
      classifyConnectionHealth({
        httpOk: true,
        wsOk: false,
        authRejected: true,
        errorText: 'Your remote gateway session has expired'
      }).layer
    ).toBe('auth_rejected')

    expect(
      classifyConnectionHealth({
        errorText: 'Gateway sign-in required'
      }).layer
    ).toBe('auth_rejected')
  })

  it('maps live gateway states to connected / reconnecting', () => {
    expect(classifyConnectionHealth({ gatewayState: 'open' }).layer).toBe('connected')
    expect(classifyConnectionHealth({ gatewayState: 'connecting' }).layer).toBe('reconnecting')
    expect(classifyConnectionHealth({ gatewayState: 'open', reconnecting: true }).layer).toBe(
      'reconnecting'
    )
    expect(classifyConnectionHealth({ httpOk: true, wsOk: true }).layer).toBe('connected')
  })

  it('never leaks tokens, tickets, or bearer secrets into detail', () => {
    const report = classifyConnectionHealth({
      httpOk: true,
      wsOk: false,
      errorText:
        'ws failed for wss://127.0.0.1:9119/api/ws?ticket=super-secret-ticket-value-1234567890abcdef ' +
        'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig ' +
        'X-Hermes-Session-Token: abcdefghijklmnopqrstuvwxyz0123456789'
    })

    expect(report.detail).toBeTruthy()
    expect(report.detail).not.toMatch(/super-secret-ticket/)
    expect(report.detail).not.toMatch(/eyJhbGciOi/)
    expect(report.detail).not.toMatch(/abcdefghijklmnopqrstuvwxyz0123456789/)
    expect(report.detail).toMatch(/\[redacted\]/)
    expect(report.code).toBe('health.http_ok_ws_rejected')
  })

  it('sanitizeConnectionDetail redacts query credentials while keeping host:port', () => {
    const cleaned = sanitizeConnectionDetail(
      'Could not connect to http://127.0.0.1:9119/api/ws?token=abc123secret&x=1'
    )

    expect(cleaned).toContain('127.0.0.1:9119')
    expect(cleaned).toContain('token=[redacted]')
    expect(cleaned).not.toContain('abc123secret')
  })

  it('marks hard failures as recoverable for the recovery overlay', () => {
    expect(isRecoverableHealthFailure('endpoint_unreachable')).toBe(true)
    expect(isRecoverableHealthFailure('http_ok_ws_rejected')).toBe(true)
    expect(isRecoverableHealthFailure('auth_rejected')).toBe(true)
    expect(isRecoverableHealthFailure('connected')).toBe(false)
    expect(isRecoverableHealthFailure('reconnecting')).toBe(false)
  })
})
