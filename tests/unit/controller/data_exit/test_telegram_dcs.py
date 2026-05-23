def test_flatten_combines_v4_and_v6():
    from mthydra.controller.data_exit.telegram_dcs import flatten_cidrs
    out = flatten_cidrs(v4=("1.0.0.0/8", "2.0.0.0/8"), v6=("::/0",))
    assert out == ["1.0.0.0/8", "2.0.0.0/8", "::/0"]


def test_flatten_rejects_invalid_cidr():
    import pytest
    from mthydra.controller.data_exit.telegram_dcs import flatten_cidrs
    with pytest.raises(ValueError, match="invalid CIDR"):
        flatten_cidrs(v4=("not-a-cidr",), v6=())


def test_flatten_empty_is_empty():
    from mthydra.controller.data_exit.telegram_dcs import flatten_cidrs
    assert flatten_cidrs(v4=(), v6=()) == []
