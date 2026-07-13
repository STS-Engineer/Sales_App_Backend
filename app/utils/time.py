from datetime import datetime, timedelta, timezone

# Business timezone (Tunisia, Africa/Tunis): fixed UTC+1, no DST.
_LOCAL_UTC_OFFSET = timedelta(hours=1)


def local_now() -> datetime:
    """Current local wall-clock time, tagged as UTC.

    The DB session timezone is UTC, so a true UTC instant would read 1 hour
    behind the local wall clock when inspected directly (e.g. via psql/pgAdmin).
    This stores the local time value labeled as UTC so raw reads match the clock.
    """
    return datetime.now(timezone.utc) + _LOCAL_UTC_OFFSET
