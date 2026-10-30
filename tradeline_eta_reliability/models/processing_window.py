"""Database-bound owner cutoff, checked without the configuration cache."""
from datetime import datetime, timedelta


class EtaStopped(Exception):
    """An owner pause is not an invoice error or an invitation to retry."""

    def __init__(self, message, planned=True):
        super().__init__(message)
        self.planned = planned


def check_processing_window(env, now=None, drain_seconds=60):
    # Direct reads make the cutoff effective for already-running workers too.
    env.cr.execute("SELECT key, value FROM ir_config_parameter WHERE key IN %s", [(
        'tradeline_eta_reliability.processing_cutoff_database',
        'tradeline_eta_reliability.processing_cutoff_utc',
        'tradeline_eta_reliability.owner_cutoff_utc',
        'tradeline_eta_reliability.owner_drain_utc',
    )])
    values = dict(env.cr.fetchall())
    if values.get('tradeline_eta_reliability.processing_cutoff_database') != env.cr.dbname:
        return
    try:
        explicit = values.get('tradeline_eta_reliability.processing_cutoff_utc')
        deadline = datetime.strptime(explicit or values.get(
            'tradeline_eta_reliability.owner_cutoff_utc', ''), '%Y-%m-%d %H:%M:%S')
        start_deadline = deadline - timedelta(seconds=drain_seconds)
        # The owner's operational stop and the transport guard share one
        # date treatment, including a new approved day after a manual resume.
        if not explicit and drain_seconds:
            drain = values.get('tradeline_eta_reliability.owner_drain_utc')
            if drain:
                drain = datetime.strptime(drain, '%Y-%m-%d %H:%M:%S')
                if drain > deadline:
                    raise ValueError('Drain is after the hard deadline')
                start_deadline = min(start_deadline, drain)
    except (ValueError, TypeError) as exc:
        raise EtaStopped('ETA is paused: the owner cutoff needs review.', planned=False) from exc
    if (now or datetime.utcnow()) >= start_deadline:
        raise EtaStopped('ETA is paused at the owner cutoff. Explicit approval is required to resume.')
