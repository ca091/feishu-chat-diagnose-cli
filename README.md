# Feishu Chat Diagnose CLI

手动选择飞书群聊或私聊，拉取限定时间范围内的消息与多模态附件，再让 Codex 在明确指定的本地仓库中进行只读问题诊断。

## 安装

```bash
# 在项目目录执行一次安装，使用 uv.lock 中已验证的依赖版本。
diagnose_constraints="$(mktemp)"
uv export --locked --no-dev --no-emit-project --no-hashes --output-file "$diagnose_constraints"
uv tool install --python 3.12 --constraints "$diagnose_constraints" .
# 若安装时提示命令目录不在 PATH 中，执行后重新打开终端。
uv tool update-shell

# 首次升级时在旧项目目录执行，以自动迁移配置；之后可在任意目录使用。
feishu-diagnose init
```

在 `~/.feishu-diagnose/config.toml` 中配置允许诊断的仓库绝对路径、默认诊断 Skill、可覆盖的 Skill 白名单与视频模型。Skill 必须位于目标仓库的 `.agents/skills/<name>/SKILL.md`：

```toml
[repositories.demo-project]
path = "/absolute/path/to/demo-project"
description = "Demo project"
diagnosis_skill = "project-bugfix"
allowed_skills = ["project-bugfix", "performance-optimization"]
```

仓库只能通过别名选择，聊天内容不能改变仓库、Skill 或权限。`diagnosis_skill` 可以留空；设置后将作为该仓库的默认诊断流程。

### 全局配置与初始化

唯一配置模板位于 [`src/feishu_chat_diagnose/config.example.toml`](src/feishu_chat_diagnose/config.example.toml)，随安装包分发；通过 `feishu-diagnose init` 生成实际配置。

```text
~/.feishu-diagnose/
├── config.toml
└── cases/
    └── <case-id>/
```

`init` 生成带注释的配置模板和案例目录，不调用外部服务；重复执行会保留已有全局配置。如果当前目录存在旧的 `config/config.toml` 且全局配置尚不存在，会自动迁移配置：保留注释及模型、Skill 等设置，将相对仓库和可执行文件路径按当前项目目录转换为绝对路径，并将 `case_dir` 改为 `cases`。原配置保留，旧案例不迁移。旧配置无效时会报错，不会用模板替代。

因此，从旧版本升级时，应先在旧项目目录运行一次 `feishu-diagnose init`，之后即可在任意目录使用。若之前已设置 `FEISHU_DIAGNOSE_CONFIG`，迁移后请取消该变量或指向新配置，否则普通命令仍会使用旧配置。

配置优先级为：各子命令的 `--config` → `FEISHU_DIAGNOSE_CONFIG` → `~/.feishu-diagnose/config.toml`。普通命令不再自动读取当前目录的配置；`init` 始终初始化全局位置。缺少配置时会提示初始化。

```bash
feishu-diagnose list --config /absolute/path/to/other-config.toml
```

配置文件路径支持 `~`，相对路径按调用目录解析；配置内的仓库和案例相对路径按配置文件所在目录解析。未设置 `case_dir` 时始终使用全局案例目录。普通可执行命令名通过 `PATH` 查找，可执行文件路径按配置目录解析。显式使用旧配置时，也适用新的相对路径规则；请先将旧相对路径改为绝对路径。

`uv tool install` 默认不读取项目的 `uv.lock`；上述导出约束的步骤让安装与测试使用相同的依赖版本，避免重新解析出需要额外编译环境的新版本。临时约束文件无需保留到项目中。

源码或依赖更新后，重新导出约束并执行安装命令，添加 `--reinstall`。开发时在安装命令中增加 `--editable`，源码修改直接生效，依赖或入口变更仍需重新安装。仓库内开发与测试继续使用 `uv sync --python 3.12 --extra dev` 和 `uv run pytest`，也可运行 `uv run feishu-diagnose ...`。

飞书访问使用当前用户身份，需要先完成 `lark-cli` 登录，并授权以下只读 scope：

```text
im:message:readonly im:chat:read
im:message.group_msg:get_as_user im:message.p2p_msg:get_as_user
contact:user.base:readonly
```

视频理解需要在环境中设置 `GEMINI_API_KEY`，不要将值写入配置或提交到 Git。

## 使用

```bash
export GEMINI_API_KEY="你的 Gemini API Key"
GEMINI_API_KEY="你的 Key" \
feishu-diagnose doctor
feishu-diagnose analyze --group "示例群" --repo demo-project --since 2h
feishu-diagnose analyze --group "示例群" --repo demo-project --skill project-bugfix --since 2h
feishu-diagnose analyze --group "研发示例群" --repo demo-project --since 2h
feishu-diagnose analyze --person "张三" --repo demo-project --start "2026-07-15 16:00" --end "2026-07-15 17:00"
feishu-diagnose analyze --person "张三" --repo demo-project --skill project-bugfix --since 2h
feishu-diagnose list
feishu-diagnose show <case-id>
feishu-diagnose resume <case-id>
```

`analyze` 会先显示消息、图片、视频和文件数量，默认不继续；附件下载不完整时还会进行第二次确认。模型输出不符合 Schema、Codex 失败或视频工具失败时都不会自动重试，只有显式执行 `resume` 才会再次启动诊断。

没有传 `--skill` 时使用仓库的 `diagnosis_skill`；传入时只能选择 `allowed_skills` 中的 Skill。CLI 会先校验本地文件，再通过 app-server `skills/list` 确认 Codex 已发现并启用它，最后以原生 Skill 输入显式激活。实际使用的 Skill 会写入 `diagnosis.json`、`report.md` 和执行日志。

确认后，终端会持续输出会话、附件、Skill 选择、Codex 初始化、工具步骤和报告保存进度。Codex 暂无新事件时每 10 秒输出一次等待心跳。相同事件会写入 case 的 `execution.log`：

```bash
tail -f ~/.feishu-diagnose/cases/<case-id>/execution.log
feishu-diagnose show <case-id>
```

执行日志不包含聊天正文、附件内容、附件 key、临时文件路径、密钥、原始命令参数或模型完整响应。

## 完整流程

```mermaid
flowchart TD
    START(["手动执行 feishu-diagnose analyze"])

    subgraph INPUT["输入与上下文准备"]
        CONFIG["读取配置、时间范围、Skill 和群聊/私聊参数"]
        REPO{"仓库别名在白名单中且路径有效？"}
        SKILL{"默认或覆盖 Skill 在白名单中且文件有效？"}
        CONFIG_FAIL(["配置错误：退出，尚未创建运行资源"])
        RESOLVE["按群名或私聊对象搜索会话"]
        CHOOSE{"存在多个候选？"}
        SELECT["用户选择唯一会话"]
        FETCH["分页拉取消息"]
        EXPAND["补全引用父消息和完整话题"]
        ASSEMBLE["去重、排序并按关键词组装上下文"]
        CHECKPOINT["持久化 request、conversation 和 messages checkpoint"]
        PREVIEW["预览消息、图片、视频、文件和缺失上下文数量"]
        CONFIRM{"用户确认只读诊断？"}
        CANCEL["标记 case 为 cancelled，不调用模型"]
    end

    subgraph ATTACHMENTS["临时附件准备"]
        MATERIALIZE["下载附件到本次运行的临时目录"]
        IMAGE["图片：作为 localImage 交给 Codex"]
        TEXT["文本：按字节限制解码后加入上下文"]
        VIDEO["视频：仅向 Codex 提供受限临时路径"]
        PARTIAL{"存在附件下载失败？"}
        PARTIAL_CONFIRM{"用户确认使用不完整附件继续？"}
    end

    subgraph DIAGNOSIS["单次 Codex 只读诊断"]
        APP["启动 CLI 专属 codex app-server --stdio"]
        DISCOVER["调用 skills/list，确认 Codex 发现并启用指定 Skill"]
        SANDBOX["创建 read-only thread，禁止审批、写文件和外部副作用"]
        MCP["注册必需的 understand_video MCP 工具"]
        TURN["以原生 Skill 输入启动一个 turn：聊天、附件、视频映射和仓库"]
        SEARCH["Codex 搜索代码、Git 历史并整理证据"]
        NEED_VIDEO{"相关证据包含视频？"}
        VIDEO_TOOL["Codex 调用 understand_video"]
        UPLOAD["上传原始视频并等待 provider 激活"]
        UNDERSTAND["返回摘要和带时间点的观察"]
        DELETE_REMOTE["finally 删除 provider 侧临时视频"]
        FINAL["Codex 生成 phase=final_answer 的 Diagnosis JSON"]
        VALID{"JSON 通过严格 Schema 校验？"}
    end

    subgraph RESULT["结果、失败与恢复"]
        SAVE["持久化 diagnosis.json 和确定性 report.md"]
        COMPLETE["标记 case 为 completed"]
        FAIL["保存 failure.json 并标记失败阶段"]
        NO_RETRY["不自动重试"]
        RESUME{"用户显式执行 resume case-id？"}
        RESTORE["读取 checkpoint，仅从失败阶段及后续阶段继续"]
        REENTER["重新进入记录的失败阶段"]
    end

    subgraph CLEANUP["所有终态的资源清理"]
        TERMINATE["finally：若已启动，则终止 CLI 专属 codex app-server"]
        WAIT{"5 秒内退出？"}
        KILL["否：kill 后再次等待"]
        MCP_CLOSE["app-server 退出时关闭视频 MCP 子进程"]
        LOCAL_DELETE["删除本地临时图片、文本和视频"]
        REMOTE_DELETE["删除仍存在的 provider 侧临时视频"]
        PRESERVE["仅保留 case checkpoint、execution.log、报告或失败信息"]
        END(["本次运行结束"])
    end

    START --> CONFIG --> REPO
    REPO -- "否" --> CONFIG_FAIL
    REPO -- "是" --> SKILL
    SKILL -- "否" --> CONFIG_FAIL
    SKILL -- "是或未配置 Skill" --> RESOLVE --> CHOOSE
    CHOOSE -- "是" --> SELECT --> FETCH
    CHOOSE -- "否，唯一候选" --> FETCH
    FETCH --> EXPAND --> ASSEMBLE --> CHECKPOINT --> PREVIEW --> CONFIRM
    CONFIRM -- "否" --> CANCEL --> TERMINATE
    CONFIRM -- "是" --> MATERIALIZE

    MATERIALIZE --> IMAGE
    MATERIALIZE --> TEXT
    MATERIALIZE --> VIDEO
    IMAGE --> PARTIAL
    TEXT --> PARTIAL
    VIDEO --> PARTIAL
    PARTIAL -- "是" --> PARTIAL_CONFIRM
    PARTIAL_CONFIRM -- "否" --> CANCEL
    PARTIAL_CONFIRM -- "是" --> APP
    PARTIAL -- "否" --> APP

    APP --> DISCOVER --> SANDBOX --> MCP --> TURN --> SEARCH --> NEED_VIDEO
    NEED_VIDEO -- "是" --> VIDEO_TOOL --> UPLOAD --> UNDERSTAND --> DELETE_REMOTE --> SEARCH
    NEED_VIDEO -- "否或视频分析完成" --> FINAL --> VALID
    VALID -- "是" --> SAVE --> COMPLETE --> TERMINATE
    VALID -- "否" --> FAIL
    FETCH -. "API、分页或上下文失败" .-> FAIL
    MATERIALIZE -. "不可继续的附件失败" .-> FAIL
    APP -. "初始化、MCP、模型或超时失败" .-> FAIL
    FAIL --> NO_RETRY --> RESUME
    RESUME -- "是" --> RESTORE --> REENTER
    REENTER -. "会话定位失败" .-> RESOLVE
    REENTER -. "消息拉取失败" .-> FETCH
    REENTER -. "上下文组装失败" .-> ASSEMBLE
    REENTER -. "附件准备失败" .-> MATERIALIZE
    REENTER -. "Codex 诊断失败" .-> APP
    RESUME -- "否" --> TERMINATE

    TERMINATE --> WAIT
    WAIT -- "是" --> MCP_CLOSE
    WAIT -- "否" --> KILL --> MCP_CLOSE
    MCP_CLOSE --> REMOTE_DELETE --> LOCAL_DELETE --> PRESERVE --> END

    FORCE(["kill -9、断电或系统崩溃：可能绕过 finally，无法保证清理"])
    START -. "进程被强制中止" .-> FORCE
```

## 数据与边界

case 默认保存在 `~/.feishu-diagnose/cases/<case-id>/`，其中可能包含私聊正文，请勿提交或公开分享。图片、视频和普通附件只存在于运行期临时目录，结束后删除。Gemini 仅作为 Codex 可调用的原始视频理解工具，最终问题归因和仓库分析只由同一个 Codex turn 完成。

首版不提供机器人、消息监听、飞书回复、代码修改、提交或推送，也没有自动重试。

## 手工冒烟测试

准备一个测试群，发送一段问题文本、一张截图和一个 MP4；将对应测试仓库加入配置。先运行 `doctor`，再用较短的 `--since` 执行 `analyze`。确认报告包含聊天事实、视频时间点和仓库代码证据，并检查 case 目录中没有残留图片或视频。
