# 运行与部署

本仓库直接在根目录实现 README 描述的 `performance-v2`。Web 工作台沿用同级 `LLM-API-service` 的 Model Studio 白灰主题。服务端口固定为 **8001**，只启动 **1 个 Uvicorn worker**，避免同一工作台启动多个互不知情的评测任务。

## Docker 部署

需要宿主机可访问 Docker，以及构建时可下载 Python 依赖和 `o200k_base` 编码表。

```bash
cd /home/pillar/llm-performance
export DASHSCOPE_API_KEY='你的 API Key'
export DASHSCOPE_BASE_URL='https://ws-ebzun9rcecl8l3rh.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1'
bash deploy/start.sh
```

脚本在标准 `python:3.12-slim` 容器中按照 `requirements.lock` 准备 `.runtime`，然后启动 `llm-performance-worker-1`，轮询 `http://127.0.0.1:8001/healthz`，通过后输出访问地址。远程访问使用 `http://服务器地址:8001`。重复执行会先平滑停止旧 worker，再以原有结果目录重建。容器使用 `unless-stopped` 自动重启策略，运行一个 Uvicorn worker；源码挂载为只读，`results/` 和 `state/` 可写。

工作台无需登录口令，直接访问页面即可使用。模型密钥从环境变量传入，不写入源码、浏览器或评测结果。

```bash
docker logs --tail=100 llm-performance-worker-1  # 运行日志
docker ps --filter name=llm-performance-worker-1 # 容器状态
docker stop --time 30 llm-performance-worker-1   # 停止并保存已开始请求的状态
```

日常查看日志、停止服务不需要重新提供密钥。再次部署时需要 `DASHSCOPE_API_KEY` 环境变量。模型 Key 只通过 Docker 环境变量传递，没有保存到源文件中。

容器配置保存到宿主机 `state/config.local.yaml`，结果保存到 `results/`；重建容器保留这两个目录。网页「保存配置」只保存非密钥配置。修改 YAML 的任务文件路径时，应相对于项目根目录，并限定在 `prompts/` 中。新增模型可在 YAML 中填写独立 `base_url` 和 `api_key_env`，对应密钥还需通过 `docker run -e 变量名` 传入。

另保留 `Dockerfile` 和 `compose.yaml` 供镜像构建部署；使用该方案前先停止单独启动的 worker，避免争用 8001。执行 `docker compose up -d --build`。两种部署方式使用相同的持久化目录。

默认模型声明 `base_url_env: DASHSCOPE_BASE_URL`：环境变量存在时覆盖 YAML 的 `base_url`。如果需要为某模型使用另一个地址，删除该模型的 `base_url_env`，或指向另一个环境变量。

## 模型目录与重点配置

网页「模型目录」读取当前端点 `/models` 的全部分页结果，支持搜索、归属方筛选、GLM-5.3 / GLM-5.2 / Qwen3.8 筛选、下载完整 JSON 和加入实验。列表缓存 5 分钟，可手动刷新；上游失败时明确显示旧缓存。2026-10-14 实测该端点返回 **99 个模型**，列表可见不代表每个模型都支持文本 Chat Completions。

默认配置为 `config.focus.example.yaml`，启用 `glm-5.3`、`glm-5.2`、`qwen3.8-max`，保留 `qwen3.8-flash` 可选。`glm5.2` 在端点中的实际 ID 是 `glm-5.2`。旧保存配置已备份至 `state/config.before-model-catalog.yaml`；网页保存的配置优先于示例文件。

GLM-5.3 和 GLM-5.2 按用户提供的档位配置 `low`（低）、`high`（高）、`max`（最大 / 默认），请求发送 `thinking: {type: enabled}` 和对应 `reasoning_effort`。旧配置备份在 `state/config.before-glm-effort.yaml`。Qwen3.8-Max 按用户提供的 `low`（低）、`medium`（中）、`xhigh`（超高 / 默认）三档配置，请求发送 `enable_thinking: true` 和对应 `reasoning_effort`；旧配置备份在 `state/config.before-qwen-effort.yaml`。

从目录新加入的其他模型默认不发送推断的思考参数，`thinking_enabled: null` 表示思考状态未知。加入时默认不启用，需要勾选后才参与评测。目录查询及计划检查不验证上游对推理参数的执行效果。

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

已在 Python 3.12 容器中运行完整测试：35 项全部通过，无跳过。浏览器检查覆盖免登录访问、完整模型目录、搜索与导出、矩阵与模型选择、计划检查、YAML 编辑、桌面/移动布局和页签；没有页面脚本异常。部署后，本机和公网 8001 的健康检查均返回 200。默认三个模型均出现在服务端模型列表中。真实 `qwen3.8-flash / off` 短回答任务已成功执行：512 公共输入 Token，首答案约 0.55 秒，完成约 1.03 秒。该单次样本仅用于部署联调，不代表稳定性能结论。初次长回答因输出上限截断的历史记录保留在同一 run 中。

`deploy/verify.py` 检查健康与免登录配置，不发起模型请求；`deploy/smoke.py` 通过工作台发起一个 `qwen3.8-flash / off` 的 512 输入 Token 请求，验证结果、续跑跳过和离线报告。该脚本使用独立的 `deployment-smoke/v1` 运行，不覆盖默认配置；重复运行会复用已有成功记录。
