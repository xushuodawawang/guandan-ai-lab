from collections import Counter

from .belief import PublicHistory
from .models import Observation, action_cards, is_pass


def public_message(event, seat=None):
    kind = event.get("kind")
    if kind == "begin" and (seat is None or event.get("recipient") == seat):
        return event["message"]
    if kind == "public_event":
        return event["message"]
    if kind == "received" and event["message"].get("type") == "notify":
        return event["message"]
    return None


def replay_snapshot(events, cursor, seat=0):
    hands = [[] for _ in range(4)]
    rest = [27] * 4
    latest = None
    trump = "2"
    leader = -1
    greater = None
    order = []
    for event in events[:cursor + 1]:
        if event.get("kind") == "decision":
            obs = event["observation"]
            hands[event["seat"]] = obs["hand"][:]
            rest = list(obs["rest"])
            trump = str(obs["trump"])
            if event["seat"] == seat:
                latest = event
        msg = public_message(event)
        if not msg:
            continue
        stage = msg.get("stage")
        if stage == "beginning":
            recipient = event.get("recipient", msg.get("myPos", seat))
            if recipient == 0 or event.get("kind") == "received":
                hands = [[] for _ in range(4)]
                rest = [27] * 4
                order, latest, leader, greater = [], None, -1, None
            hands[recipient] = list(msg["handCards"])
            raw_rank = msg.get("curRank", "2")
            trump = "23456789TJQKA"[raw_rank - 2] if isinstance(raw_rank, int) and 2 <= raw_rank <= 14 else str(raw_rank)
        elif stage == "play":
            pos, action = msg["curPos"], msg["curAction"]
            if not is_pass(action):
                cards = Counter(action_cards(action))
                hands[pos] = list((Counter(hands[pos]) - cards).elements())
                rest[pos] -= sum(cards.values())
                if rest[pos] == 0:
                    order.append(pos)
            leader, greater = msg.get("greaterPos", -1), msg.get("greaterAction")
        elif stage in ("tribute", "back"):
            for giver, receiver, card in msg.get("result", []):
                hands[giver] = list((Counter(hands[giver]) - Counter([card])).elements())
                if hands[receiver]:
                    hands[receiver].append(card)
                rest[giver] -= 1
                rest[receiver] += 1
        elif stage == "episodeOver":
            order = msg["order"]
    return {"hands": hands, "rest": rest, "trump": trump, "leader": leader,
            "greater": greater, "order": order, "decision": latest}


def history_at(events, selected):
    seat = selected["seat"]
    history = PublicHistory(seat)
    for event in events:
        if event["seq"] > selected["seq"]:
            break
        msg = public_message(event, seat)
        if msg:
            if msg.get("stage") == "beginning" and msg.get("myPos", seat) != seat:
                continue
            history.observe(msg)
        if event is selected or event["seq"] == selected["seq"]:
            break
    fields = selected["observation"]
    obs = Observation(**fields)
    history.rest = list(obs.rest)
    return history, obs

