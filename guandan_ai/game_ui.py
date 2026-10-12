"""Embed the browser game in the experiment dashboard."""

import json
from urllib.request import urlopen

import streamlit as st
from streamlit.components.v1 import iframe

from .web_server import start_background_server


GAME_URL = "http://127.0.0.1:8502/"


@st.cache_resource
def browser_game_server():
    try:
        with urlopen(GAME_URL + "health", timeout=0.5) as response:
            if json.load(response).get("app") != "guandan-browser":
                raise RuntimeError("8502 端口被其他应用占用，可使用 play --port 指定端口")
        return None
    except OSError:
        return start_background_server(8502)


def play_page(resources=None):
    browser_game_server()
    left, right = st.columns([3, 1])
    left.caption("网页牌桌 · 真人与对家 AI 组队 · 点击牌面选牌；跟牌可直接不出")
    right.link_button("全屏打开牌桌 ↗", GAME_URL, use_container_width=True)
    iframe(GAME_URL, height=1120, scrolling=True)
