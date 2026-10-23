# Operations warning system

This app detects symptoms that can delay reservations and stock publication.
It does not stop checkout, change stock/costs, restart jobs, terminate database
sessions, or guarantee that overselling cannot happen. It is independent of
the stock import/valuation workflows and is disabled when first installed.

## What it detects

| Condition | Initial thresholds / handling |
| --- | --- |
| Shopify inventory/order queue delayed | Warning after 3 minutes; critical after 10 |
| Critical cron disabled, failing, or overdue | Disabled/failing is critical; overdue uses the delay thresholds |
| Partial stock push even when batch says Done | Critical, based on persisted connector failure summaries |
| Individual order rejected inside a Done batch | Critical, based on the persisted per-order rejection log |
| Shopify order event blocked, failed, or waiting too long | Critical rejection; age-based delay warning |
| Database lock wait | Warning after 30 seconds; critical after 120 seconds |
| Idle transaction holding resources | Warning after 60 seconds; critical after 180 seconds |
| Long active SQL | Warning after 60 seconds; critical after 300 seconds |
| PostgreSQL connection pressure | Warning at 85%; critical at 95% |
| Excel import failed or stopped progressing | Immediate failure signal; age-based stalled-progress check |
| Failed outgoing mail / executive report | Critical persisted failure |
| Operational alert queued too long | Age-based warning/critical signal |
| Monitor/scheduler stopped | External heartbeat expires; authenticated HTTP health becomes 503 after 3 minutes |

Live conditions need two consecutive checks by default. Explicit persisted
failures alert on the first check. Checks run every minute, not continuously;
email delivery and host availability add time. These are detection targets,
not guaranteed response-time SLAs. A blocked scheduler can only be detected
reliably by the independent cloud monitor.

Odoo 18's scheduler keeps a coordination transaction open while its callback
runs on another connection. The known acquisition query is exempt from idle
alerts only while it holds scheduler metadata locks, no advisory locks, blocks
no sampled peer, and remains below the configured critical minutes. Business
transactions, aborted transactions, actual blockers and overlong coordination
transactions still alert. Database alerts include backend start time and the
last SQL relation name without copying SQL values or customer payloads.

An external **DOWN (received a failure signal)** means a confirmed operational
incident; it does not establish that the website was offline. An independent
HTTP poller is needed to measure public website downtime.

For slow attachment deletion, `maintenance/attachment-reference-indexes.sql`
ensures five references have supporting indexes using concurrent builds only
where needed, preserving Odoo's existing not-null partial indexes. Run it with
psql in autocommit after staging verification; it is deliberately not run
during module upgrade, where a large blocking index build could interrupt work.
The script verifies the expected foreign keys and usable indexes, and changes
no business records. `maintenance/check_attachment_rollout.py` captures row
counts, DML counters and deterministic business-record sample fingerprints for
before/after verification, and measures read-only reference lookup plans.

An old Multi Stock Update **Draft is not flagged as a stalled import**: drafts
may be legitimate work in progress. Multi Stock is an atomic transaction, so
its blocking/long-running SQL is detected by the database probes instead.

## Odoo setup

1. Install **Tradeline Operations Monitor**. Administrator access is required.
2. Open **Operations Monitor → Settings**. Enter only the intended two
   recipients and the existing executive-report sender/SMTP identity.
3. Click **Check Now (No Email)** and inspect **Operations Monitor → Incidents**.
   Review old Shopify rejection/mapping errors before activation. Failed jobs
   are checked over the last 24 hours. The HTML log scan starts at the last
   500 IDs and advances 500 rows per check, classifying errors up to 24 hours
   old. Historical errors before that initial tail require a separate audit;
   this monitor is for early detection after activation. Catch-up is reported
   explicitly if incoming logs exceed the scan budget.
4. Click **Send Test Email** and confirm receipt at both addresses. This queues
   one test; the separate alert sender runs within the next scheduler minute.
5. Enable monitoring. Emails group new/escalated incidents, repeat at most
   every 15 minutes per continuing incident, and report live-condition recovery.

Individual order/publication failures remain open until reviewed. Their absence
from a later scan or another successful batch is not proof they were repaired.
Open the referenced job/log/event, correct the cause, verify the affected
stock/order, then **Mark Reviewed / Resolved**. New failure evidence reopens it.

Shopify order-import and partial-inventory incidents show the latest referenced
**Shopify Log Message** directly below **What Was Detected**, with an
**Open Original Shopify Log** link. These are database records under
**Shopify → Configuration → Log Messages**; their IDs are not Odoo.sh server-log
line numbers. Existing incidents use their saved evidence IDs without rescanning
the log table. Messages are shown as plain text (up to 8,000 characters, with an
explicit truncation notice); the original log contains the full message.
Deleted/inaccessible logs are reported as unavailable. Viewing the message
respects connector log permissions. Original messages are not copied to alert
emails or the public health/heartbeat responses.

The probes run with a two-second SQL timeout and 100ms lock timeout each,
without taking business row locks. They read status/timestamps and relevant
log summaries, not stock import workbooks or job/order JSON payloads. Mail is
queued separately so SMTP waits do not hold the health-check transaction open.

## Independent outage email: cloud heartbeat

No extra always-on PC is needed for a hosted heartbeat check:

1. Create a check at [Healthchecks.io](https://healthchecks.io/docs/).
2. Set period **1 minute** and grace **2 minutes**.
3. Configure and verify email notifications for both intended recipients in
   the provider account. Complete any invitation/email confirmation yourself.
4. Copy the UUID-based `https://hc-ping.com/<UUID>` URL into Odoo's secret
   **Heartbeat URL** field. Do not paste it into Git, email bodies or URLs in
   documents; it allows signalling the check.
5. Confirm pings arrive after committed Odoo checks. The app sends `/fail` for
   confirmed critical conditions and success pings otherwise. No stock,
   customer, order, database, or recipient data is included in the ping.
6. On a test system, stop/pause the monitoring cron and confirm the provider
   emails both addresses when check-ins expire. Resume and verify recovery.

If Odoo, its database, cron workers, or its outgoing mail stop working, the
provider can send its own emails without depending on Odoo. A configured URL
alone is not proof that recipients or alert delivery were verified.

## Website/HTTP availability and response time

For independent frontend availability/latency checks, configure an HTTP monitor
such as [Better Stack](https://betterstack.com/docs/uptime/monitoring-start/).
Use `https://<production-host>/tradeline/operations/health`, with a secret
`Authorization: Bearer <Health Token>` header generated in Settings. HTTP 503
means confirmed critical operations status or a stale monitor; 401 means the
token/configuration is wrong. The response contains only overall status and
the monitoring timestamp. Do not put the token in the URL. The HTTP monitor
also detects timeouts, server errors, TLS/network failures, and slow responses
according to its configured thresholds.

Heartbeat expiry covers scheduler/database failure; HTTP polling covers the
case where Odoo cron is alive but HTTP workers or the public site are stuck.
Both can live in a hosted monitoring service; provider account activation and
recipient verification are external setup steps.

## Maintenance and limitations

Set **Maintenance Until** for intentional maintenance while the monitor is
running. It mutes Odoo notifications and sends success heartbeats during that
window. If Odoo itself will be offline, pause the corresponding external checks
at the provider for that window too; an offline server cannot send its own
maintenance signal.

This does not independently compare every accepted Shopify order against Odoo
or every live Shopify quantity against warehouse stock. A failure that leaves
no queue, event or persistent connector error can escape these checks; source
reconciliation is separate. CPU/RAM/disk metrics need the hosting provider's
infrastructure telemetry; database symptoms and HTTP latency are covered here.
Monitoring permissions must expose PostgreSQL sessions for the Odoo role;
otherwise the database probe is reported unavailable, not falsely healthy.

Tests simulate slow/failed probes, a blocking chain, partial Done-batch errors,
rejected orders, stalled imports, confirmation/escalation/reminders/recovery,
manual failure review, maintenance, authenticated health, and mail isolation.

## App menu visibility

The app and all its submenus are visible only to the built-in Administrator
account (`base.user_admin`), including in developer mode. Other Settings
administrators do not see the app. Existing model/API permissions and scheduled
operations are unchanged.
