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

The external free-account sign-in email has been requested. Activation is
pending confirmation of the provider's account terms. No external check or
outage notifications are active yet.

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
enable monitoring. Connect an external provider check and test missing-ping
and recovery notification delivery to both intended recipients. The external
account/check and actual email delivery were not verified by the tests above.
