import { describe, expect, it } from 'vitest'

import { en } from '@/i18n/en'
import { connectionHealthCopy } from '@/lib/connection-health-copy'

describe('connectionHealthCopy', () => {
  it('maps endpoint_unreachable onto architecture-neutral recovery copy', () => {
    const copy = connectionHealthCopy(
      {
        layer: 'endpoint_unreachable',
        code: 'health.endpoint_unreachable',
        detail: 'fetch failed: ECONNREFUSED 127.0.0.1:9119'
      },
      en
    )

    expect(copy.layer).toBe('endpoint_unreachable')
    expect(copy.title).toBe(en.boot.health.endpoint_unreachable.title)
    expect(copy.hint).toMatch(/Retry|Gateway settings/i)
    expect(copy.hint).not.toMatch(/ssh|tailscale|expose|9119 port/i)
    expect(copy.detail).toContain('127.0.0.1:9119')
  })

  it('maps http_ok_ws_rejected without teaching destructive recovery', () => {
    const copy = connectionHealthCopy(
      {
        layer: 'http_ok_ws_rejected',
        code: 'health.http_ok_ws_rejected',
        detail: 'Timed out waiting for the WebSocket to open.'
      },
      en
    )

    expect(copy.title).toBe(en.boot.health.http_ok_ws_rejected.title)
    expect(copy.hint).toMatch(/Gateway settings|sign in|Retry/i)
    expect(copy.hint).not.toMatch(/kill|rm -|firewall-cmd|ufw/i)
  })

  it('falls back to classifying free-text boot errors', () => {
    const copy = connectionHealthCopy(
      null,
      en,
      'Reached the gateway over HTTP, but the live WebSocket (/api/ws) connection failed'
    )

    expect(copy.layer).toBe('http_ok_ws_rejected')
    expect(copy.title).toBe(en.boot.health.http_ok_ws_rejected.title)
  })
})
