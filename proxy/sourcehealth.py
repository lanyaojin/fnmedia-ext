#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在线源健康画像：按源（source key）统计成功率/延迟/连续失败，用于排序与自动降权。

设计目标：轻量、无外部依赖、进程内热路径零 IO（统计落盘由后台/低频触发）。
- record(key, ok, latency) 记录一次探测/播放结果；
- weight(key) 给出排序权重（越小越靠前）：健康且快的源在前，死源沉底；
- usable(key) 在“死源冷却窗”内返回 False，供搜索/候选短路跳过高失败源；
- 数据持久化到 state/source_health.json，重启不丢历史画像。

这是通用运维能力（不含任何凭据/私有源信息），公开构建保留。
"""
from __future__ import annotations

import json
import os
import threading
import time

_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "source_health.json")
_LOCK = threading.RLock()

# 连续失败达到阈值进入“停用”：阶梯升级（参考 LibreTV：30 分钟 → 24 小时 → 长期，
# 但本复刻版不设永久停用，封顶 3 天——源会波动，维护后可能恢复）。
# 阶梯由累计“死亡次数” deaths 决定：第一次死停 15 分钟（沿用旧值，比 LibreTV
# 的 30 分钟更宽松，避免偶发抖动误伤），第二次死停 24 小时，第三次起停 3 天封顶。
# 复用标准（自动恢复，无需手动）：停用中的源连续 2 次可用即自动恢复——
# 可用指起播成功（record(ok=True)）或管理页探活首字节成功
# （note_probe_success(ok=True)）；单次成功只计入恢复计数，不立即恢复，
# 防一次偶然成功后立即复发抖动。3 天期满也自动清零重算（见 is_dead），
# 同样无需手动恢复；手动恢复（restore）仍可随时提前恢复。
# 注意：搜索“无结果”不算失败——只有起播探测/播放失败才 record(ok=False)，
# 否则小众片名在某源搜不到就会误杀该源。
DEAD_FAILS = 3
RECOVER_OK_THRESHOLD = 2        # 复用标准：连续 2 次可用即自动恢复
DEAD_COOLDOWN = 15 * 60          # 阶梯 1：15 分钟
DEAD_COOLDOWN_2 = 24 * 3600      # 阶梯 2：24 小时
DEAD_COOLDOWN_3 = 3 * 86400      # 阶梯 3：3 天封顶，绝不永久停用
DEAD_COOLDOWN_LONG = DEAD_COOLDOWN_3  # 旧名兼容别名，含义已改为 3 天
TIER_LABELS = {0: "正常", 1: "停用15分钟", 2: "停用24小时", 3: "停用3天"}
DEGRADED_FAIL_RATE = 0.6         # 样本足够时，失败率高于此判差
MIN_SAMPLES = 6                  # 低于此样本不做失败率判级，避免偶发误杀
SLOW_LATENCY = 12.0              # 平均延迟超过此秒视为慢源
_MAX_KEYS = 2000


def _blank() -> dict:
    return {"n": 0, "ok": 0, "lat": 0.0, "fails": 0,
            "last_ok": 0.0, "last_fail": 0.0, "dead_until": 0.0,
            "deaths": 0, "tier": 0, "last_fail_reason": "",
            "claimed_bitrate_bps": None, "claimed_bitrate_at": 0.0,
            "recover_ok": 0}


def _load() -> dict:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {}
        # 兼容旧的长期停用 state（10 年远期 dead_until）：一律钳制到
        # 最后失败时间 + 3 天，封顶规则对旧数据同样生效。
        now = time.time()
        for s in d.values():
            if not isinstance(s, dict):
                continue
            if int(s.get("tier", 0)) >= 3:
                base = s.get("last_fail") or now
                cap = float(base) + DEAD_COOLDOWN_3
                if s.get("dead_until", 0) > cap:
                    s["dead_until"] = cap
        return d
    except Exception:
        return {}


_STATS: dict[str, dict] = _load()


def _save_locked() -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STATS, f, ensure_ascii=False)
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass


def source_key_of(line: str | None, picked: dict | None = None) -> str:
    """从线路名（"src::显示名"）或 picked 记录取稳定的源 key。"""
    if picked:
        for k in ("source", "src", "source_key", "spider_src", "provider_key",
                  "play_from"):
            v = picked.get(k)
            if v:
                return str(v)
    if line and "::" in line:
        return line.split("::", 1)[0]
    return line or ""


def _tier_cooldown(tier: int) -> float:
    if tier <= 1:
        return DEAD_COOLDOWN
    if tier == 2:
        return DEAD_COOLDOWN_2
    return DEAD_COOLDOWN_3


def _restore_locked(s: dict) -> None:
    """清停用态（内部用，调用方须已持锁）：tier/deaths/fails/恢复计数清零，
    保留样本历史（n/ok/lat/last_ok）供排序参考。"""
    s["fails"] = 0
    s["dead_until"] = 0.0
    s["tier"] = 0
    s["deaths"] = 0
    s["recover_ok"] = 0
    s["last_fail_reason"] = ""


def _apply_success_locked(s: dict, now: float) -> bool:
    """一次可用记录的内部处理（调用方须已持锁），返回是否触发自动恢复。

    tier 为 0 时为普通成功：清 fails/dead_until，不动 tier/deaths。
    tier 大于 0 时为恢复计数：连续 RECOVER_OK_THRESHOLD 次可用才恢复，
    单次成功只计 recover_ok、不立即清 dead_until，防抖动源一次偶然成功
    就复活、随即又死造成的反复横跳。
    """
    s["last_ok"] = now
    s["fails"] = 0
    if int(s.get("tier", 0)) <= 0:
        s["recover_ok"] = 0
        s["dead_until"] = 0.0
        return False
    s["recover_ok"] = int(s.get("recover_ok", 0)) + 1
    if s["recover_ok"] >= RECOVER_OK_THRESHOLD:
        _restore_locked(s)
        return True
    return False


def record(key: str, ok: bool, latency: float = 0.0,
           reason: str = "") -> None:
    key = (key or "").strip()
    if not key:
        return
    now = time.time()
    with _LOCK:
        s = _STATS.get(key)
        if s is None:
            if len(_STATS) >= _MAX_KEYS:
                # 超容量：淘汰最久没动静的
                old = min(_STATS, key=lambda k: max(
                    _STATS[k].get("last_ok", 0), _STATS[k].get("last_fail", 0)))
                _STATS.pop(old, None)
            s = _blank()
            _STATS[key] = s
        # 兼容旧 state 文件：缺失的新字段补默认值
        for k, v in _blank().items():
            s.setdefault(k, v)
        s["n"] += 1
        if latency and latency > 0:
            s["lat"] += float(latency)
        if ok:
            s["ok"] += 1
            _apply_success_locked(s, now)
        else:
            s["recover_ok"] = 0
            s["fails"] = int(s.get("fails", 0)) + 1
            s["last_fail"] = now
            if reason:
                s["last_fail_reason"] = str(reason)[:120]
            if s["fails"] >= DEAD_FAILS:
                # 只有从“非停用态”新进入停用才升阶梯；已在停用窗内又失败
                # 不重复升阶（避免一次长故障连升两级）。
                if now >= s.get("dead_until", 0):
                    s["deaths"] = int(s.get("deaths", 0)) + 1
                    s["tier"] = min(int(s["deaths"]), 3)
                s["dead_until"] = now + _tier_cooldown(int(s.get("tier", 1)) or 1)


def stats(key: str) -> dict:
    with _LOCK:
        s = _STATS.get(key)
        if not s:
            return {}
        n = max(1, s.get("n", 0))
        tier = int(s.get("tier", 0))
        bps = s.get("claimed_bitrate_bps")
        if bps:
            if bps >= 1_000_000:
                bitrate_label = f"源标称 {bps / 1_000_000:.0f} Mbps"
            else:
                bitrate_label = f"源标称 {bps / 1_000:.0f} Kbps"
        else:
            bitrate_label = "源未标码率"
        return {
            "key": key,
            "samples": s.get("n", 0),
            "success_rate": round(s.get("ok", 0) / n, 3),
            "avg_latency": round(s.get("lat", 0.0) / n, 2),
            # avg_latency 即起播竞速测到的首字节延迟均值（见 app._probe_latency），
            # 管理页展示时可直接标注为“实测首字节延迟”，不要另起名目。
            "first_byte_latency": round(s.get("lat", 0.0) / n, 2),
            "fails": s.get("fails", 0),
            "last_ok": s.get("last_ok", 0),
            "last_fail": s.get("last_fail", 0),
            "last_fail_reason": s.get("last_fail_reason", ""),
            "dead_until": s.get("dead_until", 0),
            "deaths": s.get("deaths", 0),
            "tier": tier,
            "tier_label": TIER_LABELS.get(tier, "正常"),
            "recover_ok": s.get("recover_ok", 0),
            "claimed_bitrate_bps": bps,
            "claimed_bitrate_label": bitrate_label,
        }


def is_dead(key: str, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    with _LOCK:
        s = _STATS.get(key)
        if not s:
            return False
        if now < s.get("dead_until", 0):
            return True
        # 冷却已过：只清 dead_until，放行重新参与（保留 tier/deaths，
        # 下一次死继续升阶梯，不会因期满就永远停在 15 分钟档）。
        # 真正的清零重算来自复用标准：期满后连续 2 次可用即自动恢复，
        # 届时 tier/deaths 才清零；手动 restore 亦可提前清零。
        if s.get("dead_until"):
            s["dead_until"] = 0.0
        return False


def usable(key: str, now: float | None = None) -> bool:
    """死源冷却窗内返回 False（供搜索阶段短路跳过）。"""
    return not is_dead(key, now)


def _grade(s: dict, now: float) -> int:
    """0 健康 / 1 慢 / 2 高失败率 / 3 停用冷却（15分钟/24小时）/ 4 停用3天窗内。"""
    if now < s.get("dead_until", 0):
        return 4 if int(s.get("tier", 0)) >= 3 else 3
    n = s.get("n", 0)
    if n >= MIN_SAMPLES:
        if (s.get("ok", 0) / max(1, n)) < (1 - DEGRADED_FAIL_RATE):
            return 2
    if n >= MIN_SAMPLES and (s.get("lat", 0.0) / max(1, n)) > SLOW_LATENCY:
        return 1
    return 0


def weight(key: str, now: float | None = None) -> tuple:
    """排序权重，元组越小越优先：健康等级 → 平均延迟 → 成功率。无画像给中性值。"""
    now = now if now is not None else time.time()
    with _LOCK:
        s = _STATS.get(key)
        if not s:
            return (1, 5.0, 1.0)  # 未知源给中间档，给一次机会
        grade = _grade(s, now)
        n = max(1, s.get("n", 0))
        avg = s.get("lat", 0.0) / n
        rate = s.get("ok", 0) / n
        # 停用3天最末；停用冷却次之；高失败率次之；慢源次之；健康按延迟/成功率细排
        return (grade, round(avg, 2), round(1 - rate, 3))


def ordered(keys, now: float | None = None) -> list:
    """按健康权重排序一组源 key（稳定排序，未知源保持相对靠中）。"""
    now = now if now is not None else time.time()
    return sorted(keys, key=lambda k: weight(k, now))


def display_grade(key: str, now: float | None = None):
    """海报/UI 角标用等级：无任何统计样本时返回 None（不显示点），
    避免给从未探测过的新源误标黄/红点。返回 0~4 或 None（4=停用3天窗内）。"""
    now = now if now is not None else time.time()
    with _LOCK:
        s = _STATS.get(key)
        if not s or s.get("n", 0) == 0:
            return None
        return _grade(s, now)


def snapshot() -> dict:
    """管理页用：全部源画像。"""
    now = time.time()
    with _LOCK:
        out = {}
        for k in _STATS:
            st = stats(k)
            st["grade"] = _grade(_STATS[k], now)
            out[k] = st
        return out


def note_claimed_bitrate(key: str, bps: int | None) -> None:
    """记录某源的源标称码率（仅来自 m3u8 主清单 BANDWIDTH 的解析值）。

    bps 为 None 表示该源主清单未标码率：不覆盖已有的标称值，只是不更新；
    调用方展示时对 None 显示“源未标码率”。本函数不做任何网络请求。
    """
    key = (key or "").strip()
    if not key or not bps:
        return
    with _LOCK:
        s = _STATS.get(key)
        if s is None:
            s = _blank()
            _STATS[key] = s
        s["claimed_bitrate_bps"] = int(bps)
        s["claimed_bitrate_at"] = time.time()


def note_probe_success(key: str, ok: bool = True) -> bool:
    """管理页探活的可用记录（不计入播放样本 n/ok，不计失败死亡）。

    探活首字节成功时调用 ok=True：按与 record(ok=True) 相同的复用标准
    累计 recover_ok，连续 RECOVER_OK_THRESHOLD 次即自动恢复，返回是否恢复。
    探活失败时调用 ok=False：只清 recover_ok（连续链断开），不记 fails、
    不升阶梯——探活失败可能是 NAS 侧网络抖动，不是源死亡的定罪证据。
    本函数立即落盘（管理页触发，非热路径）。
    """
    key = (key or "").strip()
    if not key:
        return False
    with _LOCK:
        s = _STATS.get(key)
        if s is None:
            s = _blank()
            _STATS[key] = s
        for k, v in _blank().items():
            s.setdefault(k, v)
        if not ok:
            s["recover_ok"] = 0
            _save_locked()
            return False
        restored = _apply_success_locked(s, time.time())
        _save_locked()
        return restored


def restore(key: str | None = None) -> None:
    """一键恢复：只清停用态（fails/dead_until/tier/deaths/失败原因/恢复计数），
    保留样本历史（n/ok/lat/last_ok），恢复后该源立即重新参与排序与搜索。
    与 reset()（清空该源全部历史）不同，恢复不应抹掉画像。
    """
    with _LOCK:
        keys = [key] if key else list(_STATS.keys())
        for k in keys:
            s = _STATS.get(k)
            if s is None:
                continue
            _restore_locked(s)
        _save_locked()


def reset(key: str | None = None) -> None:
    with _LOCK:
        if key:
            _STATS.pop(key, None)
        else:
            _STATS.clear()
        _save_locked()


def flush() -> None:
    with _LOCK:
        _save_locked()
