from reconcile.alloc_rules import allocate_with_reference, fifo_allocate, split_shares


def test_split_shares_sums_exactly():
    for net in (1, 99, 100_001, 237_755):
        shares = split_shares(net, [("dept", 83.0), ("resident", 17.0)])
        assert sum(shares) == net


def test_split_shares_order_is_independent_of_input_order():
    a = split_shares(100_001, [("dept", 70.0), ("resident", 30.0)])
    b = split_shares(100_001, [("resident", 30.0), ("dept", 70.0)])
    assert a == b[::-1]


def test_fifo_conserves_cents_and_fills_oldest_first():
    edges, left = fifo_allocate(250, [("jan", 100), ("feb", 100), ("mar", 100)])
    assert edges == [("jan", 100), ("feb", 100), ("mar", 50)]
    assert left == 0
    edges, left = fifo_allocate(350, [("jan", 100), ("feb", 100)])
    assert sum(c for _, c in edges) + left == 350
    assert left == 150


def test_reference_beats_fifo():
    edges, left = allocate_with_reference(100, [("mar", 100)], [("jan", 100), ("mar", 100)])
    assert edges == [("mar", 100)] and left == 0
