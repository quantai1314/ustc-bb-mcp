# USTC BB MCP

让支持 MCP 的 AI 客户端读取中国科学技术大学 Blackboard 的课程、作业、公告和课件，并通过“先预览、再确认”的流程提交标准文件作业。服务运行在本机，通过 stdio 通信，每位用户使用自己的学校账号。

本项目参考 [changshenhan/bb-mcp](https://github.com/changshenhan/bb-mcp) 的工具设计，独立适配中科大统一身份认证及 Blackboard 页面。项目与学校 Blackboard 官方无隶属关系。

## 安装

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。以下以 Windows 和项目目录 `D:\Project\ustc-bb-mcp` 为例；macOS、Linux 使用相应的项目绝对路径。uv 可以自动安装所需 Python。

先克隆仓库，再安装锁定依赖：

```powershell
git clone https://github.com/quantai1314/ustc-bb-mcp.git
cd ustc-bb-mcp
uv sync --frozen
uv run playwright install chromium
uv run ustc-bb-mcp doctor
```

Linux 如果提示 Chromium 缺少系统库，可以执行 `uv run playwright install --with-deps chromium`。这一步可能需要安装系统依赖的权限。

`doctor` 检查本机配置与运行环境，不访问学校网站，不显示密码或 Cookie。首次安装需要联网下载 Python 依赖和 Chromium；日常使用仍需要能访问学校 BB 和统一身份认证网站。

## 配置与登录

优先使用系统凭据库保存密码：

```powershell
uv run ustc-bb-mcp configure
```

按提示输入统一身份认证账号和密码。密码输入不可见，保存到当前用户的系统凭据库；`config.json` 只保存账号。配置完成后，工具在需要时自动登录，之后复用本地会话；会话失效时自动尝试重新登录一次。

如果学校要求验证码、二次认证，或系统凭据库不可用，执行：

```powershell
uv run ustc-bb-mcp login --browser
```

在打开的独立浏览器中完成学校认证，等待命令确认登录成功。这里使用项目专用的浏览器配置，不读取日常 Chrome、Edge 的会话。只使用浏览器登录也可以运行查询和提交工具；会话失效后需要再次执行此命令。

系统凭据库不可用时不会自动把密码写入明文文件。也可通过运行服务进程的环境变量提供账号密码，适合已有安全配置环境的用户：

| 环境变量 | 用途 |
|---|---|
| `USTC_CAS_USR` | 统一身份认证账号，覆盖本地账号配置 |
| `USTC_CAS_PWD` | 本次进程使用的密码，优先于系统凭据库 |
| `BB_OUTPUT_DIR` | 课件与附件的下载根目录 |
| `BB_DATA_DIR` | 配置、会话和项目浏览器配置的保存目录 |
| `BB_BASE_URL` | 可选 BB 入口，仅接受学校 `www.bb.ustc.edu.cn` 或 `bb.ustc.edu.cn` 的 HTTPS 地址 |

环境变量中的密码不会由本项目写入配置文件。不要把密码加入仓库、共享的 MCP 配置示例或聊天消息。

## 接入 MCP 客户端

将 [examples/mcp.json](examples/mcp.json) 中的 `mcpServers` 条目加入客户端配置。按实际位置替换项目路径；如果客户端找不到 `uv`，将 `command` 换成 `uv` 可执行文件的绝对路径。

```json
{
  "mcpServers": {
    "ustc-bb": {
      "command": "uv",
      "args": [
        "--directory", "D:\\Project\\ustc-bb-mcp",
        "run", "--frozen", "ustc-bb-mcp", "serve"
      ]
    }
  }
}
```

客户端需要以完成配置的同一系统用户运行。如果配置时指定了 `BB_DATA_DIR`，启动 MCP 的环境也要设置相同值。保存配置后，按客户端要求重启或重新连接服务。

服务的 stdout 仅用于 MCP 协议，日志写入 stderr。直接运行 `uv run ustc-bb-mcp serve` 后等待标准输入属于正常行为；交互式诊断请使用 `doctor`。

## 9 个工具

| 工具 | 作用与参数 |
|---|---|
| `bb_login()` | 自动登录并保存 BB 会话；需要人工补验时返回操作提示 |
| `bb_status()` | 检查会话，过期时尝试重新登录一次 |
| `bb_list_courses()` | 列出账号可见课程及其 ID、学期信息和原始链接 |
| `bb_list_course_files(course)` | 遍历课程全部可发现的内容区和嵌套目录，列出文件 |
| `bb_download_courseware(course, keyword="", latest=2)` | 按文件名关键词筛选，下载最新的指定数量课件 |
| `bb_list_assignments(course)` | 查询作业、测试入口、截止日期及可确认的状态；`course="all"` 查询全部课程 |
| `bb_assignment_details(course, keyword="", download=True)` | 查询作业完整要求、附件、状态及可见成绩；默认会下载附件，设 `download=False` 可只查看 |
| `bb_announcements(course, limit=8)` | 读取公告；`course="all"` 查询全部课程 |
| `bb_submit_assignment(course, assignment, file_path, comment="", confirm=False, preview_token="")` | 为单个标准文件作业生成提交预览，再凭预览令牌确认提交 |

课程与作业支持准确 ID、完整名称或唯一名称关键词。命中多项时会返回候选，不会自动选择第一项；后续调用请使用准确 ID。

可以向客户端提出这些请求：

- “列出我的课程，检查所有课程的作业提交状态，并附上原始链接。”
- “查看数据结构最近的作业要求，先不要下载附件。”
- “下载电磁学课程最新的两份 PDF 课件。”
- “读取所有课程最近的公告，整理与考试有关的信息。”

公告总结由使用工具的 AI 客户端完成。本项目不提供定时提醒、跨课程全文索引、教师批改或自动答题。

### 如何理解查询结果

每次结果包含可读说明和结构化数据，附查询时间。`complete=false` 表示有页面读取失败或扫描被截断；`warnings` 说明不完整的原因。部分失败不会被当成“没有作业”或“已交齐”。

作业状态区分 `not_submitted`（未提交）、`draft`（草稿）、`submitted`（已提交）、`graded`（已评分）和 `unknown`（无法确认）。草稿不算已提交；零分仍属于已评分。缺失的日期返回空值，不根据文件名推测截止时间。

文件有明确日期时按新到旧排序，无日期文件排在后面。下载返回本机实际路径，按课程组织；重名文件不会直接覆盖，同大小但内容不同的文件不会被误判为重复。网页、附件或未知结构与预期不符时，工具会返回错误或不完整提示。

### 提交作业

提交工具只处理 Blackboard 标准文件作业，上传一个本地文件并可附评语。测试、考试、自动答题及教师端操作不在此工具支持范围内。

1. 使用 `confirm=False` 调用，检查返回的账号会话、课程、作业、已有提交、文件路径、大小、SHA-256、评语和平台允许的提交条件。已交且允许重交时，`starts_new_attempt=true` 表示确认后会创建新尝试。预览阶段不创建尝试、不上传文件，也不点击开始测试或提交按钮。
2. 用户确认具体目标和文件后，使用相同参数、`confirm=True` 及返回的 `preview_token` 调用。令牌 10 分钟内有效，并绑定账号会话、目标、文件内容、评语和提交条件；文件或目标改变时需要重新预览。重交会先创建新尝试，再核对新表单后上传。
3. 工具提交后有限次数只读回查提交历史，并重新下载提交附件核对 SHA-256；只有目标、尝试记录及附件内容核验通过才报告成功。若表单预先分配了尝试 ID，回读必须对应同一次尝试，旧尝试不能证明重交成功。回查不会重放 `newAttempt` 或提交动作。平台另换尝试 ID、附件无法回读等无法证明的情况返回“结果待确认”，应查看 BB 网页，不要立即重复提交。

开发和演示阶段不会自行向真实课程提交。需要实际提交验证时，应由用户明确指定作业和文件，并通过上述预览确认流程。

## 本地数据

未设置 `BB_DATA_DIR` 时，使用 `platformdirs` 返回的当前用户数据目录：Windows 通常在 `%LOCALAPPDATA%\ustc-bb-mcp`，macOS 在 `~/Library/Application Support/ustc-bb-mcp`，Linux 在 `~/.local/share/ustc-bb-mcp`。

其中 `config.json` 保存账号配置，`session.json` 保存登录会话，`browser-profile/` 保存人工登录使用的独立浏览器配置。密码存于系统凭据库或仅由环境变量提供。下载默认进入用户下载目录下的 `USTC-BB`，可用 `BB_OUTPUT_DIR` 更改。会话与下载内容留在本机，工具读取到的课程信息会返回给调用它的 MCP 客户端。

## 常见问题

| 现象 | 处理方式 |
|---|---|
| 客户端提示找不到 `uv` | 使用 `uv` 可执行文件的绝对路径，并检查项目目录 |
| 提示 Chromium 未安装 | 在项目目录运行 `uv run playwright install chromium` |
| Linux 提示浏览器缺少共享库 | 运行 `uv run playwright install --with-deps chromium` |
| 凭据库不可用或没有可用 backend | 使用 `login --browser`，或由安全的进程环境提供账号密码 |
| 登录失败、需要验证码或二次认证 | 在浏览器确认账号能正常登录，再执行 `login --browser`；密码变更后重新 `configure` |
| 工具反复提示未登录 | 确认客户端和配置命令使用同一用户及同一 `BB_DATA_DIR`，并检查校园网、代理和认证入口是否可达 |
| 课程或作业关键词命中多项 | 从候选列表复制准确 ID 后重试 |
| 查询显示 `unknown` 或 `complete=false` | 根据返回的失败信息检查原始页面；不要据此认定作业已提交或不存在 |
| 提交预览令牌过期或内容不匹配 | 重新生成预览并确认，不能复用旧令牌 |
| 提交后结果待确认 | 打开 BB 提交历史核验回执与附件，不自动重复提交 |

## 开发与验证

本机最终检查：Python 3.11.15，157 项测试全部通过，Ruff 通过。初次适配时已完成 wheel 和源码包构建；本次修复验证的是源码开发环境。

```powershell
uv sync --frozen
uv run playwright install chromium
uv run ruff check .
uv run pytest -q
uv build
```

自动化测试使用虚构页面、模拟响应和本地测试站，不需要学校账号，也不会访问真实课程或提交作业。测试覆盖页面解析、状态判定、附件下载、认证边界、MCP 协议以及提交预览与回执处理。CI 配置了 Windows、macOS、Linux 与 Python 3.11、3.13 的验证矩阵；远端执行结果以实际工作流为准。

真实 BB 验证状态：已完成项目独立浏览器登录，并通过真实 MCP 验证会话、课程列表、两门课程样本的文件/作业/公告、PDF下载与去重、作业详情及提交预览。后续在用户明确授权下，通过真实 stdio MCP 完成一次标准文件作业重交，回读新尝试及附件 SHA-256 与本地文件一致。初次只读验收的 83 次 GET、0 次写入统计不包含后续实际重交。真实密码自动登录分支尚未验证。详见 [验证记录](VALIDATION.md)。

## 授权与来源

本项目采用 [MIT License](LICENSE)。工具设计参考和第三方依赖说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
