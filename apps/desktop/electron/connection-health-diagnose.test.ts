import assert from 'node:assert/strict'

import { describe, expect, it } from 'vitest'

import { diagnoseRemoteConnection } from './connection-health-diagnose'

describe('diagnoseRemoteConnection', () => {
  it('reports endpoint_unreachable when HTTP /api/status fails', async () => {
    const result = await diagnoseRemoteConnection(
      { baseUrl: 'http://127.0.0.1:9119/' },
      {
        probeHttp: async () => {
          throw new Error('fetch failed: ECONNREFUSED 127.0.0.1:9119')
        },
        resolveWsUrl: async () => {
          throw new Error('should not mint ws url when http is down')
        },
        probeWs: async () => ({ ok: true })
      }
    )

    expect(result.layer).toBe('endpoint_unreachable')
    expect(result.httpOk).toBe(false)
    expect(result.wsOk).toBeNull()
    expect(result.baseUrl).toBe('http://127.0.0.1:9119')
    expect(result.detail).toMatch(/ECONNREFUSED/)
    expect(result.detail).not.toMatch(/token=/i)
  })

  it('reports http_ok_ws_rejected when HTTP works but WS probe fails', async () => {
    const result = await diagnoseRemoteConnection(
      { baseUrl: 'http://127.0.0.1:9119' },
      {
        probeHttp: async () => ({ version: '1.2.3' }),
        resolveWsUrl: async () => 'ws://127.0.0.1:9119/api/ws?ticket=secret-ticket-value-should-not-leak',
        probeWs: async wsUrl => {
          // The credentialized URL is used in-process; the report must not echo it.
          assert.match(wsUrl, /ticket=/)

          return { ok: false, reason: 'The gateway accepted the connection then closed it (credential rejected?).' }
        }
      }
    )

    expect(result.layer).toBe('auth_rejected')
    expect(result.httpOk).toBe(true)
    expect(result.wsOk).toBe(false)
    expect(result.version).toBe('1.2.3')
    expect(result.detail).not.toMatch(/secret-ticket/)
  })

  it('reports http_ok_ws_rejected for a non-auth WS transport failure', async () => {
    const result = await diagnoseRemoteConnection(
      { baseUrl: 'http://127.0.0.1:9119' },
      {
        probeHttp: async () => ({ version: null }),
        resolveWsUrl: async () => 'ws://127.0.0.1:9119/api/ws?token=raw-session-token-value-xxxxxxxx',
        probeWs: async () => ({
          ok: false,
          reason: 'Timed out after 10000ms waiting for the WebSocket to open.'
        })
      }
    )

    expect(result.layer).toBe('http_ok_ws_rejected')
    expect(result.httpOk).toBe(true)
    expect(result.wsOk).toBe(false)
    expect(result.detail).not.toMatch(/raw-session-token/)
  })

  it('reports auth_rejected when WS URL mint fails (OAuth session dead)', async () => {
    const result = await diagnoseRemoteConnection(
      { baseUrl: 'http://127.0.0.1:9119' },
      {
        probeHttp: async () => ({ version: '9' }),
        resolveWsUrl: async () => {
          throw new Error('Your remote gateway session has expired. Sign in again.')
        },
        probeWs: async () => ({ ok: true }),
        isAuthRejected: () => true
      }
    )

    expect(result.layer).toBe('auth_rejected')
    expect(result.httpOk).toBe(true)
    expect(result.wsOk).toBe(false)
  })

  it('reports connected when HTTP and WS both succeed', async () => {
    const result = await diagnoseRemoteConnection(
      { baseUrl: 'http://127.0.0.1:9119' },
      {
        probeHttp: async () => ({ version: '2.0.0' }),
        resolveWsUrl: async () => 'ws://127.0.0.1:9119/api/ws?token=ok',
        probeWs: async () => ({ ok: true })
      }
    )

    expect(result.layer).toBe('connected')
    expect(result.httpOk).toBe(true)
    expect(result.wsOk).toBe(true)
    expect(result.version).toBe('2.0.0')
    expect(result.detail).toBeNull()
  })
})
