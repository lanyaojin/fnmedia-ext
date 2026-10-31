#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""播放流信息：只呈现“源给的”和“实测的”，绝不编造清晰度标签。

背景：飞牛播放器的质量菜单本身就是线路菜单，源名由播放器显示；有的源
（如优酷线）在菜单里带码率/原画标记，有的源（如光速/极速/新狼）不带——
那是源没给，不是播放器没显示。本模块只做两件事：

1. 源标称码率：解析 m3u8 主清单里的 STREAM-INF BANDWIDTH，取最大值。
   主清单里没有该字段，就返回 None，调用方必须显示“源未标码率”，
   不得用分片大小、文件名或经验值猜一个清晰度。
   注意：app.py 里 hls_preset 输出的 BANDWIDTH 占位值（2000000 等）
   是“变体区分占位值”（让播放器把多条线路当成不同变体），不是源标称码率，
   绝不能把它当成清晰度展示——本模块不读、不改那段逻辑。

2. 实测首字节延迟：对一个 URL 做流式 GET，只等到首字节就关闭，不下载
   正文。这是管理页/探活用的测量原语；起播竞速里已有的 _probe_latency
   测的也是同类延迟，sourcehealth 的 avg_latency 就是它的滚动均值，
   两者口径一致，不要另起一套“清晰度分”。

本模块的解析函数是纯函数（无网络、无第三方依赖），可单测；
两个 async 探测函数只在管理页显式触发时调用，绝不在起播关键路径调用
（速度优先：preset 不为显示码率而多拉一次清单）。
"""
from __future__ import annotations

import time


def parse_master_bandwidth(text: str) -> int | None:
    """从 m3u8 文本解析源标称码率（bps）。

    只有含 STREAM-INF 的主清单才有意义；媒体清单（只有分片，
    没有 STREAM-INF）返回 None。多个变体取最大值（代表该源提供的最高档）。
    实现用按行切分加字符串匹配，不用正则，避免转义问题。
    """
    if not text or "#EXT-X-STREAM-INF" not in text:
        return None
    vals = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        # BANDWIDTH 与 STREAM-INF 在同一逗号段里（冒号后），不能按逗号切分后
        # 再找段首；直接在行内找 BANDWIDTH= 并取其后连续数字（属性顺序不限）。
        idx = line.find("BANDWIDTH=")
        if idx < 0:
            continue
        digits = []
        for ch in line[idx + len("BANDWIDTH="):]:
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            try:
                vals.append(int("".join(digits)))
            except ValueError:
                continue
    return max(vals) if vals else None


def format_claimed_bitrate(bps: int | None) -> str:
    """源标称码率展示文案。bps 为 None 时必须显示“源未标码率”。"""
    if not bps:
        return "源未标码率"
    if bps >= 1_000_000:
        return "源标称 %d Mbps" % int(bps / 1_000_000)
    return "源标称 %d Kbps" % int(bps / 1_000)


async def probe_claimed_bitrate(url: str, headers: dict | None = None,
                                timeout: float = 8.0) -> tuple[int | None, str]:
    """拉一次清单文本（不下载分片），返回 (bps, 展示文案)。

    仅供管理页显式探活调用；失败一律返回 (None, "源未标码率")，不抛异常。
    """
    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True,
                                     trust_env=False) as client:
            r = await client.get(url, headers=headers or {},
                                 timeout=timeout)
            if r.status_code != 200:
                return None, "源未标码率"
            bps = parse_master_bandwidth(r.text)
            return bps, format_claimed_bitrate(bps)
    except Exception:
        return None, "源未标码率"


async def probe_first_byte(url: str, headers: dict | None = None,
                           timeout: float = 8.0) -> float | None:
    """测首字节延迟（秒）：流式 GET，首字节到达即关闭，不下载正文。

    失败返回 None（不断言死链，调用方按失败计数处理）。
    """
    try:
        import httpx
        t0 = time.time()
        async with httpx.AsyncClient(follow_redirects=True,
                                     trust_env=False) as client:
            async with client.stream("GET", url, headers=headers or {},
                                     timeout=timeout) as r:
                async for _chunk in r.aiter_bytes():
                    break
                return time.time() - t0
    except Exception:
        return None
