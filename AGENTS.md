# Tradeline project workflow

- This project uses only the existing `stage` and `production` branches for the development and release workflow.
- Never create new Git branches in this project, including feature, temporary, test, or `codex/` branches.
- Develop and test changes on the existing `stage` branch. If it is already checked out in another worktree, use that existing checkout.
- Promote tested changes from `stage` to the existing `production` branch.
- Preserve uncommitted work and existing production changes when synchronizing the two branches.
- Use Karim Sharaf (`114778393+karimsharaf04@users.noreply.github.com`, GitHub `karimsharaf04`) for both commit author and committer in this repository. Honor the repository Git identity; do not override it to Codex.

## ETA integration: Ahmed Salama only

- Use only Ahmed Salama's `egyptian_electronic_invoice` integration and its configured company credentials and signer for ETA sending.
- Do not use or enable Odoo's native ETA connector (`l10n_eg_edi_eta` transport, native authentication, thumb-drive signing, submission, status or cancellation). Never use it as a fallback.
- `tradeline_eta_reliability` may queue, audit, reconcile and retry Ahmed Salama integration documents; it must not activate a second sender or bypass the configured integration with ad hoc direct submission scripts.
- Native receipt fields and local formatting helpers may remain for compatibility; they do not authorize native connector network calls.
- Keep automatic, manual, bulk and retry actions on the same Ahmed Salama sending path, preserving durable request markers, receipt reconciliation and ETA cooldowns.
- When changing ETA code, verify that native connector calls stay blocked and that native EDI actions delegate to the Ahmed Salama workflow without submitting another copy.
