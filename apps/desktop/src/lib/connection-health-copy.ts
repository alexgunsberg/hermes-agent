import type { ConnectionHealthReport } from '@hermes/shared'
import { classifyConnectionHealth } from '@hermes/shared'

import type { Translations } from '@/i18n/types'

/** Map a health report (or free-text boot error) onto localized recovery copy. */
export function connectionHealthCopy(
  health: ConnectionHealthReport | null | undefined,
  t: Translations,
  fallbackError?: null | string
): { layer: ConnectionHealthReport['layer']; title: string; hint: string; detail: string | null } {
  const report =
    health ??
    classifyConnectionHealth({
      errorText: fallbackError
    })

  const copy = t.boot.health[report.layer]

  return {
    layer: report.layer,
    title: copy.title,
    hint: copy.hint,
    detail: report.detail
  }
}
