# 深圳湾日记

一个可以独立部署的个人日记与认知工作区。日记本提供注册登录、日历、每日记录、跨日任务、资产记录和 Excel 导出；认知工作区提供工作区对话、个人认知整理和模型调用记录。

日记本是服务组入口，负责认知工作区的登录接入、启动与代理。认知工作区保留独立代码、服务身份和数据目录，也可以单独运行。本仓库不依赖外层主站。

```text
.
├─ 日记本/
│  ├─ identity/                    公开服务身份及子服务相对路径
│  └─ current/
│     ├─ app.py                    日记本服务入口
│     ├─ assets/welcome-audio/      内置欢迎提示音
│     ├─ config-examples/           无真实凭证的部署配置示例
│     ├─ tests/                    离线回归用例
│     └─ data/                     本机生成，Git 忽略
├─ 认知工作区/
│  ├─ identity/
│  └─ cognitive_workspace/
│     ├─ server.py                 工作区服务入口
│     ├─ backend/
│     └─ data/                     本机生成，Git 忽略
└─ tools/init_demo.py              创建一名虚构用户及空数据
```

## 快速开始

使用 Python 3.10 或更高版本，在仓库根目录执行：

```sh
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS / Linux：source .venv/bin/activate
python -m pip install -r 日记本/current/requirements.txt
python 日记本/current/app.py --host 127.0.0.1 --port 8081
```

打开 <http://127.0.0.1:8081/>，注册自己的账号。默认不配置模型密钥、不启用 OSS 备份。点击顶部黄色“认知工作区”入口时，日记本会核验并启动本机工作区服务；工作区左上角蓝色图标可以返回日记本。默认工作区端口为 8766，仅监听本机。

两份内置欢迎音频随源码保留，首次使用时复制到日记本 `data/music-data/`；新注册用户默认等权使用这两份音频，可在个人设置中关闭声音。

## 空白示例账号

首次下载、尚未启动服务时，可以在仓库根目录执行：

```sh
python tools/init_demo.py
python 日记本/current/app.py --host 127.0.0.1 --port 8081
```

脚本仅在空数据目录创建 **1 名虚构用户**：用户名 `demo_guest`，密码 `ExampleOnly-42!`。日记、资产、任务和认知对话均为空，不含任何现有使用者的信息。遇到非空目录会停止，不覆盖账号或数据。

本次整理的本机副本已经初始化这一示例账号。生成的 `data/` 被 Git 忽略，因此从 GitHub 新克隆的仓库需执行初始化脚本，或直接注册新账号。示例密码是公开的；对外部署前请修改，或从空数据目录自行注册。

## 模型与部署配置

认知工作区从进程环境变量读取 `DEEPSEEK_API_KEY`。配置方法及其他环境变量见[工作区说明](认知工作区/cognitive_workspace/README.md)和[空配置示例](认知工作区/cognitive_workspace/.env.example)。程序不会自动加载 `.env`，需在实际服务进程的环境中设置。

OSS 配置示例位于 `日记本/current/config-examples/`。按[备份说明](日记本/current/OSS备份说明.md)填写自己的 Bucket、地域、Endpoint 和独立前缀，密钥通过环境变量提供。自动备份默认关闭。

服务身份 UUID 是公开的软件标识。登录签名秘密和子服务凭证在本机首次运行时生成，不随源码提供。数据及服务配置使用相对路径，也支持环境变量覆盖，参见[日记本部署说明](日记本/README.md)。

## 数据与备份

两套业务数据的现有目录和备份边界保持独立：

| 位置 | 内容与备份方式 |
| --- | --- |
| `日记本/current/data/user-data/` | 日记 SQLite、资产、设置及用户映射；属于现有日记 OSS 快照范围 |
| `日记本/current/data/auth/` | 账号与密码哈希；需单独妥善备份 |
| `日记本/current/data/music-data/` | 欢迎音频；不在日记 OSS 快照范围内 |
| `日记本/current/data/.backup-runtime/` | 备份调度状态，迁移时保留 |
| `认知工作区/cognitive_workspace/data/` | 工作区数据库、对话、调用记录及自身备份；独立保管，不属于日记 OSS 快照 |

日记 OSS 的 ZIP 格式、备份范围、校验流程与清理策略保持原有规则：保留最近 7×24 小时内的全部成功版本，并至少保留最新 7 个成功版本；本次上传及保留版本校验成功后，才删除同时超过这两个界限的旧版本。

“在途任务”和“待提升属性”是跨日当前状态。“计划与摘要”属于每日记录：未保存日期显示更早最近一个已保存日期的值，保存后成为当天的独立内容。详见[日记本说明](日记本/current/README.md)。

升级已有部署时使用其最新完整数据，不把示例数据覆盖到正式数据。流程见[部署与迁移说明](新版部署与数据迁移说明.md)。

## 发布与验证

源码不包含真实账号、密码哈希、日记、认知对话、画像、调用日志、历史版本、云端备份、模型密钥或部署凭证。`.gitignore` 排除两套运行数据、缓存、归档、环境配置、日志和密钥文件；发布时保留这些规则。

日记离线测试：

```sh
cd 日记本/current
python -m unittest discover -s tests -v
node --test tests/session_guard.test.cjs tests/tasks_form.test.cjs
```

程序源码采用 [MIT License](LICENSE)。依赖包遵循各自许可证。内置音频作为单独素材保留，其内容不纳入源码 MIT 授权范围。
