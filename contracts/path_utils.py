from __future__ import annotations

from typing import Any

MISSING = object()


def _split_path(path: str) -> list[str]:
    return [part for part in path.split(".") if part]


def _extract_from_value(value: Any, parts: list[str]) -> list[Any]:
    if not parts:
        return [value]

    head, *tail = parts

    if head.endswith("[*]"):
        key = head[:-3]

        if not isinstance(value, dict):
            return []

        arr = value.get(key, MISSING)
        if arr is MISSING or arr is None or not isinstance(arr, list):
            return []

        out: list[Any] = []
        for item in arr:
            out.extend(_extract_from_value(item, tail))
        return out

    if head == "__len__":
        if isinstance(value, (list, dict, str)):
            return [len(value)]
        return []

    if not isinstance(value, dict):
        return []

    child = value.get(head, MISSING)
    if child is MISSING:
        return []

    return _extract_from_value(child, tail)


def extract_values(record: dict[str, Any], path: str) -> list[Any]:
    return _extract_from_value(record, _split_path(path))


def extract_first(record: dict[str, Any], path: str) -> Any | None:
    values = extract_values(record, path)
    return values[0] if values else None


def path_exists_in_record(record: dict[str, Any], path: str) -> bool:
    return len(extract_values(record, path)) > 0


def path_exists_in_dataset(records: list[dict[str, Any]], path: str) -> bool:
    return any(path_exists_in_record(record, path) for record in records)
