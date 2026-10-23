# 运行与部署

## OSS 存储配置

线上私有桶：`llm-performance-eval`，地域 `cn-beijing`，前缀 `results/`。仅复用 paragraph-player 的 OSS 账号，新桶与原应用数据隔离。凭证配置存放在 `state/oss.env`（Git 忽略，权限 `0600`）：

```dotenv
RESULTS_BACKEND=oss
OSS_BUCKET=llm-performance-eval
OSS_REGION=cn-beijing
OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
OSS_PREFIX=results
OSS_ACCESS_KEY_ID=填写服务端凭证
OSS_ACCESS_KEY_SECRET=填写服务端凭证
```

可按需增加 `OSS_SESSION_TOKEN`。部署脚本、worker、Compose 和 systemd 模板均加载此配置。未配置 `RESULTS_BACKEND` 的开发环境默认使用本地存储；未知后端或 OSS 配置错误直接报错。需要的运行权限为新桶的 ListObjects、GetObject、PutObject；`deploy/provision_oss.py` 另外需要 CreateBucket 和 DeleteObject 以创建私有桶并清理读写探针。

结果列表和下载从 OSS 获取，增量运行先下载最新指针和请求记录。执行过程中每 5 秒尝试上传日志和汇总检查点；结束时上传 JSON、JSONL 和 HTML，再提交最终任务元数据，成功后清理该运行的本地文件。失败时保留 `results/.pending/` 与工作文件，页面显示上传状态；服务每 15 秒重试，也会在启动时重试。不要手工清理待上传目录。OSS 故障不会回退到本地结果。运行中的进度仍来自单进程内存，重启从 OSS 记录和未上传工作文件恢复。多个服务器/worker 同时写同一结果前缀不受支持。

CLI 与独立 `uvicorn` 进程需要显式加载相同环境变量，例如启动前执行 `set -a; source state/oss.env; set +a`。CLI 结束时上传报告并清理工作文件；`report.py` 在 OSS 模式从云端读取结果，生成本地导出文件并回存云端报告。CLI 上传失败可通过启动服务重试同一 `RESULTS_ROOT` 的待上传队列。

验证命令（使用真实 OSS、模拟模型调用，不消耗模型请求；自动清理专用验证前缀）：

```bash
docker run --rm --env-file "$PWD/state/oss.env" \
  -v "$PWD:/workspace:ro" -w /workspace \
  -e TIKTOKEN_CACHE_DIR=/workspace/.token-cache -e PYTHONDONTWRITEBYTECODE=1 \
  python:3.12-slim .runtime/bin/python deploy/verify_oss.py
```

本次验证覆盖上传/读取、清理本地后下载、全新本地目录启动后恢复任务，以及增量跳过已有成功项。之前的本地测试运行和任务记录已清空。

本仓库直接在根目录实现 README 描述的 `performance-v2`。Web 工作台沿用 Model Studio 简洁布局，保留白灰色调，使用卡片底色区分区域，并保留 1px 浅灰外框。服务端口固定为 **8001**，只启动 **1 个 Uvicorn worker**，由同一任务管理器调度多个独立后台评测，并协调同一运行目录的写锁。

## Docker 部署

需要宿主机可访问 Docker，以及构建时可下载 Python 依赖和 `o200k_base` 编码表。

```bash
cd /home/pillar/llm-performance
export DASHSCOPE_API_KEY='你的 API Key'
export DASHSCOPE_BASE_URL='https://ws-ebzun9rcecl8l3rh.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1'
bash deploy/start.sh
```

脚本在标准 `python:3.12-slim` 容器中按照 `requirements.lock` 准备 `.runtime`，然后启动 `llm-performance-worker-1`，轮询 `http://127.0.0.1:8001/healthz`，通过后输出访问地址。远程访问使用 `http://服务器地址:8001`。重复执行会先平滑停止旧 worker，再以原有结果目录重建。容器使用 `unless-stopped` 自动重启策略，运行一个 Uvicorn worker；源码挂载为只读，`results/` 和 `state/` 可写。

工作台无需登录口令，直接访问页面即可使用。默认 SG 密钥从环境变量传入，不下发浏览器。网页也支持填写自定义 Key 和 Base URL；自定义 Key 自动保存在当前浏览器 localStorage，按来源与 API 地址绑定，刷新后恢复，不写入服务器共享配置或评测结果。不同浏览器互不共享；同一浏览器配置共用时仍可读取，可用「清除本机 Key」移除。

```bash
docker logs --tail=100 llm-performance-worker-1  # 运行日志
docker ps --filter name=llm-performance-worker-1 # 容器状态
docker stop --time 30 llm-performance-worker-1   # 停止并保存已开始请求的状态
```

日常查看日志、停止服务不需要重新提供密钥。再次部署时需要 `DASHSCOPE_API_KEY` 环境变量。模型 Key 只通过 Docker 环境变量传递，没有保存到源文件中。

容器配置保存到宿主机 `state/config.local.yaml`，结果保存到 `results/`；重建容器保留这两个目录。网页的连接源与条件自动保存到浏览器 localStorage，默认不过期，首次使用才从服务器配置初始化；模型选择只在本次页面中保留，刷新后清空。`PUT /api/config` 继续支持程序保存非密钥共享配置。修改 YAML 的任务文件路径时，应相对于项目根目录，并限定在 `prompts/` 中。网页直接从当前连接的接口列表选择模型，也可填写 Model Name；每个模型始终绑定其来源，同名模型可以从不同来源同时评测。命令行仍可在 YAML 中填写独立 `base_url` 和 `api_key_env`，对应密钥需通过环境变量传入。网页已移除 YAML 编辑器。

另保留 `Dockerfile` 和 `compose.yaml` 供镜像构建部署；使用该方案前先停止单独启动的 worker，避免争用 8001。执行 `docker compose up -d --build`。两种部署方式使用相同的持久化目录。

默认模型声明 `base_url_env: DASHSCOPE_BASE_URL`：环境变量存在时覆盖 YAML 的 `base_url`。如果需要为某模型使用另一个地址，删除该模型的 `base_url_env`，或指向另一个环境变量。

## 后台任务与恢复

网页默认新建独立运行，提交后可继续配置并启动其他任务。`GET /api/jobs` 返回各任务进度，`POST /api/jobs/{job_id}/stop` 单独停止任务；旧 `/api/status`、`/api/stop` 只对应最近启动的一项。进度按完成的评测项计数，重试次数另列。

线上任务状态保存在 OSS 的 `results/.jobs/`，本地暂存待上传元数据，不保存自定义 Key。刷新或关闭页面不影响运行；服务关闭时取消执行中的任务并等待结果上传，重启从 OSS 恢复任务记录。异常退出后的未完成任务标记为中断，可用增量模式补跑。部署前检查 `/api/jobs` 没有 `starting` / `running` 或 `storage_pending` 任务，避免中断用户评测。

`max_retries` 取 0–5，网页默认 2，CLI 未配置时保持 0。429、408、5xx、超时及网络中断按 1、2、4、8、16 秒退避重试，鉴权/参数错误不重试。每次尝试独立保存，成功请求的性能指标不包含退避等待。

## 模型目录与重点配置

网页「实验配置 → 评测对象」按连接源读取当前端点 `/models` 的全部分页结果，支持搜索、多选、手动添加 Model Name 和刷新。独立的模型目录 Tab 已移除。列表缓存 5 分钟，可手动刷新；上游失败时明确显示旧缓存。2026-10-14 实测该端点返回 **99 个模型**，列表可见不代表每个模型都支持文本 Chat Completions。

命令行重点示例 `config.focus.example.yaml` 启用 `glm-5.3`、`glm-5.2`、`qwen3.8-max`，保留 `qwen3.8-flash` 可选。`glm5.2` 在端点中的实际 ID 是 `glm-5.2`。旧保存配置已备份至 `state/config.before-model-catalog.yaml`；新版网页每次刷新都清空模型选择，连接源与评测条件仍会恢复；旧示例仍供 CLI 使用。

GLM-5.3 和 GLM-5.2 按用户提供的档位配置 `low`（低）、`high`（高）、`max`（最大 / 默认），请求发送 `thinking: {type: enabled}` 和对应 `reasoning_effort`。旧配置备份在 `state/config.before-glm-effort.yaml`。Qwen3.8-Max 按用户提供的 `low`（低）、`medium`（中）、`xhigh`（超高 / 默认）三档配置，请求发送 `enable_thinking: true` 和对应 `reasoning_effort`；旧配置备份在 `state/config.before-qwen-effort.yaml`。

从目录新加入的其他模型默认不发送推断的思考参数，`thinking_enabled: null` 表示思考状态未知。从目录勾选或手动添加后即参与评测；同名模型按来源独立加入。目录查询及计划检查不验证上游对推理参数的执行效果。

## 本地 Python / systemd

```bash
uv sync --locked
export DASHSCOPE_API_KEY='你的 API Key'
uv run uvicorn server:app --host 0.0.0.0 --port 8001 --workers 1
```

已生成 `uv.lock`，以及从锁文件导出的 `requirements.lock`。Python 本地环境使用 `uv sync --locked`；Docker 构建和 worker 准备脚本安装 `requirements.lock` 中固定的版本。更新依赖时先运行 `uv lock`，再运行 `uv export --locked --no-dev --no-hashes --no-emit-project --format requirements-txt --output-file requirements.lock`。

如果使用 systemd，将环境变量以 `NAME=value`（不加 `export`）保存在项目 `.env`，权限设为 `0600`。环境文件需要 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`。按实际运行账号审查并安装 [服务模板](deploy/llm-performance.service)，随后启用服务。模板使用此仓库路径及 `.venv/bin/uvicorn`。

## CLI 评测与报告

```bash
# 默认只检查计划，不请求模型
uv run python benchmark.py --config config.example.yaml

# 全量执行；会调用配置中的真实模型
uv run python benchmark.py --config config.example.yaml --execute --mode full

# 增量续跑：保留历史失败，跳过已有成功项
uv run python benchmark.py --config config.example.yaml --execute --mode incremental

# 读取最新运行生成单文件 HTML
uv run python report.py --config config.example.yaml

# 指定历史运行
uv run python report.py --results results/实验名称/v1/runs/运行ID/results.json
```

网页启动的评测结束或停止后自动生成报告；CLI 通过独立 `report.py` 生成。报告只嵌入汇总数据，不带逐请求答案正文，离线无需网络。JSON/JSONL 保留答案片段与完整尝试历史。

原始示例的思考参数来自需求快照；目录已经在线获取，参数支持情况仍需通过实际请求验证。首次建议通过 YAML 仅启用一个模型、一个 profile、512 输入 Token、并发 1、重复 1、预热 0，然后再扩大矩阵。工具不会自行为模型修正或推断思考参数。

## 验证

```bash
uv run python -m unittest discover -s tests -v
```

测试覆盖配置与矩阵身份、参数覆盖、增量计划、统计口径、离线报告转义，以及通过真实 SDK + `httpx.MockTransport` 的模拟 SSE、并发、取消、失败追加、Token 精确长度和 ASGI 控制接口。测试不调用线上模型；缺少 SDK、tiktoken 或 Web 依赖时，对应测试会明确标为跳过。

2026-10-23 后台多任务版本：Python 3.12 容器中 62 项测试全部通过（无跳过），包含独立任务并发/停止/重启恢复、重试预算/取消退避、进度与统计口径。浏览器额外验证自动保存来源与 Key、旧凭证缓存恢复来源、刷新任务恢复、进度条分母、桌面/手机布局。浏览器验证覆盖多来源同名模型、独立参数与凭证、保存恢复、单击启动检查、重复提交防护、迟到目录响应、来源删除、CSV / 离线报告与桌面及手机布局。正式 8001 服务已重启并验证静态资源、双来源计划接口及历史报告；原有配置和结果文件校验一致。本次验证使用模拟生成请求，没有调用真实模型。

以下为早期单来源版本的历史部署记录：35 项测试全部通过，无跳过。浏览器检查覆盖免登录访问、完整模型目录、搜索与导出、矩阵与模型选择、计划检查、YAML 编辑、桌面/移动布局和页签；没有页面脚本异常。部署后，本机和公网 8001 的健康检查均返回 200。默认三个模型均出现在服务端模型列表中。真实 `qwen3.8-flash / off` 短回答任务已成功执行：512 公共输入 Token，首答案约 0.55 秒，完成约 1.03 秒。该单次样本仅用于部署联调，不代表稳定性能结论。初次长回答因输出上限截断的历史记录保留在同一 run 中。

`deploy/verify.py` 检查健康与免登录配置，不发起模型请求；`deploy/smoke.py` 通过工作台发起一个 `qwen3.8-flash / off` 的 512 输入 Token 请求，验证结果、续跑跳过和离线报告。该脚本使用独立的 `deployment-smoke/v1` 运行，不覆盖默认配置；重复运行会复用已有成功记录。
