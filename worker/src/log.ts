/**
 * Structured logging.
 *
 * Emitted as single-line JSON so Cloudflare Workers Logs indexes the fields
 * and they're filterable (`wrangler tail --format=json`, or the dashboard).
 *
 * Never log credentials: no API keys, access tokens, client secrets, or
 * authorization codes. Status codes and identifiers only.
 */

function emit(level: "info" | "error", event: string, fields: Record<string, unknown>): void {
  const line = JSON.stringify({ level, event, ...fields });
  if (level === "error") console.error(line);
  else console.log(line);
}

export function log(event: string, fields: Record<string, unknown> = {}): void {
  emit("info", event, fields);
}

export function logError(event: string, fields: Record<string, unknown> = {}): void {
  emit("error", event, fields);
}
