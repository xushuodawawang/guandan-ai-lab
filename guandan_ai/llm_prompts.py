"""Built-in Guandan coaching prompts and strictly player-visible context."""
from collections import Counter

from .analysis import features
from .cards import RANKS, rank_value, remove_cards
from .models import action_cards, is_pass

PROMPT_VERSION = "guandan-team-v2"
CONTROL_TYPES = ("Bomb", "StraightFlush", "FourKings")
PATTERNS = {"PASS": "不出", "Single": "单张", "Pair": "对子", "Trips": "三张",
            "ThreeWithTwo": "三带二", "Straight": "顺子", "ThreePair": "三连对",
            "TwoTrips": "钢板", "Bomb": "同点炸弹", "StraightFlush": "同花顺",
            "FourKings": "四王炸", "tribute": "进贡", "back": "还贡"}

RULES_PROMPT = """【身份与目标】
你是掼蛋四人赛的一名团队牌手，不是斗地主的地主或农民。对家是唯一队友，左右两家是对手。
为本人及队友争取有利出完顺序，优先争头游与双上；不能为了个人多出几张牌而破坏团队牌权。
所有座位号都是裁判的绝对座位，不是网页旋转后的显示位置。只能使用本人手牌与公开事实。

【本平台规则与编码】
两副牌共108张，四人各27张；0和2同队，1和3同队。S/H/C/D为黑桃/红桃/梅花/方块，T是10；
SB为小王、HR为大王，同一编码可能有两张实体牌。trump是本副级牌，self_rank/oppo_rank是双方级别。
非序列牌型的点数次序为：大王>小王>级牌>A>K>Q>J>10>9>8>7>6>5>4>3>2（从普通序列移除级牌）。
红桃级牌是百搭，可按官方候选的解释配普通点数，不能当大小王。保留百搭通常有利，但不能因舍不得百搭错失直接出完或必要拦截。
普通牌型：Single单张，Pair对子，Trips三张，ThreeWithTwo三带二；Straight五张顺子，
ThreePair三个连续对子，TwoTrips两个连续三张（钢板）。序列按自然点数，级牌不会在顺子中升为高点；
A的低位/高位序列解释以候选为准，不能自行重排或判定。三带二按三张主点比较，带的对子不决定大小。
Bomb为至少四张同点炸弹，StraightFlush为五张同花顺，FourKings为两大王加两小王。
炸弹可压普通牌，同点炸弹先比较张数再比较点数；同花顺与不同长度炸弹的覆盖关系必须服从官方候选。
跟牌通常须同型、同长度且更大，或用合法炸弹。候选中的每个动作已经通过裁判验证，不要再凭记忆剔除合法候选。
PASS是主动不出：即使能压也可不出；新一圈领出、进贡和还贡时不能凭空PASS。can_pass与turn_mode由当前合法列表决定。
别人都不压时，持有牌权者领出；已出完者的接风与跳过空手玩家由裁判处理，收到领出候选就按新圈处理，不用过期leading_action推理。
tribute/back是进贡/还贡阶段：只挑该阶段合法交接候选，比较交接后的手牌结构；抗贡、可交接点数和接收人均服从裁判，不套用普通跟牌策略。
名次与升级按裁判结算；队友已出完后不能继续向空手队友“送牌”，应为本人的后续名次争取牌权。

【配合与策略】
1. 先检查是否能直接出完，再判断这一手对团队名次、牌权和对手下次出完机会的影响；不是简单选择张数最多的动作。
2. 队友持有牌权时通常PASS，尤其队友只剩1至2张且这手可能封住对手。不要用更大单张、百搭或炸弹无意义接管队友。
   例外要有具体依据：本人能直接出完、接管后有可执行的收尾、或公开局面表明必须封堵对手。对手少牌不等于必须压队友；PASS也不会立刻结束这一圈。
3. 对手持有牌权且剩1至2张时提高拦截优先级，必要时用控制牌或炸弹，不为“省大牌”让其轻易走完；仍比较拦截后能否保持控制。
4. 本人领出且对手剩1张时，避免无依据送低单；对手剩2张时，小对子可能放走对手，但不能断言其两张必定成对。
   尽量选对手难以整手走完且本人结构完整的牌型。队友少牌时，根据已公开的出牌/交接推断可接牌型；不知道队友牌值就不能声称“队友一定能接”。
5. 先处理难走的散牌，同时保留顺子、对子、钢板和炸弹的整体性。低代价压牌一般优于过度压大；不要拆炸弹凑无关单张或为小收益消耗多张百搭。
6. 炸弹是争夺牌权的资源，不是看到能炸就炸，也不是永远不用。用于抢关键牌权、阻止对手出完、掩护队友或组织连续收尾；四王炸尤其要考虑后续。
7. 末局看剩余牌的可执行收尾与对手接牌压力：检查下一圈能否出掉尾张，避免只剩一张小散牌却没有控制手段。
8. PASS只是行为信息，可能是让队友或保留资源，不能据此断言某人绝无大牌。未知牌属于谁不可知，公开剩余未见牌不是某个对手的手牌。

【候选信息与输出】
每个candidate附原始action、remaining_after、结构粗估、百搭消耗和拆牌标记。结构/本地评分是近似提示，不是精确最优解；
“拆对子/拆三张”有时有利，应结合实际剩余手牌和团队局面，而不是盲目遵从标记。候选排序与编号也不代表必选顺序。
在内部按“身份与阶段→直接收尾→团队牌权→对手危险→剩余结构→控制资源”比较候选。
只输出一个JSON对象，且仅有candidate_id整数，例如 {"candidate_id":0}；这是格式示例，不是在推荐0。
不得输出牌列表、座位指令、解释段落、Markdown或新增候选，不得从历史动作编号复制本回合candidate_id。"""


def effective_leader(obs):
    can_pass = obs.stage == "play" and any(is_pass(a) for a in obs.actions)
    return obs.greater_pos if can_pass and not is_pass(obs.greater_action) else -1


def system_prompt(obs):
    opponents = [(obs.seat + 1) % 4, (obs.seat + 3) % 4]
    leader = effective_leader(obs)
    role = f"\n【本回合职责】你是座位{obs.seat}，队友是座位{obs.teammate}，对手是座位{opponents[0]}和{opponents[1]}。"
    if obs.stage != "play":
        role += f"当前为{PATTERNS.get(obs.stage, obs.stage)}阶段；只比较合法交接后结构，不做抢牌权或送队友牌的判断。"
    elif obs.rest[obs.teammate] == 0 or obs.teammate in obs.finished:
        role += "队友已出完；你负责争取自己的有利名次，不再向队友送牌。"
    elif leader == obs.teammate:
        role += "队友正在持有牌权。你的主要职责是配合让牌，优先评估PASS，只有有具体团队收益才接管。"
    elif leader in opponents:
        role += f"座位{leader}的对手正在持有牌权。评估最低代价接管及拦截机会。"
    elif leader < 0:
        role += "当前由你领出新一圈。选择有利于自己结构与团队推进的牌型，不能PASS。"
    else:
        role += "本人的上一手仍是当前最大；按本回合跟牌候选处理，是否可PASS由候选决定，不能误当作新领出。"
    danger = [s for s in opponents if s not in obs.finished and 0 < obs.rest[s] <= 2]
    if obs.stage == "play" and danger:
        role += f"对手少牌警报：{[(s, obs.rest[s]) for s in danger]}；关注其下一次可能出完，不能假定其具体牌值。"
    return RULES_PROMPT + role


def action_power(action, trump):
    if is_pass(action) or action[0] in ("tribute", "back"):
        return 0
    rank = action[1]
    if action[0] in ("Straight", "StraightFlush", "ThreePair", "TwoTrips"):
        return 1 if rank == "A" else RANKS.index(rank) + 2
    return rank_value(rank, trump)


def candidate_detail(obs, action, candidate_id, score):
    cards = action_cards(action)
    remaining = remove_cards(obs.hand, cards)
    before, after = Counter(c[1] for c in obs.hand), Counter(c[1] for c in remaining)
    return {"candidate_id": candidate_id, "action": action,
            "pattern_name": PATTERNS.get(action[0], action[0]), "cards_played": len(cards),
            "can_finish": obs.stage == "play" and bool(cards) and not remaining,
            "remaining_after": remaining, "structure_estimate": features(remaining, obs.trump),
            "wildcards_used": cards.count("H" + obs.trump), "uses_control_pattern": action[0] in CONTROL_TYPES,
            "breaks_natural_bomb": [r for r, n in before.items() if n >= 4 and 0 < after[r] < 4],
            "breaks_pair": [r for r, n in before.items() if n >= 2 and after[r] == 1],
            "breaks_trips": [r for r, n in before.items() if n >= 3 and 0 < after[r] < 3],
            "local_heuristic_score": round(score, 3)}


def public_events(history):
    # Never forward arbitrary wire fields, even when a notify contains extras.
    rows = []
    for event in history.events[-32:]:
        stage = event.get("stage")
        if stage == "play":
            rows.append({key: event[key] for key in ("stage", "curPos", "curAction", "greaterPos", "greaterAction") if key in event})
        elif stage in ("tribute", "back"):
            rows.append({"stage": stage, "result": event.get("result", [])})
    return rows


def build_context(obs, history, candidates):
    leader = effective_leader(obs)
    opponents = [(obs.seat + 1) % 4, (obs.seat + 3) % 4]
    can_pass = obs.stage == "play" and any(is_pass(a) for a in obs.actions)
    return {"seat": obs.seat, "teammate": obs.teammate, "opponents": opponents,
            "seat_roles": [{"seat": s, "role": "本人" if s == obs.seat else "队友" if s == obs.teammate else "对手",
                            "remaining": obs.rest[s]} for s in range(4)],
            "stage": obs.stage, "turn_mode": obs.stage if obs.stage != "play" else "follow" if can_pass else "lead",
            "can_pass": can_pass, "leader": leader,
            "leader_relation": "队友" if leader == obs.teammate else "对手" if leader in opponents else "本人" if leader == obs.seat else "无",
            "leading_action": obs.greater_action if leader >= 0 else None,
            "hand": list(obs.hand), "hand_rank_counts": dict(Counter(c[1] for c in obs.hand)),
            "trump": obs.trump, "wild_card": "H" + obs.trump,
            "self_rank": obs.self_rank, "oppo_rank": obs.oppo_rank,
            "remaining": list(obs.rest), "finished": list(obs.finished),
            "opponents_near_finish": [s for s in opponents if s not in obs.finished and 0 < obs.rest[s] <= 2],
            "teammate_finished": obs.rest[obs.teammate] == 0 or obs.teammate in obs.finished,
            "public_history": public_events(history), "candidates": candidates}
