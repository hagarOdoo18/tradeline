"""Read proof bytes independently of the web client's binary size context."""


def attachment_bytes(attachment):
    # Keep the caller's access rights and company scope. Disabling size-only
    # reads is necessary for JSON validation; it grants no attachment access.
    return attachment.with_context(bin_size=False, bin_size_raw=False,
        bin_size_datas=False, bin_size_db_datas=False).raw
