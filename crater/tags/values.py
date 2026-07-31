"""Coercion between tag values as strings (CSV, change log) and typed values."""

from crater.tags.models import TrackTags

_INT_FIELDS = {"release_year", "release_month", "release_day", "track_no"}
_REAL_FIELDS = {"bpm"}


def to_db_value(field: str, str_val: str | None) -> object:
    if str_val is None:
        return None
    if field in _INT_FIELDS:
        try:
            return int(str_val)
        except ValueError:
            return None
    if field in _REAL_FIELDS:
        try:
            return float(str_val)
        except ValueError:
            return None
    return str_val


def current_str(tags: TrackTags, field: str) -> str | None:
    val = getattr(tags, field)
    if val is None:
        return None
    if field in _REAL_FIELDS and float(val).is_integer():
        return str(int(val))
    return str(val)


def apply_changes_to_tags(
    current: TrackTags, updates: dict[str, str | None]
) -> TrackTags:
    kwargs = {f: getattr(current, f) for f in current.__dataclass_fields__}
    for f, new_val in updates.items():
        kwargs[f] = to_db_value(f, new_val)
    return TrackTags(**kwargs)
