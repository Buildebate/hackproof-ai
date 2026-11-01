from scratch_e2e.calc import total


def test_total_sums_without_surcharge():
    assert total([1, 2, 3]) == 6
