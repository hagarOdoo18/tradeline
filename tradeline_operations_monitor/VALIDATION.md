# Validation — 17 October 2026

The app is deployed and installed on the neutralized **staging** database
`tradelinestores-stage-40023055`. Monitoring is disabled. This is not a claim
that production alerts or independent outage notifications are active.

## Deployment hold

The owner explicitly requested keeping this staged until an agreed quiet
period. Do not merge or push it to the production branch, install it on the
production database, enable production monitoring, or restart production
until that window is explicitly agreed. External checks must remain paused
until the production heartbeat is connected, except for a controlled alert
delivery test. No rollout time has been scheduled.

The external free Healthchecks account was activated after the owner's
explicit acceptance of its terms. The production check is configured for a
one-minute period and two-minute grace, and remains **paused and disconnected
from Odoo**. No production outage notifications are active yet.

## Independent provider delivery test

A separate check named **TEST ONLY — Operations alert delivery** received a
manual success ping and was deliberately allowed to expire. The provider
sent a DOWN email at **12:56:33 UTC**, after its one-minute period and
two-minute grace. A new manual success ping triggered an UP email at
**12:57:12 UTC**. Both messages were read back in the intended owner's Gmail
inbox, verifying actual provider delivery without stopping any Odoo service.
The test check was then paused too.

The second intended recipient has an email integration assigned to both
checks, but it is **Unconfirmed**. The provider sent its confirmation link;
that recipient must confirm it before her alert delivery can be tested.
Neither this provider-only test nor the isolated Odoo tests verifies the
future production Odoo-to-provider connection or production SMTP delivery.
The production ping URL has not been saved in staging or committed to Git.

## Completed verification

- 18 isolated Odoo 18 transaction tests passed, with no failed tests/errors.
- Coverage includes database lock-chain/idle-transaction detection, bounded
  failed probes, real PostgreSQL metadata queries, partial Done-batch stock
  failures, per-order rejection logs, paused/stalled imports, confirmation,
  escalation, reminder suppression, recovery, manual failure review, mail
  isolation/escaping, administrator permissions and secret health auth.
- The warning → suppress duplicate → recovery flow queued exactly one warning
  and one recovery message in the isolated test database; no real recipients
  were emailed by these tests.
- An initial real staging probe hit its two-second Shopify scan budget.
  The unindexed date/text scan was replaced with a bounded primary-key log
  scan. The next real staging check completed in **0.2377 seconds**, with no
  unavailable-probe incidents left unresolved.
- The staging probe correctly identified its intentionally disabled business
  crons and a persisted failed Shopify job. A neutralized staging snapshot is
  not evidence that these conditions currently hold in production.
- The deployed HTTP health route rejected an unauthenticated request with
  HTTP 401 and a minimal JSON response.
- The intended two recipients were checked against the connected account and
  existing executive-report settings, and saved to the disabled staging
  monitor. Email addresses and secrets are not committed as app defaults.

Before activation: deploy/install on production in the agreed window, review
the no-email baseline, send a test email and verify SMTP/inbox delivery, then
enable monitoring. Connect the prepared external provider check and verify
the first committed production heartbeat, missing-ping/recovery delivery to
both intended recipients, and the production Odoo SMTP channel. Keep external
checks paused until that rollout. Independent HTTP availability/latency polling
and host CPU/RAM/disk telemetry are not configured by this staging work.
