import { afterEach, expect, test, vi } from 'vitest'
import { farmRequest, formatFarmResponse, resolveFarmChannel } from './farm-client.js'
import { buildFarmCommand } from './commands/farm.js'

const connection = { url: 'http://127.0.0.1:8090', token: 'test-only'.repeat(8), channels: ['1:2'] }
afterEach(() => vi.unstubAllGlobals())

test('channel routing fails closed for missing or ambiguous project mapping', () => {
  expect(
    resolveFarmChannel({
      config: { one: connection },
      guildId: '1',
      channelId: '3',
      parentId: '2',
    }),
  ).toEqual(connection)
  expect(
    resolveFarmChannel({
      config: { one: connection },
      guildId: '9',
      channelId: '2',
      parentId: null,
    }),
  ).toBeInstanceOf(Error)
  expect(
    resolveFarmChannel({
      config: { one: connection, two: connection },
      guildId: '1',
      channelId: '2',
      parentId: null,
    }),
  ).toBeInstanceOf(Error)
})

test('API submission preserves idempotency and never follows credential redirects', async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValue(new Response(JSON.stringify({ state: 'queued' }), { status: 200 }))
  vi.stubGlobal('fetch', fetchMock)
  const body = { idempotency_key: 'stable-retry-key', operation: 'device-info', serial: 'SERIAL' }
  expect(await farmRequest({ connection, path: '/v1/jobs', body })).toEqual({
    value: { state: 'queued' },
  })
  expect(fetchMock).toHaveBeenCalledWith(
    'http://127.0.0.1:8090/v1/jobs',
    expect.objectContaining({ redirect: 'error', body: JSON.stringify(body) }),
  )
})

test('errors do not expose response bodies or authorization credentials', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(new Response('secret private proxy output', { status: 503 })),
  )
  const result = await farmRequest({ connection, path: '/v1/devices' })
  expect(result).toBeInstanceOf(Error)
  expect(String(result)).toContain('HTTP 503')
  expect(String(result)).not.toContain('secret')
})

test('farm command is guild-only with Manage Server default permissions', () => {
  const command = buildFarmCommand().toJSON()
  expect(command.dm_permission).toBe(false)
  expect(command.default_member_permissions).toBe('32')
  expect(command.options?.map((o) => o.name)).toEqual(['devices', 'status', 'cancel', 'submit'])
})

test('device summaries surface degraded transport and quarantine', () => {
  expect(
    formatFarmResponse([
      { name: 'Phone', serial: 'SERIAL', status: 'WIFI_FALLBACK', quarantined: true },
    ]),
  ).toMatchInlineSnapshot(`"Phone (SERIAL): WIFI_FALLBACK — quarantined"`)
})
