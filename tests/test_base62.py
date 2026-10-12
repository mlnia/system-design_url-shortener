import pytest

from app import base62


@pytest.mark.parametrize(
    ("number", "encoded"),
    [(0, "0"), (9, "9"), (10, "a"), (35, "z"), (36, "A"), (61, "Z"), (62, "10"), (3843, "ZZ")],
)
def test_known_values(number, encoded):
    assert base62.encode(number) == encoded
    assert base62.decode(encoded) == number


def test_book_example():
    # System Design Interview, ch. 8: 11157 (base10) == "2TX" (base62).
    assert base62.encode(11157) == "2TX"


@pytest.mark.parametrize("number", [1, 12345, 2**41, 2**63 - 1])
def test_roundtrip(number):
    assert base62.decode(base62.encode(number)) == number


def test_seven_chars_cover_3_5_trillion_urls():
    assert len(base62.encode(62**7 - 1)) == 7
    assert 62**7 > 3.5e12


def test_rejects_invalid_input():
    with pytest.raises(ValueError):
        base62.encode(-1)
    with pytest.raises(ValueError):
        base62.decode("")
    with pytest.raises(ValueError):
        base62.decode("abc$")
