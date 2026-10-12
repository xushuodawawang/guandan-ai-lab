"""Original vector playing cards, served locally without external assets."""

from functools import lru_cache

from .cards import CARD_TYPES


SUITS = {"S": "♠", "H": "♥", "C": "♣", "D": "♦"}
PIPS = {
    "2": [(50, 34), (50, 106)],
    "3": [(50, 34), (50, 70), (50, 106)],
    "4": [(32, 34), (68, 34), (32, 106), (68, 106)],
    "5": [(32, 34), (68, 34), (50, 70), (32, 106), (68, 106)],
    "6": [(x, y) for x in (32, 68) for y in (34, 70, 106)],
    "7": [(x, y) for x in (32, 68) for y in (34, 70, 106)] + [(50, 52)],
    "8": [(x, y) for x in (32, 68) for y in (34, 70, 106)] + [(50, 52), (50, 88)],
    "9": [(x, y) for x in (32, 68) for y in (30, 56, 84, 110)] + [(50, 70)],
    "T": [(x, y) for x in (32, 68) for y in (30, 56, 84, 110)] + [(50, 43), (50, 97)],
}


@lru_cache(maxsize=54)
def card_svg(card):
    if card not in CARD_TYPES:
        raise ValueError("无效的牌")
    red = card.startswith(("H", "D"))
    color = "#c12e45" if red else "#202d3c"
    suit = SUITS[card[0]]
    rank = card[1].replace("T", "10")
    if card in ("SB", "HR"):
        corner = ''.join(f'<text x="10" y="{17 + i * 10}" font-size="10">{letter}</text>' for i, letter in enumerate("JOKER"))
        label = "大王" if card == "HR" else "小王"
        center = f'''<path d="M28 57 Q16 25 39 39 Q51 15 61 39 Q89 25 72 57" fill="{color}"/>
          <path d="M30 55 Q50 49 70 55 L66 73 Q50 82 34 73Z" fill="#e8b557"/>
          <circle cx="40" cy="66" r="2" fill="{color}"/><circle cx="60" cy="66" r="2" fill="{color}"/>
          <path d="M42 73 Q50 80 58 73" fill="none" stroke="{color}" stroke-width="2"/>
          <path d="M35 82 L25 100 L44 95 L50 108 L57 95 L75 100 L65 82Z" fill="{color}"/>
          <g fill="#e8b557"><circle cx="27" cy="36" r="4"/><circle cx="51" cy="29" r="4"/><circle cx="74" cy="36" r="4"/></g>
          <text x="50" y="124" text-anchor="middle" font-size="12" font-weight="700">{label}</text>'''
    else:
        corner = f'<text x="9" y="22" font-size="20" font-weight="750">{rank}</text><text x="10" y="40" font-size="18">{suit}</text>'
        if card[1] in "JQK":
            crown = '<path d="M32 41 L29 27 L40 32 L50 22 L60 32 L71 27 L68 41Z" fill="#dca34f" stroke="#a2763b"/>'
            center = f'''<rect x="26" y="24" width="48" height="92" rx="4" fill="#f5eddf" stroke="#d8c8aa"/>
              {crown}<path d="M34 51 Q31 42 39 39 L61 39 Q70 44 66 54 L62 66 L38 66Z" fill="{color}"/>
              <path d="M37 49 Q50 43 63 49 L61 65 Q50 78 39 65Z" fill="#edd2a4"/>
              <path d="M36 72 L26 89 L32 116 L68 116 L74 89 L64 72 L50 88Z" fill="{color}"/>
              <path d="M38 74 L50 88 L62 74 L58 96 L50 101 L42 96Z" fill="#dca34f"/>
              <circle cx="43" cy="55" r="1.5"/><circle cx="57" cy="55" r="1.5"/>
              <path d="M43 65 Q50 70 57 65" fill="none" stroke="#a16d51"/>
              <text x="50" y="109" text-anchor="middle" fill="#fff5dc" font-size="19" font-weight="700">{suit}</text>'''
        elif card[1] == "A":
            center = f'<text x="50" y="91" text-anchor="middle" font-size="64">{suit}</text>'
        else:
            pieces = []
            for x, y in PIPS[card[1]]:
                transform = f' transform="rotate(180 {x} {y})"' if y > 70 else ""
                pieces.append(f'<text x="{x}" y="{y + 8}" text-anchor="middle" font-size="25"{transform}>{suit}</text>')
            center = ''.join(pieces)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="140" viewBox="0 0 100 140">
      <defs><linearGradient id="paper" x2=".8" y2="1"><stop stop-color="#ffffff"/><stop offset="1" stop-color="#f6f2e9"/></linearGradient></defs>
      <rect x=".8" y=".8" width="98.4" height="138.4" rx="7" fill="url(#paper)" stroke="#d9d7ce"/>
      <g fill="{color}" font-family="Georgia, 'Noto Serif', serif">{corner}<g transform="rotate(180 50 70)">{corner}</g>{center}</g></svg>'''
