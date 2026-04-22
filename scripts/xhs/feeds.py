"""首页 Feed 列表，对应 Go xiaohongshu/feeds.go。"""

from __future__ import annotations

import json
import logging
import random
import time

from .cdp import Page
from .errors import NoFeedsError
from .human import sleep_random
from .types import Feed
from .urls import HOME_URL

logger = logging.getLogger(__name__)

# 从 __INITIAL_STATE__ 提取 feeds 的 JS
_EXTRACT_FEEDS_JS = """
(() => {
    if (window.__INITIAL_STATE__ &&
        window.__INITIAL_STATE__.feed &&
        window.__INITIAL_STATE__.feed.feeds) {
        const feeds = window.__INITIAL_STATE__.feed.feeds;
        const feedsData = feeds.value !== undefined ? feeds.value : feeds._value;
        if (feedsData) {
            return JSON.stringify(feedsData);
        }
    }
    return "";
})()
"""


def browse_and_extract_feeds(page: Page) -> list[Feed]:
    """自然浏览首页并提取 Feed（含滚动行为，用于伪装 + 缓存双收益）。

    导航到首页后模拟人类浏览行为：等待加载、滚动 2~4 次、偶尔点击笔记，
    最后提取 __INITIAL_STATE__ 中的 feed 数据。

    Raises:
        NoFeedsError: 没有捕获到 feeds 数据。
    """
    page.navigate(HOME_URL)
    page.wait_for_load()
    page.wait_dom_stable()
    sleep_random(1500, 3000)  # 1.5~3 秒"看"内容

    # 模拟浏览：滚动 2~4 次
    for _ in range(random.randint(2, 4)):
        viewport = page.get_viewport_height()
        scroll_delta = viewport * random.uniform(0.5, 0.9)
        page.scroll_by(0, int(scroll_delta))
        sleep_random(800, 2500)

    # 30% 概率点进一个笔记看看
    if random.random() < 0.3:
        try:
            note_cards = page.query_selector_all("section.note-item")
            if note_cards:
                page.click_element("section.note-item")
                sleep_random(1500, 3000)
                page.press_key("Escape")
                sleep_random(500, 1000)
        except Exception:
            pass  # 点笔记失败不影响提取

    # 滚回顶部
    page.scroll_to(0, 0)
    sleep_random(300, 600)

    result = page.evaluate(_EXTRACT_FEEDS_JS)
    if not result:
        raise NoFeedsError()

    feeds_data = json.loads(result)
    logger.info("browse_and_extract_feeds: 提取到 %d 条 feeds", len(feeds_data))
    return [Feed.from_dict(f) for f in feeds_data]


def list_feeds(page: Page) -> list[Feed]:
    """获取首页 Feed 列表（兼容旧接口，使用自然浏览行为）。

    Raises:
        NoFeedsError: 没有捕获到 feeds 数据。
    """
    return browse_and_extract_feeds(page)
