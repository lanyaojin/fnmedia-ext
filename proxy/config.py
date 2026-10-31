#!/usr/bin/env python3
"""fnmedia-ext 运行配置（可热加载）。state/config.json。"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

import features
from providers import DEFAULT_SOURCES

_STATE = Path(__file__).resolve().parent.parent / "state" / "config.json"
_LOCK = threading.RLock()

# 噪音关键词：标题或备注命中则不注入（电影解说、预告、花絮、AI 漫剧等）
# 复刻定制：加“短剧”，使搜索结果里不再出现短剧（海量库分类聚合本身已排除短剧子类）。
JUNK_KEYWORDS = [
    "电影解说", "影视解说", "解说", "预告片", "预告", "花絮", "抢先", "枪版",
    "AI漫剧", "漫剧", "动态漫画", "彩蛋", "删减", "片场", "开机", "特辑",
    "短剧",
]

DEFAULTS = {
    "sources": [dict(s) for s in DEFAULT_SOURCES],
    # 被删除的内置源 id（自定义源删除时直接从 sources 移除）
    "removed_ids": [],
    "max_per_keyword": 12,
    "filter_junk": True,
    "junk_keywords": JUNK_KEYWORDS,
    # 在线资源前排展示（在线片库更齐全，默认前排）
    "online_first": True,
    # 播放线路健康探测与自动换线：当前线路解析失败/文件不存在时，自动探测同集其他
    # 线路并切到首个可用线路；坏线短时记忆，起播自动跳过（关闭则保持纯手动选线）
    "line_autofail": True,
    # 完全自动选最快线：起播时并行竞速探测前若干条候选线路，选首字节延迟最低的线；
    # 最快线短时记忆，同一集后续/重放稳定用它（手动在播放器切线仍被尊重）
    "line_fastest": True,
    # 自动选最快线时最多参与竞速的候选线路数（控制起播开销）
    "line_fastest_max": 6,
    # 竞速超时预算（秒）：超时未响应的线路本轮判慢，不阻塞起播
    "line_fastest_timeout": 4.0,
    # 默认线（高质量源）领先优势（秒）：它在该优势内返回就算赢，避免无谓抖动
    "line_fastest_prefer": 0.25,
    # 虚拟“热门榜单”四库媒体库（豆瓣真实周榜/标签榜）
    "hot_library_enabled": True,
    # 虚拟“海量分类”四库媒体库（苹果 CMS 分类深翻）。与热门榜单相互独立开关。
    "catalog_library_enabled": True,
    # 复刻定制：按分类隐藏媒体库卡片（仅影响媒体库列表展示，不拦搜索，
    # 也不删后台数据）。默认隐藏动漫：电影 / 剧集 / 综艺显示，动漫不显示。
    # 可在管理页 /v/api/ext/config 里改（仅接受 movie / tv / anime / variety）。
    # 注意：已有安装的 state/config.json 里没有这两个键时取此处默认值；
    # 若以后在管理页保存过，则以保存的值为准。
    "hidden_catalog_categories": ["anime"],
    "hidden_chart_categories": ["anime"],
    # 复刻新增：源健康阶梯停用总开关（默认开）。关闭时，停用窗内的源仍参与
    # 搜索（仅排序降权），用于“健康数据异常时先恢复播放、再查原因”的回滚。
    "source_health_tiered": True,
    # 复刻新增：自由筛选预设（绝非固定“儿童模式”）。
    # 结构：{预设名: {hidden_catalog_categories?, hidden_chart_categories?,
    #                 junk_keywords?, enabled_sources?}}，键缺失表示激活时不动该项。
    # 预设名由用户自定，代码不预置任何预设。
    "filter_presets": {},
    "active_filter_preset": None,
    # 复刻新增：海量库自定义子库（GoFilm 两级分类思路：大库下再分子库）。
    # 每项：{id, title, parent(movie|tv), keywords[体裁/类型关键字],
    #        hidden?}。匹配只看条目的 type_name（CMS 子分类名，如 动作片/国产剧），
    # 不看标题（标题含关键字会误分类），vod_class 因 _map_vod 未保留而不用。
    "custom_catalog_libraries": [],
    # 是否把每个启用的 TVBox 加固源（csp_*Guard，如比特/糯米/花花）生成为一张
    # 独立“专区媒体库”卡片。默认关闭：加固源只作为播放时可切换的线路/搜索源，
    # 不在媒体库列表里产生空库。需要专区墙时可在管理页打开。
    "guard_zone_enabled": False,
    # 是否在飞牛原生媒体库注入单张「🛡加固影视」聚合库（源→分类片单，后台预取
    # 缓存、请求即时返回）。与 guard_zone_enabled（每源一张专区墙）相互独立。
    "guard_native_enabled": True,
    # 置顶的虚拟媒体库 guid 集合（打开=在飞牛影视媒体库列表置顶，关闭=普通顺序）。
    "pinned_libs": [],
    "hot_library_title": "🌐在线热门",
    "hot_limit": 300,          # 四库合计保留条数上限
    "hot_per_source": 120,    # 每个资源站最多抓取的可播条目
    "hot_pages_per_source": 6,  # 每个资源站翻“最近更新”的页数（每页约20条）
    "hot_refresh_interval": 6 * 3600,
    # 源质量优先级：source_id -> 权重（越高越优先）。未列出的源取内省默认（见
    # providers.source_priority）。热门榜单点击搜源时按 (优先级, 直链数) 排序。
    "source_priority": {},
    # 热门资源解析是否优先高质量源（关闭则只按可播直链数）
    "prefer_quality_source": True,
    "admin_token": "",
    # 仅用于「拉取订阅资源」的可选 HTTP(S) 代理（订阅配置 / spider jar / DRPY 脚本）。
    # 播放、聚合搜索、CMS 探测等一律不走此代理（产品客户端保持 trust_env=False 直连）。
    # 形如 http://user:pass@host:port ；留空表示直连。
    "subscription_proxy": "",
    # 检查/下载插件更新安装包时可选的 HTTP(S) 代理（仅更新用途）。
    # 留空则回退 subscription_proxy，再不行直连。仅存于本机 state，不随包分发。
    "update_proxy": "",
    # m3u8 流层去广告总开关（TVBox/OK影视 Pro rules 引擎 + 通用启发式）。
    "ad_filter_enabled": True,
    # 播放时动态检测（无订阅 rules 时，按 DISCONTINUITY 分段+时长/CDN/位置识别广告段）。
    "ad_filter_dynamic": True,
}

# 已废弃、需从旧配置里清除的键
_DEPRECATED = {"public_base", "strm_root"}
_BUILTIN_IDS = {s["id"] for s in DEFAULT_SOURCES}


class Config:
    def __init__(self):
        self._cfg: dict = {}
        self._mtime = 0
        self.load()


    def load(self):
        with _LOCK:
            try:
                j = json.loads(_STATE.read_text())
            except Exception:
                j = {}
            cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
            cfg.update(j)
            for k in list(cfg.keys()):
                if k in _DEPRECATED:
                    cfg.pop(k, None)
            removed = set(cfg.get("removed_ids", []))

            # 源列表：以内置为底（跳过被删除的），保留用户的启用/改名/改地址，
            # 再追加用户自定义源。
            saved = j.get("sources") if isinstance(j.get("sources"), list) else []
            saved_by_id = {s.get("id"): s for s in saved if isinstance(s, dict)}
            merged = []
            for s in DEFAULT_SOURCES:
                if s["id"] in removed:
                    continue
                cur = saved_by_id.get(s["id"], {})
                m = dict(s)
                m.update({k: cur[k] for k in ("enabled", "name", "api") if k in cur})
                m["builtin"] = True
                m["standalone"] = bool(cur.get("standalone", False))
                m["loose_filter"] = bool(cur.get("loose_filter", False))
                merged.append(m)
            for sid, s in saved_by_id.items():
                if sid in _BUILTIN_IDS or sid in removed:
                    continue
                m = {k: s.get(k) for k in ("id", "name", "api", "enabled")}
                m["enabled"] = bool(s.get("enabled", True))
                m["standalone"] = bool(s.get("standalone", False))
                m["loose_filter"] = bool(s.get("loose_filter", False))
                m["builtin"] = bool(s.get("builtin", False))
                merged.append(m)
            cfg["sources"] = merged
            cfg["removed_ids"] = sorted(removed)
            # 旧版本（单库/小数量）保存过的热门参数自动升级到深度爬取规模；
            # 仅在检测到旧小值时迁移一次，用户之后自定义的大值不会被覆盖。
            migrated = False
            try:
                if int(cfg.get("hot_limit", 0)) <= 120 and int(DEFAULTS["hot_limit"]) > 120:
                    cfg["hot_limit"] = DEFAULTS["hot_limit"]; migrated = True
                if int(cfg.get("hot_per_source", 0)) <= 40:
                    cfg["hot_per_source"] = DEFAULTS["hot_per_source"]; migrated = True
                if not cfg.get("hot_pages_per_source"):
                    cfg["hot_pages_per_source"] = DEFAULTS["hot_pages_per_source"]; migrated = True
            except (TypeError, ValueError):
                pass
            if migrated:
                j.update({k: cfg[k] for k in
                          ("hot_limit", "hot_per_source", "hot_pages_per_source")})
                try:
                    _STATE.write_text(json.dumps(j, ensure_ascii=False, indent=2))
                except Exception:
                    pass
            if not cfg.get("admin_token"):
                import secrets
                cfg["admin_token"] = secrets.token_hex(8)
            self._cfg = cfg
            try:
                self._mtime = _STATE.stat().st_mtime
            except FileNotFoundError:
                self._mtime = 0

    def maybe_reload(self):
        try:
            mt = _STATE.stat().st_mtime
        except FileNotFoundError:
            return
        if mt != self._mtime:
            self.load()

    def get(self):
        with _LOCK:
            return json.loads(json.dumps(self._cfg))

    def enabled_sources(self):
        self.maybe_reload()
        with _LOCK:
            return [dict(s) for s in self._cfg["sources"] if s.get("enabled")]

    def all_sources(self):
        self.maybe_reload()
        with _LOCK:
            return [dict(s) for s in self._cfg["sources"]]

    def save(self, patch: dict):
        with _LOCK:
            cfg = self.get()
            for k, v in patch.items():
                if k in cfg:
                    cfg[k] = v
            self._persist(cfg)

    def _persist(self, cfg: dict):
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        tmp.replace(_STATE)
        self._cfg = cfg
        self._mtime = _STATE.stat().st_mtime

    @staticmethod
    def make_custom_id(api: str) -> str:
        return "custom_" + hashlib.md5(api.strip().encode()).hexdigest()[:10]

    def add_source(self, name: str, api: str, enabled: bool = True,
                   standalone: bool = False) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            api = (api or "").strip()
            name = (name or "").strip()
            if not api.startswith(("http://", "https://")):
                return False, "API 地址需以 http(s):// 开头"
            if not name:
                name = api.split("/")[2]
            sid = self.make_custom_id(api)
            norm = api.rstrip("/").lower()
            if any(s["id"] == sid or s.get("api", "").rstrip("/").lower() == norm
                   for s in cfg["sources"]):
                return False, "该资源站地址已存在"
            cfg["sources"].append(
                {"id": sid, "name": name, "api": api,
                 "enabled": bool(enabled), "standalone": bool(standalone),
                 "builtin": False})
            self._persist(cfg)
            return True, sid

    def source_priorities(self) -> dict:
        with _LOCK:
            return dict(self.get().get("source_priority") or {})

    def set_source_priority(self, mapping: dict) -> tuple[bool, str]:
        """整体保存源优先级（id -> 整数权重），忽略未知源与非法值。"""
        with _LOCK:
            cfg = self.get()
            valid = {s["id"] for s in cfg["sources"]}
            out = {}
            for k, v in (mapping or {}).items():
                if k in valid:
                    try:
                        out[k] = max(-100, min(100, int(v)))
                    except (TypeError, ValueError):
                        continue
            cfg["source_priority"] = out
            self._persist(cfg)
            return True, "ok"

    def set_subscription_proxy(self, proxy_url: str) -> tuple[bool, str]:
        """保存「订阅资源抓取」专用代理（空串=直连）。仅用于订阅配置/jar/DRPY脚本，
        不影响播放与搜索。仅接受 http(s)/socks5 URL 形态。"""
        from urllib.parse import urlparse
        p = (proxy_url or "").strip()
        if p:
            u = urlparse(p)
            if u.scheme not in ("http", "https", "socks5", "socks5h") or not u.hostname:
                return False, "代理地址格式应为 http://user:pass@host:port"
        with _LOCK:
            cfg = self.get()
            cfg["subscription_proxy"] = p
            self._persist(cfg)
            return True, "ok"

    def set_standalone(self, sid: str, on: bool) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            target["standalone"] = bool(on)
            self._persist(cfg)
            return True, "ok"

    def pinned_lib_guids(self) -> set:
        with _LOCK:
            try:
                return set(self.get().get("pinned_libs") or [])
            except TypeError:
                return set()

    def set_lib_pinned(self, guid: str, on: bool) -> tuple[bool, str]:
        """置顶/取消置顶某个虚拟媒体库（guid）。"""
        if not isinstance(guid, str) or not guid:
            return False, "guid 非法"
        with _LOCK:
            cfg = self.get()
            cur = [g for g in (cfg.get("pinned_libs") or []) if isinstance(g, str)]
            if on:
                if guid not in cur:
                    cur.append(guid)
            else:
                cur = [g for g in cur if g != guid]
            cfg["pinned_libs"] = cur
            self._persist(cfg)
            return True, "ok"

    def set_loose_filter(self, sid: str, on: bool) -> tuple[bool, str]:
        """放宽正片（标题/备注噪音）过滤；分类层面的红线不受影响。"""
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            target["loose_filter"] = bool(on)
            self._persist(cfg)
            return True, "ok"


    def remove_source(self, sid: str) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s["id"] == sid), None)
            if not target:
                return False, "源不存在"
            if target.get("builtin"):
                # 内置源不物理删除，标记移除（可在重置时恢复）
                if sid not in cfg["removed_ids"]:
                    cfg["removed_ids"].append(sid)
            cfg["sources"] = [s for s in cfg["sources"] if s["id"] != sid]
            self._persist(cfg)
            return True, "ok"

    def restore_builtin(self) -> tuple[bool, str]:
        with _LOCK:
            cfg = self.get()
            cfg["removed_ids"] = []
            self._persist(cfg)
            self.load()
            return True, "ok"

    @staticmethod
    def normalize_keywords(raw) -> list[str]:
        """把逗号/换行/空白分隔的输入清洗成去重关键词列表。"""
        if isinstance(raw, str):
            parts = re.split(r"[,，、\n\r;；\s]+", raw)
        elif isinstance(raw, (list, tuple)):
            parts = []
            for item in raw:
                parts.extend(re.split(r"[,，、\n\r;；\s]+", str(item)))
        else:
            return []
        out, seen = [], set()
        for p in parts:
            k = p.strip()
            if k and k not in seen and len(k) <= 30:
                seen.add(k)
                out.append(k)
        return out

    def set_junk_keywords(self, raw) -> list[str]:
        kws = self.normalize_keywords(raw)
        self.save({"junk_keywords": kws})
        return kws

    def reset_junk_keywords(self) -> list[str]:
        self.save({"junk_keywords": list(JUNK_KEYWORDS)})
        return list(JUNK_KEYWORDS)

    # ---------------- 自由筛选预设（复刻新增） ----------------
    _PRESET_CATS = {"movie", "tv", "anime", "variety"}

    @classmethod
    def _clean_preset(cls, data: dict) -> dict:
        out = {}
        if isinstance(data.get("hidden_catalog_categories"), list):
            seen, clean = set(), []
            for c in data["hidden_catalog_categories"]:
                if c in cls._PRESET_CATS and c not in seen:
                    seen.add(c); clean.append(c)
            out["hidden_catalog_categories"] = clean
        if isinstance(data.get("hidden_chart_categories"), list):
            seen, clean = set(), []
            for c in data["hidden_chart_categories"]:
                if c in cls._PRESET_CATS and c not in seen:
                    seen.add(c); clean.append(c)
            out["hidden_chart_categories"] = clean
        if "junk_keywords" in data:
            out["junk_keywords"] = cls.normalize_keywords(data.get("junk_keywords"))
        if isinstance(data.get("enabled_sources"), list):
            out["enabled_sources"] = [str(x) for x in data["enabled_sources"]
                                      if isinstance(x, str) and x]
        return out

    def save_filter_preset(self, name: str, data: dict) -> tuple[bool, str]:
        name = (name or "").strip()
        if not name or len(name) > 30 or "/" in name or "\\" in name:
            return False, "预设名非法（1-30 字，不能含 /）"
        clean = self._clean_preset(data or {})
        with _LOCK:
            cfg = self.get()
            presets = dict(cfg.get("filter_presets") or {})
            presets[name] = clean
            cfg["filter_presets"] = presets
            self._persist(cfg)
        return True, "ok"

    def delete_filter_preset(self, name: str) -> tuple[bool, str]:
        name = (name or "").strip()
        with _LOCK:
            cfg = self.get()
            presets = dict(cfg.get("filter_presets") or {})
            if name not in presets:
                return False, "预设不存在"
            presets.pop(name, None)
            cfg["filter_presets"] = presets
            if cfg.get("active_filter_preset") == name:
                cfg["active_filter_preset"] = None
            self._persist(cfg)
        return True, "ok"

    def activate_filter_preset(self, name: str | None) -> tuple[bool, str]:
        """激活预设：按预设内容改写当前隐藏分类/噪音词/源启用态，并记 active。

        name 为 None/空 = 仅清除 active 标记，不改回任何设置（见文件头说明）。
        源启用：预设含 enabled_sources 时按“列出即启用、未列出即停用”精确应用；
        不含该键时源启用态不动。应用后搜索缓存由调用方（app 层）清除。
        """
        with _LOCK:
            cfg = self.get()
            if not name:
                cfg["active_filter_preset"] = None
                self._persist(cfg)
                return True, "ok"
            presets = cfg.get("filter_presets") or {}
            preset = presets.get(name)
            if not isinstance(preset, dict):
                return False, "预设不存在"
            for k in ("hidden_catalog_categories", "hidden_chart_categories",
                      "junk_keywords"):
                if k in preset:
                    cfg[k] = preset[k]
            if "enabled_sources" in preset:
                on = set(preset["enabled_sources"])
                for src in cfg.get("sources", []):
                    src["enabled"] = src.get("id") in on
            cfg["active_filter_preset"] = name
            self._persist(cfg)
        return True, "ok"

    # ---------------- 海量库自定义子库（复刻新增） ----------------
    @classmethod
    def clean_custom_library(cls, data: dict) -> tuple[dict | None, str]:
        import re as _re
        if not isinstance(data, dict):
            return None, "子库定义须为对象"
        lid = str(data.get("id") or "").strip()
        if not _re.match(r"^[a-z0-9_-]{1,32}$", lid):
            return None, "子库 id 非法（仅小写字母/数字/_/-，1-32 字）"
        title = str(data.get("title") or "").strip()[:20]
        if not title:
            return None, "子库标题不能为空"
        parent = str(data.get("parent") or "").strip()
        if parent not in ("movie", "tv"):
            return None, "parent 只能是 movie 或 tv（子库只从海量电影/剧集切）"
        kws = cls.normalize_keywords(data.get("keywords"))
        if not kws or len(kws) > 10:
            return None, "keywords 须为 1-10 个关键字"
        return {"id": lid, "title": title, "parent": parent,
                "keywords": kws, "hidden": bool(data.get("hidden", False))}, "ok"

    def save_custom_library(self, data: dict) -> tuple[bool, str]:
        clean, msg = self.clean_custom_library(data)
        if clean is None:
            return False, msg
        with _LOCK:
            cfg = self.get()
            libs = [l for l in (cfg.get("custom_catalog_libraries") or [])
                    if isinstance(l, dict) and l.get("id") != clean["id"]]
            libs.append(clean)
            cfg["custom_catalog_libraries"] = libs
            self._persist(cfg)
        return True, "ok"

    def delete_custom_library(self, lid: str) -> tuple[bool, str]:
        lid = (lid or "").strip()
        with _LOCK:
            cfg = self.get()
            libs = [l for l in (cfg.get("custom_catalog_libraries") or [])
                    if isinstance(l, dict)]
            if not any(l.get("id") == lid for l in libs):
                return False, "子库不存在"
            cfg["custom_catalog_libraries"] = [l for l in libs if l.get("id") != lid]
            self._persist(cfg)
        return True, "ok"

    def is_junk(self, title: str, remarks: str = "") -> bool:
        if not self._cfg.get("filter_junk", True):
            return False
        kws = self._cfg.get("junk_keywords", JUNK_KEYWORDS)
        hay = f"{title} {remarks}"
        return any(k and k in hay for k in kws)


config = Config()
