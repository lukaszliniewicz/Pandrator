import random

from pandrator.web.temporal_overlap import overlap_indexes


def _brute_force(
    source_bounds: list[tuple[int, int]], query_bounds: list[tuple[int, int]]
) -> list[list[int]]:
    return [
        [
            source_index
            for source_index, (source_start, source_end) in enumerate(source_bounds)
            if source_end > source_start
            and query_end > query_start
            and min(source_end, query_end) > max(source_start, query_start)
        ]
        for query_start, query_end in query_bounds
    ]


def test_overlap_indexes_handle_nested_touching_and_duplicate_intervals():
    source_bounds = [
        (0, 10),
        (2, 3),
        (9, 12),
        (10, 10),
        (12, 8),
        (2, 3),
        (4, 8),
    ]
    query_bounds = [
        (0, 2),
        (2, 4),
        (3, 9),
        (10, 12),
        (8, 12),
        (5, 5),
        (3, 2),
    ]

    assert overlap_indexes(source_bounds, query_bounds) == _brute_force(
        source_bounds, query_bounds
    )


def test_overlap_indexes_handle_empty_source_or_query_sets():
    assert overlap_indexes([], []) == []
    assert overlap_indexes([], [(0, 1)]) == [[]]
    assert overlap_indexes([(0, 1)], []) == []


def test_overlap_indexes_match_fixed_seed_interval_reference():
    randomizer = random.Random(20261031)
    for _ in range(100):
        source_bounds = [
            (start := randomizer.randint(-10, 30), start + randomizer.randint(-3, 15))
            for _ in range(randomizer.randint(0, 40))
        ]
        query_bounds = [
            (start := randomizer.randint(-10, 30), start + randomizer.randint(-3, 15))
            for _ in range(randomizer.randint(0, 35))
        ]

        assert overlap_indexes(source_bounds, query_bounds) == _brute_force(
            source_bounds, query_bounds
        )


def test_overlap_indexes_scale_to_disjoint_1000_and_5000_row_sets():
    for count in (1_000, 5_000):
        source_bounds = [(index * 10, index * 10 + 4) for index in range(count)]
        query_bounds = [(index * 10 + 2, index * 10 + 8) for index in range(count)]

        assert overlap_indexes(source_bounds, query_bounds) == [
            [index] for index in range(count)
        ]
