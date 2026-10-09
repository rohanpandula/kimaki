import { MessageFlags, PermissionFlagsBits, SlashCommandBuilder } from 'discord.js'
import type { CommandContext } from './types.js'
import {
  farmRequest,
  formatFarmResponse,
  loadFarmConfig,
  resolveFarmChannel,
} from '../farm-client.js'
import { z } from 'zod'

export function buildFarmCommand() {
  return new SlashCommandBuilder()
    .setName('farm')
    .setDescription('Submit and inspect device farm jobs')
    .setDMPermission(false)
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageGuild)
    .addSubcommand((s) =>
      s.setName('devices').setDescription('List device observations for this project'),
    )
    .addSubcommand((s) =>
      s
        .setName('status')
        .setDescription('Inspect a job')
        .addStringOption((o) => o.setName('job').setDescription('Job UUID').setRequired(true)),
    )
    .addSubcommand((s) =>
      s
        .setName('cancel')
        .setDescription('Request cancellation; uncertain actions remain quarantined')
        .addStringOption((o) => o.setName('job').setDescription('Job UUID').setRequired(true)),
    )
    .addSubcommand((s) =>
      s
        .setName('submit')
        .setDescription('Queue a validated device operation')
        .addStringOption((o) =>
          o.setName('serial').setDescription('Enrolled hardware serial').setRequired(true),
        )
        .addStringOption((o) =>
          o
            .setName('operation')
            .setDescription('Operation')
            .setRequired(true)
            .addChoices(
              { name: 'Read device information', value: 'device-info' },
              { name: 'Check installed package', value: 'package-status' },
              { name: 'Reboot device', value: 'reboot' },
            ),
        )
        .addStringOption((o) =>
          o.setName('package').setDescription('Android package name for package-status'),
        )
        .addBooleanOption((o) =>
          o.setName('allow-wifi').setDescription('Explicitly permit degraded WiFi transport'),
        ),
    )
}

export async function handleFarmCommand({ command }: CommandContext) {
  // Check at execution time too; server command permission overrides are not authorization.
  if (!command.guildId || !command.memberPermissions?.has(PermissionFlagsBits.ManageGuild)) {
    await command.reply({
      content: 'Manage Server permission is required for farm commands.',
      flags: MessageFlags.Ephemeral,
    })
    return
  }
  await command.deferReply({ flags: MessageFlags.Ephemeral })
  const config = await loadFarmConfig()
  if (config instanceof Error) {
    await command.editReply(config.message)
    return
  }
  const channel = command.channel
  const connection = resolveFarmChannel({
    config,
    guildId: command.guildId,
    channelId: command.channelId,
    parentId: channel && 'parentId' in channel ? channel.parentId : null,
  })
  if (connection instanceof Error) {
    await command.editReply(connection.message)
    return
  }
  const action = command.options.getSubcommand()
  const jobId = command.options.getString('job')
  if ((action === 'status' || action === 'cancel') && !z.string().uuid().safeParse(jobId).success) {
    await command.editReply('Use a valid job UUID.')
    return
  }
  const path =
    action === 'devices'
      ? '/v1/devices'
      : action === 'submit'
        ? '/v1/jobs'
        : `/v1/jobs/${jobId}${action === 'cancel' ? '/cancel' : ''}`
  const body =
    action === 'submit'
      ? {
          serial: command.options.getString('serial', true),
          operation: command.options.getString('operation', true),
          package: command.options.getString('package'),
          allow_wifi: command.options.getBoolean('allow-wifi') ?? false,
          idempotency_key: command.id,
        }
      : action === 'cancel'
        ? {}
        : undefined
  const result = await farmRequest({ connection, path, body })
  await command.editReply({
    content: (result instanceof Error ? result.message : formatFarmResponse(result.value)).slice(
      0,
      1900,
    ),
    allowedMentions: { parse: [] },
  })
}
