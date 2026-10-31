# 复刻定制说明（lanyaojin/fnmedia-ext）

本复刻基于上游 yujian0815/fnmedia-ext v0.2.39（2026-10-31，提交 9f6e5fe）。
每周五早上 3 点对比上游并合入更新，合入时保住下面这些定制；
合完只汇报，不自动打 FPK、不自动装到 NAS，装不装由用户确认。

## 定制 1：补上游 lite 版缺失的两个函数（2026-10-31 实测补丁）

- `proxy/providers.py`：`source_priority(source, overrides)`。
  上游 lite 版没有这个函数，导致搜索聚合崩溃、`/v/api/ext/status` 500、
  所有片的 preset.m3u8 返回 503“暂无可播片源”。
  语义与 `config.set_source_priority` 一致：用户权重优先、钳制 ±100，
  未列出取源自身 priority/weight/score，否则 0。
- `proxy/app.py`：`_pan_match(url)`。
  上游 lite 版没有这个函数，导致 preset 500、播放器报“Trim PRC 查询超时”。
  CMS 直链返回 None，仅常见网盘域名返回带 name 的 dict。

若上游后续版本已补这两个函数，同步时以官方版本为准，删掉本复刻的补丁。

## 定制 2：按分类隐藏媒体库（2026-10-31 用户要求）

- 配置键：`hidden_catalog_categories`（海量四库）、`hidden_chart_categories`（热门榜单四库）。
- 默认值均为 `["anime"]`：电影、剧集、综艺显示，动漫不显示，综艺暂不限制。
- 生效位置：`mediadb/list`、`mediadb/sum` 跳过被隐藏的分类；
  `/v/api/ext/libs` 给每个库加 `hidden` 字段，供管理台展示。
- 改法：POST `/v/api/ext/config`，body 里带
  `{"hidden_catalog_categories": [...], "hidden_chart_categories": [...]}`，
  仅接受 `movie` / `tv` / `anime` / `variety`，非法值丢弃、去重保序。
- 范围说明：这只是展示层隐藏，不拦搜索、不删后台数据。
  搜片名仍能搜到动漫（例如死神、火影忍者、海贼王），孩子在库里翻不到动漫区。
  直接拿库 guid 打开被隐藏的库也不拦，这是展示隐藏不是访问控制。

## 定制 3：短剧不显示（2026-10-31 用户要求）

- 海量库分类聚合本身已排除含“短剧”的子类，所以短剧没有独立的库卡片。
- 搜索层面：`proxy/config.py` 的 `JUNK_KEYWORDS` 默认列表加了“短剧”，
  标题或备注含短剧的搜索结果不再注入。
- 已有安装注意：`state/config.json` 里已保存的 `junk_keywords` 会覆盖默认值，
  所以在已有 NAS 上装了复刻版 FPK 后，要去管理页“噪音关键词”点“恢复默认”
  （或手动加“短剧”），短剧搜索过滤才生效；库隐藏是新增键、没有旧值覆盖，
  装完重启即生效。

## 当前 NAS 实况（2026-10-31）

NAS 上正在跑的是上游 v0.2.39-lite + 定制 1 的两个补丁（2026-10-31 直接在 NAS 上打的），
还没有装定制 2、定制 3 的复刻版。复刻版打 FPK、装 NAS 须用户另外确认，不自动做。
