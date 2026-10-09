import { goke } from 'goke'
import { z } from 'zod'
import { farmRequest, loadFarmConfig } from '../farm-client.js'
import { createLogger, LogPrefix } from '../logger.js'

const cli = goke()
const logger = createLogger(LogPrefix.CLI)

cli
  .command('farm <action>', 'Submit or inspect farm jobs without starting OpenCode')
  .option('--project <name>', 'Project entry in KIMAKI_FARM_CONFIG')
  .option('--serial <serial>', 'Enrolled device hardware serial')
  .option('--operation <operation>', 'device-info, package-status, or reboot')
  .option('--package <package>', 'Android package for package-status')
  .option('--key <key>', 'Stable idempotency key (required for submit; reuse on retry)')
  .option('--job <id>', 'Job UUID for status/cancel')
  .option('--allow-wifi', 'Permit degraded WiFi ADB for this job')
  .action(async (action, options) => {
    const config = await loadFarmConfig()
    if (config instanceof Error) {
      logger.error(config.message)
      process.exitCode = 1
      return
    }
    const connection = options.project ? config[options.project] : null
    if (!connection) {
      logger.error('Use --project with a configured farm project')
      process.exitCode = 1
      return
    }
    if (!['submit', 'status', 'cancel', 'devices'].includes(action)) {
      logger.error('Use farm submit, status, cancel, or devices')
      process.exitCode = 1
      return
    }
    if (
      ['status', 'cancel'].includes(action) &&
      !z.string().uuid().safeParse(options.job).success
    ) {
      logger.error('Use --job <UUID>')
      process.exitCode = 1
      return
    }
    if (action === 'submit' && (!options.key || !options.serial || !options.operation)) {
      logger.error('Submit requires --key, --serial and --operation')
      process.exitCode = 1
      return
    }
    const path =
      action === 'devices'
        ? '/v1/devices'
        : action === 'submit'
          ? '/v1/jobs'
          : `/v1/jobs/${options.job}${action === 'cancel' ? '/cancel' : ''}`
    const body =
      action === 'submit'
        ? {
            serial: options.serial,
            operation: options.operation,
            package: options.package ?? null,
            idempotency_key: options.key,
            allow_wifi: options.allowWifi ?? false,
          }
        : action === 'cancel'
          ? {}
          : undefined
    const result = await farmRequest({ connection, path, body })
    if (result instanceof Error) {
      logger.error(result.message)
      process.exitCode = 1
      return
    }
    logger.log(JSON.stringify(result.value))
  })

export default cli
