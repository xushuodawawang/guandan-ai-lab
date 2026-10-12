import pytest

from guandan_ai.platform import ROOT, RUNTIME

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest


def test_all_dashboard_pages_load_without_errors():
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=15).run()
    assert not app.exception
    for page in ["欢乐掼蛋", "对局回放", "记牌预测", "PK 统计", "手牌分析"]:
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, page
        assert not app.error, page


@pytest.mark.integration
def test_hand_analysis_form_runs_official_rules():
    if not (RUNTIME / "guandan-java/guandan-java-action.jar").is_file():
        pytest.skip("官方运行时未下载")
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=15).run()
    app.sidebar.radio[0].set_value("手牌分析").run()
    app.text_area[0].input("S3 H3 C4 D5 S6 H7")
    app.button[0].click().run()
    assert not app.exception and not app.error
    assert app.session_state["analysis_result"][2][0]["turns"] == 2


@pytest.mark.integration
def test_browser_game_is_embedded_with_fullscreen_link():
    if not (RUNTIME / "guandan-java/guandan-java-action.jar").is_file():
        pytest.skip("官方运行时未下载")
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=20).run()
    assert not app.exception and not app.error
    assert app.get("iframe")[0].proto.src == "http://127.0.0.1:8502/"

