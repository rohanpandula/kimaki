# Implementation review and validation

Reviewed on 2026-10-09 against fork base `b49ac310`. This is an opt-in implementation,
not a deployment, a history migration, or certification of the existing farm.

## Review rounds

The implementation received repeated read-only reviews from GPT-6.1 Sol and source
reviews through Vercel AI Gateway from DeepSeek V4.1 Flash, Qwen 3.8 Max, Claude
Sonnet 5.5 and Claude Opus 5.5. Review inputs contained the new source and sanitized
operational constraints, not conversation history, farm databases or credentials.
Initial Qwen/Sonnet calls exhausted their response budget; focused reruns returned
usable reviews. Opus's initial response was truncated; its final review completed.
All four final gateway reviews completed normally.

Sol reviewed the final follow-up changes and found no new blockers. Qwen and Opus
reported no confirmed blockers in their final full-source reviews. DeepSeek and
Sonnet returned additional findings; these were assessed individually rather than
treated as unanimous approval.

| Finding | Resolution |
| --- | --- |
| Expiry rolled back with rejected stale requests | Recovery commits separately; PostgreSQL regression test. |
| Uncertain execution released concurrency | Unknown outcomes reserve both serial and capacity until reconciliation. |
| Absent Android package looked like execution failure | Exact package-list matching; worker execution test. |
| Suspension could resume commands after lease expiry | Sticky dual-clock deadline and synchronous renewal before each command. |
| ADB inherited API credentials | Minimal child environment allowlist. |
| Failed WiFi probe hid healthy USB inventory | Isolated bounded fallback probes; USB remains authoritative. |
| Pre-execution cancellation/lost acknowledgment lost evidence | Persist every completion before delivery; durable retry tests. |
| Stale or corrupt local receipts blocked later receipts | Separate evidence endpoint, permanent-error handling, retained operator files. |
| Weak credential validation and Bearer parsing | Validate subjects/roles/random-token shape at startup; strict scheme and byte comparison. |
| Unknown drain target silently succeeded | Explicit 404 with API regression tests. |
| Disabled Discord delivery was hard to detect | Metric plus alert; permanent 4xx disables delivery, 429 remains retryable. |
| Polling competed with heartbeats for admission lock | Status and inventory reads use ordinary read transactions. |
| Result rename was not directory-synced | POSIX directory fsync; interrupted temp files retained. |
| OS kill can orphan an ADB client | Explicit rollout constraint: no automatic release; operator must terminate surviving executors before reconciliation. |

## Findings deliberately not implemented

* Moving the local deadline to HTTP response time would extend permission beyond
  the known server lease. Measuring from request start deliberately errs early.
* An incomplete scan sets every owned device to UNKNOWN; instance replacement also
  resets inventory. Claims already check current instance, fresh per-device
  observations and eligible status. A separate `last_scan` check is redundant.
* Exact completion retries already match job, worker, instance, token, outcome and
  result and do not mutate state. They are safe after expiration.
* A cancellation racing the first command may conservatively quarantine a device.
  Avoiding false certainty takes precedence over automatically releasing capacity.
* Worker boot is authorized by that worker's credential. Compromised credentials
  can disrupt its own worker and must be revoked; this is a trusted-farm service.
* The notification sender conservatively applies every 429 delay globally. It uses
  a dedicated bot, so the tradeoff is delayed summaries across projects, not shared
  throttling of normal Kimaki chat. This is documented.

## Validation

* CLI TypeScript typecheck: passed after changes.
* Focused farm client/command tests: 5 passed.
* Existing Discord command registration test: 1 passed.
* Python unit and real PostgreSQL 16 integration tests: **28 passed**. PostgreSQL
  ran in a disposable, loopback-only test instance, separate from production.
* Full CLI suite: run on Windows; final results recorded in the PR description.
  Failures observed include Windows path/escaping differences, EPERM fixture cleanup,
  and process-signal assumptions. OpenCode-dependent e2e cases skip when the test
  runtime cannot start. Unrelated snapshot rewrites are excluded from this change.
* No live Discord messages or phone actions were used as tests. Docker deployment,
  macOS LaunchAgent installation and the five-phone hardware pilot remain unverified.
* GitHub rejected the initial push because its OAuth credential lacks `workflow`
  scope. The CI definition is included as an inactive deployment template instead;
  no active GitHub Actions workflow is added by this PR.

Before production, run the documented pilot and fault scenarios, verify removal of
legacy ADB access, rehearse backup/restore, and measure heartbeat latency under the
expected polling load. This PR neither alters nor repairs existing OpenCode history.
