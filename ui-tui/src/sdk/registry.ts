import type { WidgetApp } from './types.js'

type WidgetOwner = string | symbol

interface WidgetRegistration {
  app: WidgetApp<never>
  owner: WidgetOwner
}

const DEFAULT_OWNER = Symbol('built-in-widget')
const registrations = new Map<string, WidgetRegistration[]>()

function registerWidgetApp<S>(owner: WidgetOwner, app: WidgetApp<S>): WidgetApp<S> {
  const stack = (registrations.get(app.id) ?? []).filter(entry => entry.owner !== owner)

  stack.push({ app: app as WidgetApp<never>, owner })
  registrations.set(app.id, stack)

  return app
}

/** Identity helper that pins the state type, then registers. Last writer
 *  wins so a user/plugin app can shadow a built-in of the same id. */
export function defineWidgetApp<S>(app: WidgetApp<S>): WidgetApp<S> {
  return registerWidgetApp(DEFAULT_OWNER, app)
}

/** Register a user/plugin widget under stable ownership so removing it can
 * restore any built-in or earlier plugin it shadowed. */
export function defineOwnedWidgetApp<S>(owner: string, app: WidgetApp<S>): WidgetApp<S> {
  return registerWidgetApp(owner, app)
}

export const getWidgetApp = (id: string): undefined | WidgetApp<never> => registrations.get(id)?.at(-1)?.app

/** Remove every registration belonging to one user file. */
export function removeOwnedWidgetApps(owner: string): string[] {
  const affected: string[] = []

  for (const [id, stack] of registrations) {
    const next = stack.filter(entry => entry.owner !== owner)

    if (next.length === stack.length) {
      continue
    }

    affected.push(id)

    if (next.length) {
      registrations.set(id, next)
    } else {
      registrations.delete(id)
    }
  }

  return affected
}

/** Unregister (user-widget file deleted). Built-ins never call this. */
export const removeWidgetApp = (id: string): boolean => registrations.delete(id)

/** All registered apps, id-sorted — the registry IS the catalog: slash
 *  commands and `/` completions derive from it, nothing is hardcoded. */
export const listWidgetApps = (): WidgetApp<never>[] =>
  [...registrations.values()]
    .map(stack => stack.at(-1)?.app)
    .filter((app): app is WidgetApp<never> => Boolean(app))
    .sort((a, b) => a.id.localeCompare(b.id))
