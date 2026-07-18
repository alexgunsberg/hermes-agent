import { atom } from 'nanostores'

import { classifyConnectionHealth, type ConnectionHealthReport } from '@hermes/shared'

import type { DesktopBootProgress, DesktopConnectionHealthSummary } from '@/global'
import { translateNow } from '@/i18n'

export type BootHealth = DesktopConnectionHealthSummary | ConnectionHealthReport

export interface DesktopBootState extends DesktopBootProgress {
  visible: boolean
  health?: BootHealth | null
}

const INITIAL_BOOT_STATE: DesktopBootState = {
  error: null,
  fakeMode: false,
  health: null,
  message: translateNow('boot.steps.startingHermesDesktop'),
  phase: 'renderer.init',
  progress: 2,
  running: true,
  timestamp: Date.now(),
  visible: true
}

export const $desktopBoot = atom<DesktopBootState>(INITIAL_BOOT_STATE)

function clampProgress(value: number) {
  if (!Number.isFinite(value)) {
    return 0
  }

  return Math.max(0, Math.min(100, Math.round(value)))
}

export function applyDesktopBootProgress(progress: DesktopBootProgress) {
  const current = $desktopBoot.get()
  const nextProgress = clampProgress(progress.progress)
  const mergedProgress = progress.running ? Math.max(current.progress, nextProgress) : nextProgress

  $desktopBoot.set({
    ...current,
    ...progress,
    error: progress.error ?? null,
    health: progress.health !== undefined ? progress.health : current.health,
    progress: mergedProgress,
    visible: progress.running || mergedProgress < 100 || Boolean(progress.error)
  })
}

export function setDesktopBootStep(step: {
  phase: string
  message: string
  progress: number
  running?: boolean
  fakeMode?: boolean
  error?: string | null
  health?: BootHealth | null
}) {
  const current = $desktopBoot.get()
  applyDesktopBootProgress({
    error: step.error ?? null,
    fakeMode: step.fakeMode ?? current.fakeMode,
    health: step.health !== undefined ? step.health : current.health,
    message: step.message,
    phase: step.phase,
    progress: step.progress,
    running: step.running ?? true,
    timestamp: Date.now()
  })
}

export function completeDesktopBoot(message = translateNow('boot.ready')) {
  const current = $desktopBoot.get()
  $desktopBoot.set({
    ...current,
    error: null,
    health: null,
    message,
    phase: 'renderer.ready',
    progress: 100,
    running: false,
    timestamp: Date.now(),
    visible: false
  })
}

export function failDesktopBoot(message: string, health?: BootHealth | null) {
  const current = $desktopBoot.get()
  const resolvedHealth = health !== undefined ? health : classifyConnectionHealth({ errorText: message })

  $desktopBoot.set({
    ...current,
    error: message,
    health: resolvedHealth,
    message: translateNow('boot.desktopBootFailedWithMessage', message),
    phase: 'renderer.error',
    progress: clampProgress(current.progress),
    running: false,
    timestamp: Date.now(),
    visible: true
  })
}
