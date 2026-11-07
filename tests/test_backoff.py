import pytest

from core.backoff import ExponentialBackoff


def test_delays_double_up_to_ceiling():
    """Пауза удваивается и упирается в потолок."""
    backoff = ExponentialBackoff(start=1, ceiling=30)

    delays = [backoff.next_delay() for _ in range(7)]

    assert delays == [1, 2, 4, 8, 16, 30, 30]


def test_reset_returns_start_delay():
    """После сброса последовательность пауз начинается заново."""
    backoff = ExponentialBackoff(start=1, ceiling=30)
    for _ in range(4):
        backoff.next_delay()

    backoff.reset()

    assert [backoff.next_delay() for _ in range(3)] == [1, 2, 4]


@pytest.mark.parametrize("start, ceiling", [(0, 30), (-1, 30), (5, 2)])
def test_rejects_invalid_bounds(start, ceiling):
    """Нулевая, отрицательная или выше потолка стартовая пауза отвергается."""
    with pytest.raises(ValueError):
        ExponentialBackoff(start=start, ceiling=ceiling)
