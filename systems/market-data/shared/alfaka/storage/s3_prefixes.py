import os


DEFAULT_S3_ARCHIVE_ROOT_PREFIX = "market-data/dev/helixho"


def clean_s3_prefix(value):
    return str(value or "").strip().strip("/")


def s3_archive_root_prefix(environ=None):
    environ = environ or os.environ
    return clean_s3_prefix(environ.get("S3_ARCHIVE_ROOT_PREFIX")) or DEFAULT_S3_ARCHIVE_ROOT_PREFIX


def default_s3_archive_prefix(kind, environ=None):
    root = s3_archive_root_prefix(environ)
    suffixes = {
        "final": "final",
        "manifest": "manifest",
        "backfill_processed": "backfill/processed",
    }
    if kind not in suffixes:
        raise ValueError(f"Unsupported S3 archive prefix kind: {kind}")
    return f"{root}/{suffixes[kind]}"


def first_configured_prefix(names, default, environ=None):
    environ = environ or os.environ
    for name in names:
        value = clean_s3_prefix(environ.get(name))
        if value:
            return value
    return default
