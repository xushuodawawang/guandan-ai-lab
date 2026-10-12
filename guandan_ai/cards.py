from collections import Counter
from typing import Iterable

RANKS = "23456789TJQKA"
SUITS = "SHCD"
CARD_TYPES = tuple(s + r for s in SUITS for r in RANKS) + ("SB", "HR")


def deck() -> list[str]:
    return list(CARD_TYPES) * 2


def validate_hand(cards: Iterable[str]) -> list[str]:
    hand = list(cards)
    counts = Counter(hand)
    bad = [c for c, n in counts.items() if c not in CARD_TYPES or n > 2]
    if bad:
        raise ValueError(f"非法或超过两副牌数量的牌: {bad}")
    return hand


def rank_value(rank: str, trump: str) -> int:
    if rank == "R":
        return 17
    if rank == "B":
        return 16
    if rank == trump:
        return 15
    return RANKS.index(rank) + 2


def hand_key(cards: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(cards))


def remove_cards(hand: Iterable[str], cards: Iterable[str]) -> list[str]:
    remaining = Counter(hand)
    used = Counter(cards)
    if any(remaining[c] < n for c, n in used.items()):
        raise ValueError("动作中包含手牌没有的牌")
    remaining.subtract(used)
    return list(remaining.elements())


def format_card(card: str) -> str:
    if card in ("SB", "HR"):
        return {"SB": "小王", "HR": "大王"}[card]
    return {"S": "♠", "H": "♥", "C": "♣", "D": "♦"}[card[0]] + card[1].replace("T", "10")


def wire_matrix(hand: Iterable[str]) -> list[int]:
    """Official two-deck bit encoding; low/high suit bits preserve duplicate cards."""
    out = [0] * 15
    suit_rows = {"H": 0, "S": 1, "C": 2, "D": 3}
    for c in validate_hand(hand):
        col = 13 if c == "SB" else 14 if c == "HR" else RANKS.index(c[1])
        row = 0 if c in ("SB", "HR") else suit_rows[c[0]]
        out[col] |= 1 << (row + 4) if out[col] & (1 << row) else 1 << row
    return out

