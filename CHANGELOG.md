# 更新日志

版本号以 `config.py` 的 `APP_VERSION` 为唯一真源；发版由 `build_release.py` 负责，
窗口标题、exe 版本资源、便携目录 / zip 名、CHANGELOG 段落和 git tag 全部由它派生。
版本规则遵循语义化版本：修复 bug 升 patch，功能/行为变更升 minor，破坏性变更升 major。

## [v1.7.1] - 2026-11-15

> 这两处是 v1.7.0 发行之后发现的修复，随 v1.7.1 发布；v1.7.0 的 zip 不含它们。
> 两处都是 bug 修复，按项目规则升 patch。

一次全量同步连续跑 3 小时 20 分后用户反馈「界面打不开」。只读诊断：进程存活、
窗口可见未最小化、`Responding=True`、日志持续写入；真实原因是主线程反复卡顿
（当前轮转日志 6170 行里 63 次 `UI stall`，峰值 45.7s，37 次栈顶在
`transfer.add_item`），并顺带发现 66 条「跳转到不允许的域名」下载失败。

### 修复

- **传输面板攒批刷新**：`transfer.py` 新增 `add_items_batch()`，把 `add_item`
  的重绘开关上移到调用方，整批只 `setUpdatesEnabled` 一次；`_add_item_now`
  保持原有行复用语义（满 60 行零新建 QWidget）。
  `app.py` 的 `_dl_item_started` 改为把 (key, text) 攒进队列，120ms 定时器
  合并提交；`_dl_item_progress` / `_dl_item_done` / `_dl_item_error` /
  `_dl_item_skipped` / `_maybe_finish_downloads` / `_begin_download_stats`
  在查行前先 `_flush_transfer_adds()`，保证进度与状态不会写到还不存在的行上。
  `parallel.py` 进度节流 0.12s → 0.25s（8 并发下约 66 → 32 次信号/秒）。
- **CDN 轮换域名**：`downloader._is_allowed_host` 新增 `_maybe_b64` /
  `_origin_hints` / `_is_video_like`，识别 `real_host`、`adi`（base64）和
  `http_x-tos-host`，配合 `/video/tos/` 特征放行；新增
  `_is_allowed_redirect`，用于「初始 URL 已过白名单」的 302 链，要求标准
  `tos-cn-` 路径形态 + 至少一个 GSLB 分发参数，纯 IP / localhost / 普通路径
  一律拒绝。`_download_to_file` 的 hop 与最终 URL 检查都接上这两层判定。

### 验证

- `tests/test_longrun_fixes.py` 15 个离线用例：3 条日志里的真实轮换 CDN URL、
  base64 解码、旧版来源标记回归、5 个伪造反例、302 链端到端放行/拒绝、
  批量插入与满 60 行行复用、以及接线回归。
- 全量 `python run_tests.py` **421 个用例通过**（含本轮新增 15 个）。
- 升级后**需重启程序才生效**；已下载作品靠磁盘判重，重启不会重复下载。

## [v1.7.0] - 2026-11-15

本版包含三块改动：**网络策略加固**、**背景音乐补齐**、**导入 txt 对齐通讯录**，
并一并带上 2026-11-13 那轮静态审计修复。全量 `python run_tests.py`
**406 个用例全部通过**（本版新增 71 个离线用例）；整套约 4~5 分钟，
慢在离屏 GUI 冒烟，前台跑可能被超时信号打断，建议后台执行并取日志末尾的 `OK`。

借鉴 [Scrapling](https://github.com/D4Vinci/Scrapling) 的 Fetcher / Spider 分层思路，
对抓取链路做**重试 / 限流 / 反风控**加固。**零新依赖**（仍是 requests + PySide6 +
Playwright），PyInstaller 打包形态、五账号登录态、作品目录结构、WebDAV 通道
全部保持原样。设 `JINGYING_NET_POLICY=0` 可一键回退。

> 为什么不是直接装 Scrapling：它最强的 CSS/XPath 自适应选择器针对 HTML DOM，
> 而净影取的是页面自己发出的 XHR JSON（`resolver.js` 劫持 `Response.json`），
> 两者不对口；引入 `scrapling[fetchers]` 还会带来浏览器/指纹依赖，拖累免安装 exe。
> 本轮借鉴的是它的分层 Fetcher、会话复用、自动重试、AutoThrottle 与
> blocked-request detection 这些架构手段。

### 新增

- `net_policy.py`：统一网络策略层，不依赖任何业务模块。
  - `RetryPolicy`：指数退避 + 抖动 + 上限；`retry_sync` / `retry_async`
    统一封装「限流 → 尝试 → 断言 → 退避重试 → 结构化诊断」，所有等待
    按 0.1s 分段，取消信号随时可打断。
  - `TokenBucket` + `AutoThrottle` + `DomainThrottle`：按 `page` / `cdn` /
    `asset` 三个逻辑通道限流；平时延迟为 0，只有 403/429/5xx、风控特征或
    响应明显变慢才逐步加延迟，恢复后缓慢回落；尊重 `Retry-After` 但封顶 5 秒。
  - `check_payload` / `assert_payload` / `is_challenge`：把
    `original_stream.assert_detail` 那种「HTTP 200 但字段空了也算失败」的判定
    推广到作品详情和主页作品列表，风控/人机校验不再静默降级。
  - `build_session` / `session_for`：统一 Retry（补 408/425）、UA、可选代理与
    每线程连接池；`FetchDiagnostics` 环形缓冲记录成败、原因、耗时、自适应延迟。
- `tests/test_net_policy.py`：40 个离线用例，假时钟推进，覆盖退避抖动、令牌桶、
  AutoThrottle、`Retry-After` 封顶、坏数据断言、风控识别，以及「策略确实接进
  `parallel` / `parser` / `downloader`」的接线回归。不联网、不启浏览器、不真的 sleep。

### 接线与行为变化

- `downloader._get_session()` 改为委托 `net_policy.session_for("cdn")`（保留函数名，
  离线测试仍可 patch）；`_download_to_file` 在**每个媒体请求前**按 `cdn` 通道取令牌，
  等待期间响应取消。
- `parallel._run_one` 的「固定 `sleep(1.0)` 再试一次」换成带退避的策略重试
  （默认 3 次）；用户取消与旧轮次仍立即结束，不重试。
- `parser.resolve_video`：三处 `goto` 改走 `_goto_with_retry`（403/429/5xx 明确判被限流）；
  等待预算切成 80% + 20%，首轮没拿到数据时重载页面再试一轮，**总超时不变**；
  详情做骨架断言，坏数据记带原因的警告后才降级；人机校验改为**始终**检测
  （以前只有打开「原画质」开关才检测）。
- `parser.resolve_profile`：主页导航同样带重试，每次滚动前按 `page` 通道取令牌；
  `/aweme/post` 缺 `aweme_list` 时记明确警告，不再被当成「还没加载出来」。
- `app.py` 封面与 `author_assets.py` 头像的裸 `requests.get` 收敛到 `asset`
  通道统一会话 + 重试。
- `contacts_sync._sync_failed` 把错误里的频控/校验信号回灌策略，后续作者自动放慢。

### 背景音乐补齐

起因：作品 `7625256716829025253` 只有 `图片_01.webp` + `动图_01.mp4` + `文案.txt`
没有音乐。查证是 `models._music_url_from()` 从 `music` 节点取不到播放地址，
`downloader.download_work` 的 `if info.music_url:` 直接跳过，全程静默；
同作者同批另外 4 个实况图集都有 `音乐.mp3`，排除流程/清理/图集类型问题。
全库约 4~6% 作品没有音乐，属正常比例。

- 缺音乐时记一条带原因的 INFO 日志（区分「没有 music 节点」与「music 节点缺
  播放地址」），不再静默跳过。
- 新增 `downloader.has_music_file()` / `download_music_only()` 与
  `contacts_sync.music_backfill_needed()`；通讯录每轮同步对「已下载 + 本次拿到
  音乐地址 + 本地还没有 `音乐.*`」的作品后台补下音乐，状态栏显示数量。
- 补下载走独立轻量通道（最多 3 并发 + `cdn` 令牌桶），**不进 DownloadManager**：
  老作品已完成下载，重走队列会用只有 `music_path` 的结果覆盖历史里的
  `video_path` / `album_paths`。成功的文件自动加入 WebDAV 上传队列。
- 只处理新结构散文件目录；tar 包、旧结构、媒体已被「上传后清理」删掉的作品
  一律跳过；失败不落盘，下轮自然重试，不影响同步成败。
- `tests/test_music_backfill.py` 新增 18 个离线用例（本地 HTTP 桩）：补齐判定、
  已有音乐/无地址/目录不存在/取消、真实写入与日志、以及接线回归。

### 导入 txt 时对齐通讯录

原来「导入 txt」和「批量导入」共用 `_import_authors_text()`，只增不删。现在
**只有「导入 txt」**会把通讯录对齐成文件里的作者集合：txt 里没有的作者会被移除。
「批量导入」保持纯增量。

- `store.py` 新增 `authors_not_in()`（只读预览）与 `remove_authors()`
  （批量过滤 + **一次**落盘）；逐个调 `remove_author()` 会为每位作者重写一遍
  几百 KB 的 `authors.json`，主线程会明显卡住。
- 删除前弹确认框：显示 txt 识别数 / 通讯录现有数 / 将移除数与前 5 个名字，
  默认「否」；取消则**整次导入作废**（连新增都不做），避免半套结果。
- 同步中、txt 无法识别任何作者、通讯录已完全包含 txt 这几种情况都不会删人。
- 只移除通讯录记录，不删已下载作品 / 历史 / 作者资料；同步清理
  `_sync_failed_objs` 并刷新「重试失败」按钮；状态栏显示移出数量。
- `tests/test_author_prune.py` 13 个用例：批量删除与落盘、幂等、确认「是/否」、
  同步中阻断、批量导入仍只增、无法解析不删、接线回归
  （用 MagicMock 当 self 调未绑定方法，store 与 links 是真的）。
- 相关回归子集 171 个用例通过；上一轮完整套件 393 个用例通过。

### 兼容性

正常节奏下令牌桶不会触发等待（速率取得比现有实际节奏宽松），AutoThrottle 初始
延迟为 0，因此成功路径不会变慢；总开关 `JINGYING_NET_POLICY=0` 可完全停用新策略。
各通道速率、重试次数与退避参数、代理、UA 池均可通过环境变量覆盖，详见 README
「网络策略」一节。

## 附：2026-11-13 未发版改动（随 v1.7.0 一并发布）

本轮是**静态审计 + 低风险修复**：未修改 `config.APP_VERSION`，未执行发版，
改动可直接随下次发版一并提交。改动前源码备份在
`.backup_opt/before_20261113_opt/`；新增 16 个回归用例，集中在
`tests/test_opt_20261113.py`。全量 `python run_tests.py` **334 个用例全部通过**
（改动前基线 318 个全部通过）。

### 修复的缺陷

- **换源重试会把两段不同码流拼成一个坏 mp4**：原片 CDN 直链失败后
  `download_work` 会重新回捞（可能从原片降到最高转码档），但第一次留下的
  `.part` 仍在，`_download_to_file` 带 `Range: bytes=N-` 去请求新 URL，
  服务器返回 206 时以 `ab` 追加，最后 `os.replace` 出一个损坏的视频。
  现在给 `_download_to_file` 加 `resume` 参数，重试一律从头覆盖写；
  正常续传语义不变（新增 3 个正反例）。
- **传输列表超过 60 个任务后新任务不再显示**：`TransferPanel.add_item` 在
  `len(self._rows) >= 60` 时提前 return，让下面写好的「插入最前 + 裁剪最旧」
  循环成了死代码——v1.6.6 那轮 6060 个作品，界面只显示前 60 个。
  **中途有一版修复引入回归并已撤回**：直接启用裁剪（每来一个新任务就新建
  一套 QWidget / 布局 / 进度条，再 `deleteLater` 回收最老行）后，真实通讯录
  同步跑了两小时，主线程反复 `UI stall 57.5s / 68.7s`（栈顶 `transfer.py
  add_item`），最终 Qt C 层段错误退出（退出码 139，crash log 为空，
  faulthandler 主线程停在 `app.exec()`）。
  最终改为**行复用**：满 60 行后把最老一行的 item 挪到顶部、复用同一套
  QWidget，只改标题/状态/进度，全程零创建、零 `deleteLater`，不会累积
  待删除控件。新增 `test_full_list_reuses_row_widgets` 断言满列表后
  `_build_row` 调用 0 次、控件数恒定；6060 个任务连续插入/更新的离线压力
  验证：行/控件数稳定在 60 / 300，`clear_items()` 后全部归零。
  **行为变化**：进行中的老任务可能被新任务挤出面板，下载与统计不受影响。
- **登录确认的 10 秒自旋**：改成 `QEventLoop` + 带 parent 的 `QTimer`，等待
  期间不派发用户输入事件；点「取消」立即返回，不再干等满 10 秒；
  `login_signal` 带上轮次，上一轮超时的迟到结果不会串到下一次登录、
  误报「登录成功」。
- **窗口销毁后的迟到回调**：`app.py` 三处无 context 的 `QTimer.singleShot`
  补上 `self`；历史列表「加载更多」的滚动恢复改用与通讯录一致的带 parent
  `QTimer`（v1.6.6 只修了通讯录那处）。
- **WebDAV 自动上传 worker 意外崩溃后进度条永远差一格**：`_loop` 以前直接
  吞掉 `_handle` 之外的异常，现在记日志并补一次「失败」记账；
  `_finish_file` 的信号异常不再冒泡，避免重复计数。
- **画质缓存并发**：`_load_cache` 无锁的双重赋值会让并发首次加载互相覆盖；
  `save()` 临时文件按 pid 命名会被多线程互踩，且并发 `os.replace` 同一目标
  在 Windows 上会 `[WinError 5]`。现加载/写缓存加锁、临时文件加线程号、
  落盘串行化并取最新快照。
- **自动上传状态锁的懒初始化竞态**：首次并发可能各自 new 一把锁，导致
  20 秒节流失效、同一秒把约 10 MB 上传记录写好几遍。改用模块级专用锁
  做双重检查（不复用业务锁，避免重入死锁）。
- **资源句柄**：`load_saved_cookies` 的 `json.load(open(...))` 改成 `with`；
  `build_release._copy_db` 的 `with sqlite3.connect(...)` 其实不关闭连接，
  改用 `contextlib.closing` 并显式 `commit()`（补了数据一致性用例）。

### 性能

- **头像缓存不再整表清空**：通讯录超过 200 人后，以前一满就 `clear()`，
  每次重建列表都要把 40 行头像全部重新解码/裁圆。改成 `OrderedDict` LRU，
  上限仍是 200 个 `QPixmap`，只淘汰最久未用的一个。

### 本轮没有动（附理由）

- **日志窗口每次刷新仍会阻塞约 0.5 秒**：`line_log.load()` 无条件下
  `flush(timeout=1.5)`，而写盘线程攒批窗口是 0.5 秒，正常路径也必然等满；
  5 秒退避只覆盖「写盘卡死」，不覆盖它。这与 v1.6.5 修的「无限等锁」是
  两回事，修它要改日志窗口读取协议，日志链路刚重构过，留待下轮。
- **「加载更多」仍是清空重建**、**每作品一次主线程 SQLite commit**：
  改动面比收益大，保持原样。
- **重定向白名单校验**：属于安全边界变更，需要单独设计与回归，本轮不动。
- README 历史章节里「登录等待自旋」这条已知短板本轮已解决；
  「加载更多」等仍成立。

## [v1.6.6] - 2026-11-13

### 修复：全量同步收尾后的 Qt 崩溃与 GSLB 域名误拒

- **长时间同步收尾崩溃**：v1.6.5 跑完一轮完整通讯录同步（成功 6060 个作品、
  跳过 24657 个）约 8 分钟后，进程在 `QtWidgets.pyd` 触发 `0xc0000005` 退出
  （Windows 事件日志 Application Error 1000）。根因是 `setItemWidget` 挂的
  自定义行不会被 `QListWidget.clear()` / `takeItem()` 回收：上万条任务记录
  会在 viewport 上留下成千上万个孤儿 QWidget，同步结束重建通讯录列表时
  Qt 布局/重绘踩到失效对象。现在传输列表裁剪、`clear_items`、`sync_from`
  以及通讯录列表重建前都先 `removeItemWidget` + `deleteLater`，`_rows` 与
  控件生命周期同步；所有访问行控件前检查 C++ 对象有效性，迟到信号安全跳过。
  通讯录行的滚动恢复改用带 parent 的 `QTimer`，窗口销毁后不再有迟到回调。
- **关窗时可能强杀清理线程**：`_cleanup_uploaded_media_async` 的任务不响应
  停止请求，关窗等 3 秒后会走 `QThread.terminate()`，可能停在 `os.remove`
  中间。现在任务接收 `stop_event` 并在每个循环检查点协作退出；`stage` 信号
  直连 `QLabel.setText`，不再用捕获窗口的 lambda。
- **原片 CDN 轮换域名**：全量同步实测到 `iqhvbghbvehek.jspcdn.cn` 二次跳转
  被「不允许的域名」拒绝。逐个补白名单追不上按区域/时间轮换的 GSLB 节点，
  改为识别 URL 自带的抖音来源标记（路径里的真实主机名或 `http_x-tos-host`
  参数）且必须具备视频直链特征；`jspcdn.cn` 同时进白名单兜底。伪造来源但
  无视频特征的 URL 仍被拒绝。
- **诊断增强**：`main()` 启用 `faulthandler`，C 层崩溃的线程栈单独写到
  `JingYingData/faulthandler.log`。
- **回归**：新增 `tests/test_widget_lifecycle.py` 6 用例（含 400 行连续插入
  的控件泄漏压力测试、失效行跳过、镜像面板回收）；CDN 轮换域名与伪造来源
  用例补进 `test_original_session_thread.py`。

## [v1.6.5] - 2026-11-13

### 修复：第二轮真实同步监控暴露的两个回归

- **一个作品的探测失败拖累整轮**：部分 GSLB 节点的 HEAD / Range 响应不返回
  `Content-Type`（体积却正常，如 33 MB），`cdn_info` 断言 4 会抛
  `OriginalStreamError`；`resolve_for_download` 以前对任何异常都冷却 120 秒，
  于是一个作品的 CDN 头异常就让之后两分钟内所有作品静默回退最高转码档。
  现在 `cdn_info` 只在体积过小或 Content-Type 明确不是视频时失败，缺
  Content-Type 但体积合理按视频处理；并新增 `SessionFailure`，只有浏览器启动、
  登录态、人机校验这类会话级故障才冷却，单作品软失效只回退当前作品。
- **主线程同步写日志**：批量同步每秒数百条「云端已备份跳过」日志，
  `logging.info` 在主线程直接落盘，本机安全钩子下 UI 看门狗抓到 15~25s 卡顿
  （栈顶 `_dl_item_skipped`）。`_setup_logging` 改为 `QueueHandler` +
  `QueueListener`，日志只入内存队列、由后台线程异步写盘，退出前 flush；
  轮转 handler 与格式保持不变。
- **回归**：`tests/test_original_session_thread.py` 新增 CDN 头、软失效不冷却
  用例；`test_original_stream.py` 的会话失败用例改用 `SessionFailure`；
  全量用例通过。

## [v1.6.4] - 2026-11-13

### 修复：批量同步时「原画质」开关实际不生效

- **现象**：开启「优先下载原始上传码流」后跑「全部同步」，日志反复出现
  `原画质回捞会话失败，冷却 120 秒：Cannot switch to a different thread`，
  作品全部静默回退最高转码档。单条下载 / CLI 却正常——Playwright 的 sync API
  把 greenlet 绑定在创建它的线程上，而批量同步有 8 个下载线程复用同一个
  `OriginalSession`，跨线程调用必然失败。
- **修复**：`original_stream` 新增一条专属 daemon 会话线程，会话的创建、探测与
  关闭都在该线程内串行执行；下载线程只提交任务并等结果，异常原样带回并沿用
  原有的 2 分钟失败冷却。CLI 单线程场景仍走原来的直接路径。
- **修复**：原片直链下载时会二次 302 到第三方 GSLB/CDN 节点
  （`*.v1d.pkoplink.com` / `*.bdydns.com` / `*.bdycdn.cn`，参数里仍带
  `http_x-tos-host=*.douyinvod.com`），此前被 `ALLOWED_HOST_SUFFIXES` 拒绝后
  重取直链、最终回退转码档。现按音乐 GSLB 的既有做法放行这三个后缀，无关域名
  仍然拦截。
- **界面**：传输列表声明统一行高（`setUniformItemSizes`）、插入/绑定/裁剪
  合并进一次关闭重绘区间，可见行上限 120→60，压掉长队列同步时
  `setItemWidget` 处 3~4 秒的 `UI stall`。
- **回归**：新增 `tests/test_original_session_thread.py` 7 个用例（8 线程并发回捞
  均落在同一会话线程、close 同线程、异常回传与冷却、端到端选中原片直链、
  CDN 白名单正反例）；全量用例通过。

## [v1.6.3] - 2026-11-12

### 修复：通讯录同步时界面长时间无响应

- **现象**：同步 + WebDAV 自动上传、且开启「上传成功后删除本地」时，界面连续冻结
  十几到几十秒，关窗还会卡在线程池 `join()`。看门狗日志里 `UI stall 13~36s` 的
  栈顶全部是 `_auto_file_done → _delete_after_upload → _delete_uploaded_media →
  os.remove`：每上传成功一个文件就在主线程删一个，某些环境会为每次删除做安全
  校验（起确认子进程 / 走回收站），单个文件就要数秒，几百个文件足以把界面钉死。
- **修复**：上传完成回调只做记账、读开关和入队；真正的 `os.remove` 与空目录清理
  交给独立的 daemon 后台线程串行执行。同一路径去重，单个文件失败不拖垮队列，
  关窗时由 `shutdown_local_cleanup()` 通知收尾。「云端低画质重下」产生的本地副本
  也走同一队列（集合摘除仍留在主线程，避免与写入方并发）。
- **回归**：新增 `tests/test_local_cleanup.py` 6 个用例，覆盖删除不在主线程执行、
  开关关闭不入队、重复路径去重、升级副本集合摘除、单文件异常不中断队列、
  `shutdown` 可重复调用。

## [v1.6.2] - 2026-11-12

### CLI 新增 `compare`：原片 vs 最高转码档编码对比

工具链补充，不改 GUI 与下载链路，按 patch 记录。对应规范验收清单第 4 条。

- `python tools/original_cli.py compare <链接|ID> [--out 目录]`：捕获 detail →
  取 `bit_rate` 里 `data_size` 最大的档作分母 → 本地已有原片按字节数核对后复用
  （不重复下载大文件）、否则下载原片 → 下载清洗过 `playwm` 的最高转码档 →
  两边各跑 `inspect_mp4`，写出 `original_compare.json`（编码 / 分辨率 / 时长 /
  码率 / 体积比 / 码率比 / `codec_changed` / 结论）。
- **真实对比**（作品 `7703135432608072339`，与 v1.6.1 同一验收样本）：

  | | 原片 | 最高转码档 `normal_1080_0` |
  |---|---|---|
  | 字节 | 22,402,202 | 6,015,200（与 detail 声明完全一致） |
  | 容器 / 编码 | `qt` / `hvc1` (HEVC) | `isom` / `avc1` (H.264) |
  | 分辨率 / 时长 | 1080×1920 / 25.982 s | 1080×1920 / 25.982 s |
  | 平均码率 | 6897.8 kbps | 1852.1 kbps |
  | 拍摄时间戳 | `2026-11-01T17:40:34Z` | 无 |

  体积比 = 码率比 = **3.724**，与 `factor` 精确一致；`codec_changed=true`。
  编码从 HEVC 换成 H.264，是「最高转码档经过重编码」的硬证据，反过来印证
  原片拿到的是未经重编码的码流。分辨率和时长两档相同，说明倍率完全来自码率。
- 测试：`tests/test_original_stream.py` 新增 3 个离线用例（编码更换检出、
  已有原片复用不重复下载、同编码不算硬证据），共 50 个。

## [v1.6.1] - 2026-11-12

### 修复：MP4 结构解析读不出真实分辨率

用规范里的验收样本（3.724x 原始码流）真实下载后跑 `check`，发现 `width/height`
恒为 0，虽不影响 `original/transcoded` 判定，但结构校验不完整。定位到
`_scan_stsd` 按 sample entry **box 起始 +40** 读宽高，而 ISO BMFF 规定在
**+32**（box 头 8 + reserved 6 + data_reference_index 2 + pre_defined 16）；
合成 MP4 的测试桩按同样的错误偏移构造，两边自洽，只有真实 HEVC 文件暴露。

- 以规范 +32 为主、+40 为兼容兜底，并加 16~8192 的合理性过滤，避免把随机
  字节当分辨率。
- `stsd` 无有效宽高时退回 `trak/tkhd` 的 16.16 定点宽高；支持 tkhd v0/v1，
  忽略音轨的 0 值。
- 真实文件复核：1080×1920、`hvc1`（HEVC）、25.982 s、6897.8 kbps、`qt` 容器、
  拍摄时间戳 `2026-11-01T17:40:34Z`、`lavf` 重封装痕迹，判定仍为
  `original`（22,402,202 字节 / 最高转码档 6,015,200 字节 = 3.724x）。
- 测试：`tests/test_original_stream.py` 新增 4 个用例（规范偏移的真实布局、
  旧 +40 布局兼容、tkhd 兜底、tkhd v1 + 音轨 0 值），共 47 个；全量 290 个通过。

## [v1.6.0] - 2026-11-12

### 原始上传码流（原画质）回捞

按《抖音「原画质」获取实现规范》(2026-11) 补齐第二条画质链路。此前程序里说的
「原画」是 `bit_rate` 阶梯里分辨率最高的**转码成品**；`playwm→play` 只是换 CDN
路径，编码没有任何变化。本版目标是「视频码流未经重编码」的原始上传码流——
**不是上传时那个文件的字节副本**，容器与元数据仍被平台用 FFmpeg 重封装过。
仅用于本人作品或已获授权内容的归档与画质分析。

- **新增开关**：「优先下载原始上传码流（原画质）」，默认关闭。需要已登录抖音。
- **不实现签名**：`detail` 由真实作品页的 JS 代发（复用 `resolver.js` 的
  `__JY_STATE__.detail`），play 端点在已登录的浏览器上下文内发出，Cookie 天然正确。
- **回捞链路**：`video.play_addr.uri` 作 `video_id` →
  `GET /aweme/v1/play/?ratio=default&improve_bitrate=1&watermark=0&line=0`
  （`max_redirects=0` 只读 `Location`，缺失时页面内 `fetch` 回退并立刻取消响应体）
  → `HEAD` 取 `Content-Length` / `Content-Type` → 按 `play_addr.data_size` **降序**
  取最高转码档做分母 → `factor >= 1.2` 判 `original`，否则 `transcoded`
  并回退最高转码档。规范实测基线：24.2x / 0.43x / 3.72x。
- **CDN 的 HEAD 不一定可用**：实测腾讯 COS 节点对 `HEAD` 返回 404
  `application/xml`，但 `Range: bytes=0-1023` 的 GET 回 206 `video/mp4`。
  现在 HEAD 失败时降级成只读 1 KB 的 Range GET，从 `Content-Range`
  读总长度（不能把 `Content-Length: 1024` 当文件体积）；HEAD 正常但类型不符
  时仍直接判定为降级流，不多发请求。真实样本 40.2 MB / 59.9 MB = 0.67x，
  正确输出 `transcoded` 而不是误判或报错。
- **直链时效性**：单条解析阶段回捞并放入 120 秒缓存；批量同步「先解析全部作者、
  再统一下载」会隔很久，因此下载前由 `OriginalSession`（独立 Chromium profile +
  已保存 Cookie + 首页预热）按 `video_id` 即时重取。直链下载失败会清缓存重取一次，
  仍失败则回退最高转码档。会话级失败（未登录 / 浏览器起不来）冷却 2 分钟，
  批量同步上千作品时不会每个都去重启一次 Chromium。
- **软失效断言**（规范第 7 节，4 条）：`aweme_id` 非空；`desc` 与 `author` 不同时
  为空；`play_addr.uri` 与 `bit_rate` 存在；CDN `Content-Length > 100 KB` 且
  `Content-Type: video/*`。命中人机校验 / 未登录时明确报错；每次请求加 1.2~3.0 s
  随机节流。任何回捞失败只记日志，**不会让作品下载失败**。
- **落盘校验**：`inspect_mp4()` 不依赖 ffprobe，读 `ftyp` 品牌、遍历**所有**
  `trak` 找 `stsd` 编码 tag、宽高、`mvhd` 时长（v0/v1）、平均码率；元数据扫描
  限定在 `moov` 范围内（扫全文件会被 `mdat` 随机字节污染）。`Lavf` 两边都有，
  单独不作为判据。
- **其他改动**：原片下载 1 MB 分块（普通档仍 256 KB）；`_first_url` 不再把
  `play_addr.uri`（video_id）当成可下载直链；`MediaInfo` 增加 `video_id` /
  `best_gear_size` / `original_url` / `original_size` / `original_verdict` /
  `ladder_quality`，结果卡片与下载日志显示「原始码流 xxMB · Nx」。
- **命令行**（`tools/original_cli.py`）：`login-status` / `probe` / `download` /
  `check` / `regress`。`regress` 用规范三个实测样本做离线巡检，失败返回非零
  退出码，可直接挂每日定时任务。
- **测试**：新增 `tests/test_original_stream.py`（43 个用例，含合成 MP4 结构检查、
  三个实测样本、四条断言、`Location` 回退、HEAD 404 → Range GET、直链过期重取），
  GUI 冒烟补 2 个开关用例。真实链路用项目自检样本做过只读探测（不下载内容）。

## [v1.5.0] - 2026-11-12

### 云端低画质自动重下

- **新增开关**：WebDAV 卡片「云端已有低画质时自动重下（上传后删除本地副本）」，
  默认关闭。开启后，同步遇到「本地已无媒体、云端有备份」的作品时，先读取云端文件
  的**实际分辨率**，只有确实低于本次解析结果才重新下载。
- **只读文件头，不整包下载**：MP4 走 WebDAV `Range`，只取 `moov/tkhd`（实测一次
  256 KB 块即可，约 0.4~0.8 秒）；`moov` 在文件末尾的原片会自动回退扫尾部。旧版
  tar 作品包只能整包取，解析完立即删除临时文件。
- **先按字节数粗筛**：上传记录里存着文件大小，云端文件 ≥ 本次解析体积的 85% 就
  判为同画质，连远端请求都不发。这样「本地已删、云端本来就是同一档」的绝大多数
  老作品不会因为开这个开关而让同步变慢。
- **比较规则**：竖屏/横屏统一按短边比较（1080×1920 与 1920×1080 同档），留 5%
  容差挡掉 1078/1080 这类编码抖动；任一侧读不出分辨率一律不重下，避免解析失败
  把整个库重下一遍。图集不参与——签名模板图改参数只会 403，没有更高像素可拿。
- **重下 → 覆盖上传 → 删本地**：重下的作品沿用原上传链路（`PUT` 覆盖同名文件，
  文件指纹变化后不会被「跳过已上传」拦下），确认上传成功才删除这份本地副本；
  该删除只针对本轮重下的文件，与「上传成功后删除本地媒体」全局开关互不影响，
  也不会在音乐/图片还没传完时提前删本地。
- **前置条件**：需要同时开启「通讯录同步时自动上传新文件」（否则重下后无法覆盖
  云端）；未开启时开关会在状态栏提示。
- **结果缓存**：`data/remote_quality.json` 按「相对路径 + 文件大小」记录，7 天内
  不重复请求；关窗时强制落盘，下次同步直接命中。缓存过期只代表「重新核对一次」：
  文件大小没变就直接复用已有分辨率，绝不会因为缓存到期而把同一作品重下一遍。
- 新增 `media_probe.py`（MP4/tar 分辨率解析，纯标准库、不依赖 ffmpeg）、
  `remote_quality.py`（远端探测 + 缓存）、`WebDAVClient.stat/fetch_range/download_to`；
  `DownloadManager` 增加 `precheck` 与 `item_skipped` 信号，预检在下载线程里执行，
  不占用主线程。
- 新增 `tests/test_remote_quality.py`（共 24 个用例）：`moov` 在头/尾、Range 只取
  小块、tar 包内视频、短边比较与容差、未知分辨率不重下、缓存命中、缓存过期后
  同尺寸复用并刷新时间戳、临时文件清理、字节数粗筛、图集跳过、开关关闭不探测、
  预检跳过不发 `item_done`/不计失败。

### 收尾修复（同版本，提交前回归）

- **探测失败不再把整个库重下一遍**：远端分辨率读不出来（`Range` 不被支持、
  编码不认识、tar 超过上限）、WebDAV 不可用、或按作品名定位不到云端主文件时，
  一律保留已有云端备份并跳过下载；此前会返回「继续下载」，开一次开关就可能
  把上千个已备份作品全部重下。同步收尾汇总新增「云端已是高画质跳过 N 个」。
- **tar 备份能被定位**：云端路径索引原来只按「作品目录名」查，开启过「打包成
  tar」的老库备份是 `<作品id>.tar`，会一个都匹配不上。现在索引与「是否已备份」
  判定统一用同一组目录（净影/Videos/Music）与同一组名字（目录名/作品 id/
  tar 名）。同一作品有多份云端副本时，粗筛看完所有候选、只要有一份够清楚就跳过。
- **换 WebDAV 服务器不再串用旧缓存**：画质缓存的 key 加上「服务器 + 远程目录」，
  保存新配置时释放旧探测连接池并强制落盘；作品单元索引的缓存 key 也带上服务器，
  避免换服务器后新 dict 恰好复用旧内存地址而返回旧索引。
- **预检不再与上传线程抢同一个字典**：画质预检从下载线程遍历上传记录，自动上传
  线程会同时往这个 dict 写新记录，原来的直接迭代可能抛
  `dictionary changed size during iteration`，被当成预检失败而误重下。现在两处
  索引都先取键快照再遍历。
- **`HEAD` 没给 `Content-Length` 时回退 `PROPFIND`**：部分网关的 HEAD 只有状态码，
  旧逻辑会把文件大小当成 0 直接放弃探测。服务器忽略 `Range` 返回整包时改为流式
  读取到所需偏移就断开，不再先把几十 MB 视频整个读进内存。
- **跳过计数按轮次重置**：`_sync_all` 原先没有清零 `_dl_skip`，连续两轮同步的
  跳过数会累加；同步中的状态栏现在显示真实的跳过原因，不再固定写「云端已是高画质」。
- 测试补齐：GUI 冒烟新增开关默认值/提示/持久化/自动上传联动/换配置失效 4 个用例，
  `tests/test_webdav.py` 新增 5 个只读接口用例，`test_sync_all_flow` 增加跳过数
  清零断言，`tests/test_remote_quality.py` 再补 8 个边界与汇总用例。

## [v1.4.0] - 2026-11-12

### 原画质保障（对齐 HANDOFF 文档）

- **短链也能拿到原画**：短链（`v.douyin.com/...`）会跳到 H5 分享页，那页不请求
  `/aweme/detail/`，探针抓不到元数据，只能退回播放地址兜底（多为 720p）。现在解析出
  作品 ID 后会回到桌面作品页再取详情，走带完整 `bit_rate` 阶梯的接口。
- **不再静默降级**：只拿到播放地址时，画质标为「兜底地址（未取到画质阶梯）」，
  同时写一条 `app.log` 警告（`detail missing, fallback to player url`），
  结果卡片与下载日志都能看出来，不会再误以为下到了原画。
- **下载日志记录画质**：「下载完成」那行后面会带上「画质 原画 2160p · H.265」，
  可以直接核对每条作品实际下到的档位；不传画质时日志格式与旧版完全一致。
- 确认阶梯择优本身无需改动：实测同一作者 1321 个作品里选出 53 条 2160p、142 条
  1440p；抽一条 2160p 下载落地校验，实际文件轨道为 **2160×3840 + 音轨**，与选中档一致。
- 图集受网页接口限制：`url_list` 是签名绑定的 `~tplv-dy-aweme-images:q75.webp`，
  改模板参数会 403；`download_url_list` 同尺寸但带水印。所以图集保持取无水印的
  `url_list`，暂无法拿到更高像素（这是接口下发上限，不是选择逻辑问题）。
- 新增 `tests/test_quality_guard.py`（9 个用例）：阶梯择优、码率优先于缺失的
  `data_size`、`playwm→play` 去水印、同分辨率优先完整 mp4、H.265 识别、兜底标注、
  下载日志画质。

### 移除

- 删除内置播放器：`player.py`、`player_entry.py` 及对应 3 个测试/脚本文件全部移除，
  `MainWindow` 不再混入 `PlayerMixin`，`closeEvent` 里关播放器的那段也去掉了。
  下载产物用系统播放器打开；`archive.py` 保留（历史列表还要读旧 tar 的文案与清单）。
  备份在 `.backup_opt/before_20261112_remove_player/`。

## [v1.3.0] - 2026-11-12

### 变更

- **移除「观看已下载」内置播放器入口**：主界面右上角不再有这个按钮，下载产物就是
  作品文件夹（或勾选开关后的 tar 包），直接在资源管理器里用系统播放器打开。
- 「下载后打包成 tar」开关的说明随之上调：明确写「tar 不能直接播放、要先解包」，
  提示文案不再提内置播放器；开关本身、上传 tar 的行为与 v1.2.0 一致。
- 播放器代码（`player.py` / `player_entry.py`）暂时保留，后续要恢复入口只需加回按钮。

### 修复（播放器内部，入口暂不暴露）

- **图集切换后仍显示上一张**：切作品/切图前没有先清掉上一位的 pixmap，新图解码
  （1080×1920 原图 + 平滑缩放）的几十到上百毫秒里，屏幕上还是上一张；另外窗口刚
  创建时 `stack` 尺寸还没算出来，第一张会被兜底成 100×66 的小图。现在切图先清屏、
  尺寸依次回退到 label / 窗口、大图先快速折半再平滑收尾，窗口显示后按真实布局重绘。
- **实况图换源残留**：`setSource()` 是异步的，换源后立刻翻页会停在前一张的最后一帧；
  改为先显示静态图占位，等 `LoadedMedia` / `BufferedMedia`（900ms 兜底）再切实况页。
- **长按没有倍速**：此前从未调用 `setPlaybackRate`，长按只会被当成「按住后松手」
  触发暂停。现在按住 450ms 进入 2×（松手恢复），纯图集或后端不支持时给出明确提示。
- 新增 `tests/test_player_live_speed.py`（12 个用例）覆盖上述路径。

## [v1.2.0] - 2026-11-12

### 新增：下载后打包成 tar（可选开关）

- 主界面「WebDAV 备份」卡片新增开关「下载完成后打包成 tar（WebDAV 也上传 tar）」，
  默认关闭，保持原来的作品文件夹结构；开关旁直接提示「可能会影响播放性能」。
- 勾选后每个作品下载完由下载线程打包成 `<作者目录>/<作品id>.tar` 并删除散文件；
  先写 `.part` 再改名，打包失败会保留原文件夹并记日志，不影响已下载内容。
- WebDAV 自动上传只入队这个 tar（不再按已删除的散文件路径上传）；「上传全部」
  扫到 tar 也按 tar 上传，上传记录 / 跳过已上传 / 上传后清理都按 tar 单元处理。
- 历史记录与播放器的 `video_path` / `album_paths` 指向 tar，播放时由
  `archive.extract()` 按需解包；首次打开会稍慢，这就是开关旁提示的代价。
- 通讯录同步的「已下载」判断同时认 `净影/作者/<作品id>.tar`，避免开启开关后
  每轮同步把已有作品当成没下过再下一遍。
- 新增 `tests/test_tar_option.py`（9 个用例）覆盖打包、失败降级、上传队列、
  已下载判定；GUI 冒烟补充开关默认值 / 提示文案 / 持久化用例。

## [v1.1.0] - 2026-11-12

### 版本与发版体系（本次新增）

- `config.APP_VERSION` 升到 `1.1.0`，成为唯一版本真源；窗口标题改由
  `config.window_title()` 统一生成，不再在别处手写版本号。
- 启动日志新增 `version=1.1.0 build=<短 sha>` 一行，反馈问题时能定位到具体构建。
- `JingYingPC.spec` 接入 `file_version_info.txt`，exe 右键属性的
  `FileVersion` / `ProductVersion` 不再为空；文件内容每次由发版脚本按版本重新生成。
- 新增 `build_release.py`：测试 → 构建 → 组装带登录态便携目录 → 按版本命名 zip
  → CRC 校验 → 同步 CHANGELOG，一条命令完成发版，杜绝手写产物名。
- 发行包内附 `build_sha.txt` / `VERSION.txt`，窗口标题带构建短 sha，
  用于区分同一版本号的不同构建；源码运行（含测试）标题保持稳定。
- 便携包组装时只复制登录态与用户数据，剔除 Chromium 纯缓存目录；
  `history.db` 用 SQLite backup API 生成一致快照，不再复制 `-wal` / `-shm` 半写状态。
- README 的下载说明改为指向版本真源，新增「版本与发版」章节；本文件随版本维护。
- GitHub 私有仓库打 `v1.1.0` tag 并发布 Release 挂载对应 zip。

### 稳定性与性能（此前两轮改造，首次纳入版本号）

- 下载 / 解析线程改为 `stop_event` 协作式退出：`Worker.request_stop()` 置位
  `threading.Event` 并调用 Qt `requestInterruption()`，解析轮询、滚动等待、浏览器
  锁等待、下载 chunk 循环都能及时中断；关窗时先通知、再等待，仅对无响应线程兜底
  `terminate()`，避免数据损坏、临时文件残留和 Playwright 孤儿浏览器。
- 日志后台队列改为限时等待 / 退避，写盘失败降级到 `app.log`，修复界面冻结。
- 修复 64 位文件句柄截断、解包缓存半目录被误认完整、作者去重 O(n²)、
  WebDAV Session 未回收、上传状态频繁落盘等问题。
- 补齐 `tests/test_optimizations.py`、`tests/test_stop_event.py`，全量 188 用例通过。

### 已知问题（本版未改动，待确认产品决策）

- `contacts_sync._auto_sync` 目前没有调用点，打开程序不会自动同步，需手动点「全部同步」。
- 点「停止同步」时未对解析线程调用 `request_stop()`，线程会继续抓取一小段时间。
- `_sync_all()` 开头清空失败作者名单，中途停止会丢失该名单。

## [v1.0.0] - 2026-11-12

- 初始版本：GitHub 私有仓库 `liuhaodh/jingying-pc` 首次提交（62 个源码文件）。
- 此前所有迭代都沿用 1.0.0，仅靠 README 日期章节和文件名区分，没有 tag / Release。
