import asyncio
import json
import uuid
from dataclasses import asdict
from pathlib import Path

from websockets.asyncio.client import connect

from .belief import PublicHistory
from .evaluation import normalize_result
from .logging import JsonlLogger
from .models import Observation
from .worker import ProcessPolicy


def build_response(obs, decision, room_id):
    if not 0 <= decision.index < len(obs.actions):
        raise ValueError("非法动作索引")
    types = {"play": "PLAY", "tribute": "TRIBUTE", "back": "PAYTRIBUTE"}
    if obs.stage not in types:
        raise ValueError(f"未知动作阶段: {obs.stage}")
    data = {"roomId": room_id, "player": obs.seat, "act": obs.actions[decision.index]}
    if obs.stage == "back":
        data.update(tributePos=obs.wire["tributePos"], tribute=obs.wire["tribute"])
    return {"type": types[obs.stage], "data": data}


async def play_client(url="ws://127.0.0.1:8181", strategy="hybrid", seat=0, room_id=None,
                      rounds=1, budget_ms=200, seed=0, log_path="data/runs/client.jsonl",
                      on_room=None, timeout=1800, config=None):
    worker = ProcessPolicy(strategy, seat, seed, config)
    await asyncio.to_thread(worker.start)
    history = PublicHistory(seat)
    user = f"py-{seat}-{uuid.uuid4().hex[:10]}"
    result = None
    seen_over = False
    try:
        with JsonlLogger(log_path) as logger:
            logger.write("client_start", strategy=strategy, seat=seat, url=url, budget_ms=budget_ms)
            async with connect(url, max_size=16 * 1024 * 1024, open_timeout=15, ping_interval=None, proxy=None) as ws:
                kind = "CREATE_ROOM" if room_id is None else "JOIN_ROOM"
                data = {"userId": user, "seatNum": seat}
                data.update({"round": rounds} if room_id is None else {"roomId": room_id})
                await ws.send(json.dumps({"type": kind, "data": data}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    msg = json.loads(raw)
                    logger.write("received", message=msg)
                    if "code" in msg and msg["code"] != 200:
                        raise RuntimeError(f"服务器拒绝请求: {msg}")
                    if msg.get("type") in ("CREATE_ROOM", "JOIN_ROOM") and "data" in msg:
                        room_id = int(msg["data"]["roomId"])
                        # Native distribution uses userNum as a count/index, whereas
                        # newer Java source uses the assigned seat. beginning.myPos
                        # is the authoritative per-player seat in both distributions.
                        print(f"房间 {room_id}，请求座位 {seat}，策略 {strategy}", flush=True)
                        if on_room:
                            on_room(room_id)
                            on_room = None
                    if msg.get("type") in ("notify", "act"):
                        if msg.get("stage") == "beginning" and int(msg["myPos"]) != seat:
                            raise RuntimeError(f"实际座位 {msg['myPos']} 与请求 {seat} 不一致；请按座位顺序加入房间")
                        history.observe(msg)
                        worker.observe(msg)
                    if msg.get("type") == "act":
                        obs = Observation.from_message(msg, seat, history.finished)
                        decision = await asyncio.to_thread(worker.decide, obs, budget_ms)
                        response = build_response(obs, decision, room_id)
                        await ws.send(json.dumps(response, ensure_ascii=False))
                        logger.write("decision", seat=seat, strategy=strategy, observation=asdict(obs),
                                     decision=decision.to_dict(), action=obs.actions[decision.index])
                        if worker.process is None:
                            await asyncio.to_thread(worker.start)
                    if msg.get("stage") == "gameResult":
                        result = normalize_result(msg)
                        logger.write("result", result=result)
                    if msg.get("stage") == "gameOver":
                        seen_over = True
                    if seen_over and result is not None:
                        break
            return result
    finally:
        await asyncio.to_thread(worker.close)


async def arena(url="ws://127.0.0.1:8181", strategies=None, output="data/runs/online", **options):
    strategies = strategies or ["hybrid", "greedy", "hybrid", "greedy"]
    if len(strategies) != 4:
        raise ValueError("需要四个座位的策略名称")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    future = asyncio.get_running_loop().create_future()

    def got_room(room_id):
        if not future.done():
            future.set_result(room_id)

    first = asyncio.create_task(play_client(url, strategies[0], 0, log_path=output / "seat-0.jsonl", on_room=got_room, **options))
    tasks = [first]
    try:
        done, _ = await asyncio.wait([first, future], timeout=40, return_when=asyncio.FIRST_COMPLETED)
        if future not in done:
            if first.done():
                await first
            raise TimeoutError("建房未在规定时间内完成")
        room_id = future.result()
        # Join sequentially: older native releases assign seats by arrival order.
        for i in range(1, 4):
            joined = asyncio.get_running_loop().create_future()
            def on_joined(value, ready=joined):
                if not ready.done():
                    ready.set_result(value)
            task = asyncio.create_task(play_client(url, strategies[i], i, room_id=room_id,
                log_path=output / f"seat-{i}.jsonl", on_room=on_joined, **options))
            tasks.append(task)
            done, _ = await asyncio.wait([task, joined], timeout=40, return_when=asyncio.FIRST_COMPLETED)
            if joined not in done:
                if task.done():
                    await task
                raise TimeoutError(f"座位 {i} 入房失败或超时")
        results = await asyncio.gather(*tasks)
        (output / "results.json").write_text(json.dumps({"room_id": room_id, "strategies": strategies,
            "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
        from .evaluation import write_online_summary
        write_online_summary([output / f"seat-{i}.jsonl" for i in range(4)], strategies, output, url,
                             options.get("budget_ms", 200), options.get("seed", 1), options.get("rounds", 1))
        return results
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

