import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createHttpStatusError } from './http-error'

test('createHttpStatusError preserves the response status for auth classification', () => {
  const error = createHttpStatusError(401, 'session expired')

  assert.equal(error.message, '401: session expired')
  assert.equal(error.statusCode, 401)
})