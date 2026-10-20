# 日记 OSS 自动备份

启动入口仍是 `app.py`。辅助模块由它自动加载，不需要另开备份程序或配置系统定时任务。

本文中的 `user-data/`、`auth/`、`.backup-runtime/` 和配置文件都位于外部数据目录，默认版本目录内的 `data/`。无备份配置的新安装默认禁用自动备份。

## 首次配置

1. 安装依赖：`python -m pip install -r requirements.txt`。OSS 接入使用官方推荐的 **Python SDK V2**，单独安装命令为 `python -m pip install "alibabacloud-oss-v2>=1.4.0,<2"`，当前已用 1.4.0 验证。程序不再依赖旧的 `oss2`；机器上已有的 `oss2` 可以保留供其他项目使用。
2. 将 `config-examples/oss_backup_config.example.json` 复制为数据目录中的 `oss_backup_config.json`（默认 `data/oss_backup_config.json`）。示例默认 `enabled: false`，不会上传、下载或删除云对象。填写自己的 Bucket、地域及其公网 HTTPS Endpoint，并为此实例选择独立前缀；确认后将 `enabled` 改为 `true`。同一个前缀只供这一套日记服务使用。
3. 在**实际运行 app.py 的服务器和服务账户**设置环境变量 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`。支持可选 `OSS_SESSION_TOKEN`。密钥不写入代码、不提交 Git、不通过网页返回，也不要发到聊天中。Windows 可在系统“环境变量”设置中添加，再重新打开终端或重启服务，使进程继承变量。
4. 先运行 `python app.py --backup-now`，退出码 0 表示上传及校验成功；它使用同一个入口，执行后退出。进入 OSS 控制台确认 `shenzhen-bay-diary/snapshots/` 下出现 ZIP；随后运行 `python app.py` 常驻提供日记服务。

没有配置密钥时备份会失败，旧版本不删除，日记读写仍可使用。修改配置后重启服务；修复凭证后可通过同一入口的 `--backup-now` 验证。

RAM 用户需要这个专用前缀下的读、写、删权限，以及 Bucket 的列举权限。上传包含私有 ACL 和 AES256 服务端加密；建议保持整个 Bucket 为私有。参考最小前缀权限示例（如前缀/Bucket 改动，同步修改）：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["oss:ListObjects"],
      "Resource": ["acs:oss:*:*:your-backup-bucket"],
      "Condition": {"StringLike": {"oss:Prefix": ["shenzhen-bay-diary/*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["oss:GetObject", "oss:PutObject", "oss:DeleteObject", "oss:PutObjectAcl"],
      "Resource": ["acs:oss:*:*:your-backup-bucket/shenzhen-bay-diary/*"]
    }
  ]
}
```

不要为 `snapshots/` 设置“7 天自动过期”的 OSS 生命周期规则，否则上传失败时 OSS 仍会自行删除历史。应用使用独立文件名管理版本；不需要开启 Bucket 版本控制。若已开启，普通删除可能仅添加删除标记、仍保留收费的历史版本，本程序不清理这些历史 Object Version。

## 时间、版本及界面规则

- 每天北京时间 **04:00** 触发一次，后台检查间隔 15 秒，不依赖服务器操作系统时区。4 点后启动且当天未尝试时补执行一次；4 点前启动等待到 4 点。当天失败不自动反复重试，次日继续；手动 `--backup-now` 可主动重试。
- 服务必须常驻且机器不能关机/休眠。直接 `python app.py` 启动即开始调度；WSGI 部署会在 worker 的首次 HTTP 请求后启动调度。同一台机器同一目录的多 worker 通过操作系统文件锁和持久状态防止重复任务；不同机器不可共用该备份前缀。
- 保留最近 **7×24 小时内的全部成功版本**，同时至少保留**最新 7 个成功版本**；原来不足 7 个时全部保留，不补造历史版本。清理从最早版本开始：只有当前成功版本数**大于 7**，并且该版本年龄**严格超过 7×24 小时**，才删除这一份，再重新判断下一份；任何一个条件不满足就停止。同一天无论新增多少份，都不会因为数量超过 7 而删除这一天的版本。
- 年龄按 OSS 正式对象的 `Last-Modified`（发布时间）与当前时间的实际间隔判断，使用带时区的时间，不依赖文件名日期或本机时区。恰好满 7×24 小时仍保留。只有本次上传的正式对象最终校验成功，且所有将保留版本可读取、哈希正确后，才清理符合条件的旧版本；上传、元数据读取或校验失败都不清理，清理异常宁可暂留更多版本，待下次成功后再判断。
- 流程：本地独立快照 → 验证 SQLite / ZIP → 上传到 `_uploading/` → 下载 SHA-256 校验 → OSS 复制成正式 ZIP → 校验正式对象 → 持久化成功时间 → 按数量和年龄计算将保留版本并逐份验证 → 才删除符合两个条件的旧版本。网络上传、下载及清理均在后台，不持有业务写入锁。
- 正式快照在 `shenzhen-bay-diary/snapshots/`。`_uploading/` 是临时对象，正常会自动清理；进程被强杀或网络中断可能留下临时对象，既不计入版本数，也不作为成功记录。
- 导出 Excel 按钮后的小字：距上次成功 **不超过 30 小时**为绿色“备份成功”；超过 30 小时为红色“备份失败”，仍显示上次成功时间。尚无成功记录显示红色“备份失败 / 上次成功：暂无”。一次失败不会抹掉此前成功时间；30 小时内仍按上述阈值显示绿色。页面打开时、每分钟及切回页面时刷新。
- `.backup-runtime/` 保存调度状态和成功时间，位于用户数据之外，重启会保留；删除此目录会丢失本地成功时间和当天去重记录。请保持它持久化，不要随每次部署清空。

## 备份范围与安全

只扫描 `user-data/`：SQLite 日记库、资产记录、属性、用户映射、JSON 设置、历史文本与原始 Excel 文件。忽略 Excel 导出副本（文件名以“日志导出”或“日记导出”开头）、隐藏临时文件、SQLite WAL/SHM/journal 和无关文件类型；不包含音乐、程序、API 密钥或备份自身。遇到符号链接/目录联接、空数据目录、损坏数据库、变化中的普通文件或无效 JSON，本次失败并保留旧版本。

数据库通过 SQLite 在线备份 API 从只读连接生成副本，包含 WAL 中已提交事务，不直接复制正在写入的 `.db`，不强制 checkpoint、不关闭业务数据库。按小批量页复制，持续繁忙超过 120 秒则放弃本次；SQLite 正常并发协调可能带来短暂 I/O 或锁等待，不能承诺绝对零延迟。JSON / 文本写入使用同目录临时文件、落盘后原子替换，避免备份读取半写入内容。各数据库和各文本文件各自完整；不同用户及文件之间不承诺同一微秒的全局事务快照。

“在途任务”和“待提升属性”的当前内容统一存于每用户 `diary.db` 的 `user_state` 表，与每日 `logs` 一起完整备份；恢复同一数据库即可恢复两栏。旧属性 TXT 在首次迁移后仍保留，恢复新版时以数据库状态为准。备份调度与成功版本保留规则无需为这两栏另行调整。

**账号密码哈希在 `auth/users.json`，不在 `user-data/`。自动备份仅覆盖用户数据，因此全新服务器恢复登录仍需原来的 `auth/users.json`。应单独妥善保管该文件。**

## 恢复步骤

1. 从 OSS 下载所需正式 ZIP，在独立空目录解压；ZIP 内有 `user-data/` 和 `manifest.json`（逐文件 SHA-256）。先按清单校验文件哈希，并对 SQLite 副本执行 `PRAGMA integrity_check`，结果必须为 `ok`。
2. 停止日记服务，另存当前 `user-data/` 作为回退，然后整体换成解压得到的 `user-data/`。不要在运行中覆盖数据库，也不要把旧 WAL/SHM 文件混入恢复目录。
3. 保留/恢复相应的 `auth/users.json`，重启 `python app.py`，抽查日记、资产记录及用户归属。

## 整个项目目录迁移

本文数据相对路径以 DATA_ROOT 为根，默认是版本内的 data，可由 DIARY_DATA_ROOT 指定。迁移服务器时保留 `日记本/identity` 以及包含完整 data 的版本目录。普通升级先停止日记、完整备份 data，更换版本后把 data 放回，保留其中 user-data、auth/users.json、oss_backup_config.json 和 .backup-runtime。旧实例不要继续运行另一套相同备份前缀的服务。

迁移到另一台机器时，需要安装依赖并在新机器的运行账户重新配置 OSS 环境变量（以及已有的其他服务环境变量）。环境变量不会随文件夹一起复制。仅在同一台机器内移动目录、且运行账户不变时，通常可以沿用原环境变量。

SDK V2 使用 V4 签名，启用上传及下载 CRC64 校验，并对可重试的网络请求最多尝试 3 次（属于同一次每日备份任务，不会改变每日调度规则）。每次连接和网络读写超时设置为 30 秒；SHA-256 回读校验及成功后才清理旧版本的规则继续保留。之前配置的环境变量、Bucket、目录前缀和持久化成功时间可以沿用。

实现依据：[Python SQLite 在线备份 API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)、[阿里云 OSS Python SDK V2 上传](https://www.alibabacloud.com/help/en/oss/developer-reference/simple-upload-using-oss-sdk-for-python-v2)、[官方 SDK 迁移说明](https://www.alibabacloud.com/help/en/oss/developer-reference/python-sdk-v1-to-v2-migration-guide)。
