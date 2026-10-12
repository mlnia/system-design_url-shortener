"""Base 62 conversion ([0-9a-zA-Z]), used to turn numeric IDs into short codes."""

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
BASE = len(ALPHABET)
_INDEX = {char: i for i, char in enumerate(ALPHABET)}


def encode(number: int) -> str:
    if number < 0:
        raise ValueError("base62 can only encode non-negative integers")
    if number == 0:
        return ALPHABET[0]
    chars = []
    while number:
        number, remainder = divmod(number, BASE)
        chars.append(ALPHABET[remainder])
    return "".join(reversed(chars))


def decode(text: str) -> int:
    if not text:
        raise ValueError("cannot decode an empty string")
    number = 0
    for char in text:
        try:
            number = number * BASE + _INDEX[char]
        except KeyError:
            raise ValueError(f"invalid base62 character: {char!r}") from None
    return number
