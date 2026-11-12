# 更新日志

版本号以 `config.py` 的 `APP_VERSION` 为唯一真源；发版由 `build_release.py` 负责，
窗口标题、exe 版本资源、便携目录 / zip 名、CHANGELOG 段落和 git tag 全部由它派生。
版本规则遵循语义化版本：修复 bug 升 patch，功能/行为变更升 minor，破坏性变更升 major。

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
