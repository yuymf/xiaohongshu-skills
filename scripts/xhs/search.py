"""搜索 Feeds，对应 Go xiaohongshu/search.go。

搜索方式：在首页搜索框输入关键词并点击搜索，模拟人类操作。
避免直接导航到搜索 URL（会触发小红书反爬风控 error_code=300011）。

风控伪装增强：
- 搜索前先浏览首页（滚动+偶尔点击笔记），模拟真实用户行为
- 浏览过程中顺便提取首页 feeds 并缓存，供 listXhsFeed 复用
- 搜索结果页也会滚动浏览，避免秒提走的风控信号
"""

from __future__ import annotations

import json
import logging
import random
import time

from . import feeds as feeds_mod
from .cdp import Page
from .errors import NoFeedsError
from .human import sleep_random
from .selectors import FILTER_BUTTON, FILTER_PANEL
from .types import Feed, FilterOption
from .urls import EXPLORE_URL

logger = logging.getLogger(__name__)

# 搜索框相关选择器（小红书首页实际 DOM 结构）
SEARCH_INPUT = "#search-input"
SEARCH_BUTTON = "div.input-button"

# 筛选选项映射表：{筛选组索引: [(标签索引, 文本), ...]}
_FILTER_OPTIONS: dict[int, list[tuple[int, str]]] = {
    1: [(1, "综合"), (2, "最新"), (3, "最多点赞"), (4, "最多评论"), (5, "最多收藏")],
    2: [(1, "不限"), (2, "视频"), (3, "图文")],
    3: [(1, "不限"), (2, "一天内"), (3, "一周内"), (4, "半年内")],
    4: [(1, "不限"), (2, "已看过"), (3, "未看过"), (4, "已关注")],
    5: [(1, "不限"), (2, "同城"), (3, "附近")],
}

# 从 __INITIAL_STATE__ 提取搜索结果的 JS
_EXTRACT_SEARCH_JS = """
(() => {
    if (window.__INITIAL_STATE__ &&
        window.__INITIAL_STATE__.search &&
        window.__INITIAL_STATE__.search.feeds) {
        const feeds = window.__INITIAL_STATE__.search.feeds;
        const feedsData = feeds.value !== undefined ? feeds.value : feeds._value;
        if (feedsData) {
            return JSON.stringify(feedsData);
        }
    }
    return "";
})()
"""

# ─── 首页 Feed 缓存（跨命令复用） ──────────────────────────────────────────────

_feed_cache: dict[str, tuple[float, list[dict]]] = {}
_FEED_CACHE_TTL = 30 * 60  # 30 分钟


def get_cached_feeds() -> list[dict] | None:
    """获取缓存的 feed 数据（30 分钟内有效）。

    注意：CLI 每次调用是独立进程，此缓存仅在同一进程内有效
    （即 cmd_search_feeds 读取刚写入的 cached_home_feeds）。
    跨进程复用由 TS 端的 _feedCache 处理。
    """
    cache_key = "homepage"
    if cache_key in _feed_cache:
        cached_at, cached_data = _feed_cache[cache_key]
        if time.monotonic() - cached_at < _FEED_CACHE_TTL:
            return cached_data
        del _feed_cache[cache_key]  # 清理过期条目
    return None


def _cache_feeds(feeds_data: list[dict]) -> None:
    """缓存 feed 数据。"""
    _feed_cache["homepage"] = (time.monotonic(), feeds_data)


def _find_internal_option(group_index: int, text: str) -> tuple[int, int]:
    """查找内部筛选选项索引。

    Returns:
        (filters_index, tags_index)

    Raises:
        ValueError: 未找到匹配的选项。
    """
    options = _FILTER_OPTIONS.get(group_index)
    if not options:
        raise ValueError(f"筛选组 {group_index} 不存在")

    for tags_index, option_text in options:
        if option_text == text:
            return group_index, tags_index

    valid = [t for _, t in options]
    raise ValueError(f"在筛选组 {group_index} 中未找到 '{text}'，有效值: {valid}")


def _convert_filters(filter_opt: FilterOption) -> list[tuple[int, int]]:
    """将 FilterOption 转换为内部 (filters_index, tags_index) 列表。"""
    result: list[tuple[int, int]] = []

    if filter_opt.sort_by:
        result.append(_find_internal_option(1, filter_opt.sort_by))
    if filter_opt.note_type:
        result.append(_find_internal_option(2, filter_opt.note_type))
    if filter_opt.publish_time:
        result.append(_find_internal_option(3, filter_opt.publish_time))
    if filter_opt.search_scope:
        result.append(_find_internal_option(4, filter_opt.search_scope))
    if filter_opt.location:
        result.append(_find_internal_option(5, filter_opt.location))

    return result


def search_feeds(
    page: Page,
    keyword: str,
    filter_option: FilterOption | None = None,
) -> list[Feed]:
    """搜索 Feeds（搜索前自然浏览首页，顺便缓存 feeds）。

    通过首页搜索框输入关键词并点击搜索，模拟人类操作。
    搜索前会先浏览首页内容（滚动+偶尔点击笔记），降低风控信号，
    同时提取首页 feeds 缓存供 listXhsFeed 复用。

    Args:
        page: CDP 页面对象。
        keyword: 搜索关键词。
        filter_option: 可选筛选条件。

    Raises:
        NoFeedsError: 没有捕获到搜索结果。
        ValueError: 筛选选项无效。
    """
    # ── 1. 先逛首页（模拟自然浏览行为 + 提取 feeds 做缓存）──
    page.navigate(EXPLORE_URL)
    page.wait_for_load()
    page.wait_dom_stable()
    sleep_random(2000, 4000)  # 2~4 秒"看"内容

    # 滚动 2~3 次
    for _ in range(random.randint(2, 3)):
        viewport = page.get_viewport_height()
        scroll_delta = viewport * random.uniform(0.5, 0.8)
        page.scroll_by(0, int(scroll_delta))
        sleep_random(800, 2000)

    # 30% 概率点进一个笔记看看
    if random.random() < 0.3:
        try:
            note_cards = page.query_selector_all("section.note-item")
            if note_cards:
                current_url = page.evaluate("window.location.href")
                page.click_element("section.note-item")
                sleep_random(1500, 3000)
                page.press_key("Escape")
                sleep_random(500, 1000)
                # 验证已回到首页，如果导航走了则重新导航
                new_url = page.evaluate("window.location.href")
                if current_url != new_url:
                    page.navigate(EXPLORE_URL)
                    page.wait_for_load()
                    page.wait_dom_stable()
        except Exception:
            pass  # 点笔记失败不影响搜索

    # ★ 提取首页 feeds 并缓存（和 list-feeds 同源数据）
    # 存储为 Feed.to_dict() 扁平格式，确保 TS 端 mapFeedToNote 能正确解析
    try:
        feeds_raw = page.evaluate(feeds_mod._EXTRACT_FEEDS_JS)
        if feeds_raw:
            feeds_data = json.loads(feeds_raw)
            normalized = [Feed.from_dict(f).to_dict() for f in feeds_data]
            _cache_feeds(normalized)
            logger.info("搜索前浏览首页，缓存了 %d 条 feeds", len(normalized))
    except Exception:
        pass  # 缓存失败不影响搜索

    # 滚回顶部，准备搜索
    page.scroll_to(0, 0)
    sleep_random(500, 1500)

    # ── 2. 原有搜索逻辑 ──
    # 等待搜索框出现
    page.wait_for_element(SEARCH_INPUT, timeout=10.0)

    # 点击搜索框，聚焦
    page.click_element(SEARCH_INPUT)
    sleep_random(300, 600)

    # 使用 React nativeInputValueSetter 设置搜索关键词
    # 直接设置 value 无法触发 React 响应式更新，需要用原生 setter + 事件
    page.evaluate(
        f"""
        (() => {{
            const input = document.querySelector({json.dumps(SEARCH_INPUT)});
            if (!input) return;
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(input, {json.dumps(keyword)});
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }})()
        """
    )
    sleep_random(500, 1000)

    # 点击搜索按钮（div.input-button），触发页面导航到搜索结果页
    page.click_element(SEARCH_BUTTON)
    sleep_random(1500, 2500)

    # 等待搜索结果页加载
    page.wait_for_load()
    page.wait_dom_stable()

    # 等待 __INITIAL_STATE__ 初始化
    _wait_for_initial_state(page)

    # 应用筛选条件
    if filter_option:
        internal_filters = _convert_filters(filter_option)
        if internal_filters:
            _apply_filters(page, internal_filters)

    # ── 3. 搜索结果页滚动浏览（降低风控信号）──
    sleep_random(1000, 2000)
    for _ in range(random.randint(1, 2)):
        viewport = page.get_viewport_height()
        page.scroll_by(0, int(viewport * random.uniform(0.3, 0.6)))
        sleep_random(600, 1500)

    # 滚回顶部再提取
    page.scroll_to(0, 0)
    sleep_random(300, 600)

    # ── 4. 提取搜索结果 ──
    result = page.evaluate(_EXTRACT_SEARCH_JS)
    if not result:
        raise NoFeedsError()

    feeds_data = json.loads(result)
    return [Feed.from_dict(f) for f in feeds_data]


def _wait_for_initial_state(page: Page, timeout: float = 10.0) -> None:
    """等待 __INITIAL_STATE__ 就绪。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = page.evaluate("window.__INITIAL_STATE__ !== undefined")
        if ready:
            return
        time.sleep(0.5)
    logger.warning("等待 __INITIAL_STATE__ 超时")


def _wait_for_search_state(page: Page, timeout: float = 15.0) -> None:
    """等待 __INITIAL_STATE__.search.feeds 就绪（SPA 搜索结果加载完成）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = page.evaluate(
            "window.__INITIAL_STATE__ && "
            "window.__INITIAL_STATE__.search && "
            "window.__INITIAL_STATE__.search.feeds !== undefined"
        )
        if ready:
            return
        time.sleep(0.5)
    logger.warning("等待搜索结果状态超时")


def _apply_filters(page: Page, filters: list[tuple[int, int]]) -> None:
    """应用筛选条件。"""
    # 悬停筛选按钮
    page.hover_element(FILTER_BUTTON)

    # 等待筛选面板出现
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if page.has_element(FILTER_PANEL):
            break
        sleep_random(300, 600)

    # 点击各筛选项
    for filters_index, tags_index in filters:
        selector = (
            f"div.filter-panel div.filters:nth-child({filters_index}) "
            f"div.tags:nth-child({tags_index})"
        )
        page.click_element(selector)
        sleep_random(300, 600)

    # 等待页面更新
    page.wait_dom_stable()
    _wait_for_initial_state(page)
