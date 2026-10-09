// Farm requests never open an OpenCode session or its conversation database.
import fs from 'node:fs/promises'
import * as errore from 'errore'
import { z } from 'zod'

export class FarmError extends errore.createTaggedError({
  name: 'FarmError',
  message: '$reason. Check the farm service and KIMAKI_FARM_CONFIG',
}) {}

const connectionSchema = z.object({
  url: z
    .string()
    .url()
    .refine((value) => {
      const url = new URL(value)
      return (
        !url.username &&
        !url.password &&
        !url.search &&
        !url.hash &&
        (url.protocol === 'https:' ||
          (url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)))
      )
    }, 'Use HTTPS or a loopback SSH tunnel'),
  token: z.string().min(32),
  channels: z.array(z.string().regex(/^\d+:\d+$/)).default([]),
})
const configSchema = z.record(z.string(), connectionSchema)
export type FarmConnection = z.infer<typeof connectionSchema>

export async function loadFarmConfig() {
  const filename = process.env.KIMAKI_FARM_CONFIG
  if (!filename) return new FarmError({ reason: 'Farm integration is not configured' })
  const stat = await fs
    .stat(filename)
    .catch((cause) => new FarmError({ reason: 'Cannot inspect farm configuration', cause }))
  if (stat instanceof Error) return stat
  if (process.platform !== 'win32' && (stat.mode & 0o077) !== 0)
    return new FarmError({
      reason: 'Farm configuration must be private; run chmod 600 on the config file',
    })
  const raw = await fs
    .readFile(filename, 'utf8')
    .catch((cause) => new FarmError({ reason: 'Cannot read farm configuration', cause }))
  if (raw instanceof Error) return raw
  const parsed = errore.try(
    () => JSON.parse(raw) as unknown,
    (cause) => new FarmError({ reason: 'Invalid farm configuration JSON', cause }),
  )
  if (parsed instanceof Error) return parsed
  const result = configSchema.safeParse(parsed)
  if (!result.success) return new FarmError({ reason: 'Invalid farm configuration schema' })
  return result.data
}

export async function farmRequest({
  connection,
  path,
  body,
}: {
  connection: FarmConnection
  path: string
  body?: object
}) {
  const response = await fetch(connection.url.replace(/\/$/, '') + path, {
    method: body ? 'POST' : 'GET',
    headers: { authorization: `Bearer ${connection.token}`, 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(10_000),
    redirect: 'error',
  }).catch((cause) => new FarmError({ reason: 'Farm request unavailable', cause }))
  if (response instanceof Error) return response
  // Never log response bodies: reverse proxies may include credentials or HTML.
  if (!response.ok) return new FarmError({ reason: `Farm API returned HTTP ${response.status}` })
  const value = (await response
    .json()
    .catch((cause) => new FarmError({ reason: 'Invalid farm API response', cause }))) as unknown
  if (value instanceof Error) return value
  return { value }
}

export function resolveFarmChannel({
  config,
  guildId,
  channelId,
  parentId,
}: {
  config: z.infer<typeof configSchema>
  guildId: string
  channelId: string
  parentId: string | null
}) {
  const matches = Object.values(config).filter(
    (entry) =>
      entry.channels.includes(`${guildId}:${channelId}`) ||
      (parentId !== null && entry.channels.includes(`${guildId}:${parentId}`)),
  )
  if (matches.length !== 1)
    return new FarmError({ reason: 'Channel must map to exactly one farm project' })
  return matches[0]!
}

const jobSchema = z.object({
  id: z.string().uuid(),
  state: z.string(),
  operation: z.string(),
  serial: z.string(),
})
const devicesSchema = z.array(
  z.object({ name: z.string(), serial: z.string(), status: z.string(), quarantined: z.boolean() }),
)

export function formatFarmResponse(value: unknown) {
  const job = jobSchema.safeParse(value)
  if (job.success)
    return `Job ${job.data.id}: ${job.data.state}\n${job.data.operation} on ${job.data.serial}`
  const devices = devicesSchema.safeParse(value)
  if (devices.success) {
    const text = devices.data
      .slice(0, 20)
      .map((d) => `${d.name} (${d.serial}): ${d.status}${d.quarantined ? ' — quarantined' : ''}`)
      .join('\n')
    return (
      (text || 'No enrolled devices') +
      (devices.data.length > 20
        ? `\nShowing 20 of ${devices.data.length}; use the CLI for the full list.`
        : '')
    )
  }
  if (z.object({ cancel_requested: z.literal(true) }).safeParse(value).success)
    return 'Cancellation requested. Running actions may require reconciliation.'
  return 'Farm request accepted. Use job status to verify the outcome.'
}
