"""Find strict overlaps between source and query intervals."""

from collections.abc import Sequence


def overlap_indexes(
    source_bounds: Sequence[tuple[int, int]],
    query_bounds: Sequence[tuple[int, int]],
) -> list[list[int]]:
    """Return source indexes overlapping each query, ordered by source index.

    Empty and non-positive intervals are ignored. Intervals that only touch at
    an endpoint do not overlap.
    """
    overlaps: list[list[int]] = [[] for _ in query_bounds]
    events: list[tuple[int, int, int, int]] = []

    for source_index, (start, end) in enumerate(source_bounds):
        if end <= start:
            continue
        events.append((start, 1, 0, source_index))
        events.append((end, 0, 0, source_index))

    for query_index, (start, end) in enumerate(query_bounds):
        if end <= start:
            continue
        events.append((start, 1, 1, query_index))
        events.append((end, 0, 1, query_index))

    events.sort()
    active_sources: set[int] = set()
    active_queries: set[int] = set()
    for _time, event_kind, event_side, index in events:
        if event_kind == 0:
            if event_side == 0:
                active_sources.remove(index)
            else:
                active_queries.remove(index)
        elif event_side == 0:
            active_sources.add(index)
            for query_index in active_queries:
                overlaps[query_index].append(index)
        else:
            active_queries.add(index)
            overlaps[index].extend(active_sources)

    return [sorted(indexes) for indexes in overlaps]
