import json

from guandan_ai.evaluation import write_online_summary
from guandan_ai.logging import JsonlLogger


def test_online_report_deduplicates_public_episodes_and_is_not_paired(tmp_path):
    paths = []
    for seat in range(4):
        path = tmp_path / f"seat-{seat}.jsonl"
        paths.append(path)
        with JsonlLogger(path) as log:
            log.write("client_start", seat=seat)
            log.write("decision", decision={"elapsed_ms": 10 + seat, "fallback": False})
            log.write("received", message={"type": "notify", "stage": "episodeOver", "order": [0, 2, 1, 3], "curRank": "2"})
            log.write("result", result={"winner": 0, "basis": "official victory", "raw": {"victory": 0}})
    summary = write_online_summary(paths, ["team", "greedy", "team", "greedy"], tmp_path, "ws://localhost:8181")
    assert summary["complete_pairs"] == 0 and not summary["paired_deals"]
    assert summary["decisions"] == 4 and summary["results"][0]["episodes"] == 1
    assert summary["results"][0]["mean_ms"] == 11.5
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "未控制发牌" in report and "相同起始牌堆" not in report
    saved = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert saved["results"][0]["winner"] == 0
