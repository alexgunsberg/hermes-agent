export interface HttpStatusError extends Error {
  statusCode: number
}

export function createHttpStatusError(statusCode: number, detail: string): HttpStatusError {
  const error = new Error(`${statusCode}: ${detail}`) as HttpStatusError

  error.statusCode = statusCode

  return error
}