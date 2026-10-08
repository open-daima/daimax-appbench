# API 参考

`evalapp` CLI 命令、配置 Schema 及扩展点的完整参考文档。

## CLI 命令

### 命令总览

| 命令 | 说明 |
|------|------|
| `evalapp evaluate` | 评测产物或工作区代码（构建 → 安装 → E2E → 评分 → 报告） |
| `evalapp retest` | 重跑 E2E 测试并重新生成报告 |
| `evalapp report` | 为已完成的评测生成 / 重新生成报告 |
| `evalapp export` | 只读导出 v1 本地交换 JSON，不重新评分 |
| `evalapp history` | 查看工作区的执行历史记录 |
| `evalapp migrate-workspace` | 迁移旧版工作区目录结构 |

### `evalapp evaluate`

主评测命令，支持三种模式：

#### 产物模式（推荐）

直接评测预构建产物：

```bash
# Web 应用（URL）
evalapp evaluate --url <url> --sample-ids <id> [OPTIONS]

# Android APK
evalapp evaluate --apk <path> --sample-ids <id> [OPTIONS]

# iOS 应用
evalapp evaluate --app <path> --sample-ids <id> [OPTIONS]

# 源码项目（需配合 --platform）
evalapp evaluate --project <dir> --platform <platform> --sample-ids <id> [OPTIONS]
```

#### 批量模式

从样本集目录批量评测：

```bash
evalapp evaluate --workspace <path> --samples-dir <dir> --platform <platform> [OPTIONS]
```

#### 执行计划模式

精细控制评测顺序与范围：

```bash
evalapp evaluate --workspace <path> --exec-plan <file> [OPTIONS]
```

#### 参数说明

| 参数 | 说明 |
|------|------|
| `--url` | 已部署的 Web 应用 URL（产物模式） |
| `--apk` | 预构建 Android APK 路径（产物模式） |
| `--app` | 预构建 iOS .app 路径（产物模式） |
| `--project` | 源码项目目录（产物模式，需配合 `--platform`） |
| `--output` | 结果输出目录（产物模式默认 `./eval_output`） |
| `--workspace` | 已有工程目录路径（产物模式可省略） |
| `--samples-dir` | 样本集目录（批量模式必填，与 `--exec-plan` 二选一） |
| `--exec-plan` | 执行计划 YAML 路径（与 `--samples-dir` 二选一） |
| `--platform` | 目标平台：`web` / `android` / `ios` |
| `--sample-ids` | 样本 ID 列表，逗号分隔（默认执行计划中的全部样本） |
| `--sample-id` | 单个样本 ID（已弃用，请使用 `--sample-ids`） |
| `--generator` | 生成器名称（默认从工作区目录名推断） |
| `--workers` | 并发评测线程数（默认 1） |
| `--show-browser` | 显示浏览器界面（有头模式），默认无头 |
| `--no-uninstall` | 评测完成后不卸载应用 |
| `--no-open-report` | 禁止自动打开浏览器查看报告 |
| `--wait-generate` | 流水线模式：按样本等待生成完成后立即投入评测 |

#### 平台自动推断

| 产物参数 | 推断平台 |
|----------|----------|
| `--url` | `web` |
| `--apk` | `android` |
| `--app` | `ios` |
| `--project` | 需显式指定 `--platform` |

### `evalapp retest`

为已评测的样本重跑 E2E 测试，无需重新构建或安装。

#### 单样本模式

```bash
evalapp retest --workspace <path> --sample-id <id> --platform <platform> [OPTIONS]
```

#### 多样本模式

```bash
evalapp retest --workspace <path> --sample-ids <id1>,<id2> --exec-plan <file> [OPTIONS]
```

#### 参数说明

| 参数 | 说明 |
|------|------|
| `--workspace` | 工作区路径（必填） |
| `--sample-id` | 单样本模式：指定样本 ID |
| `--sample-ids` | 多样本批量模式：逗号分隔的样本 ID 列表 |
| `--platform` | 目标平台（单样本模式必填） |
| `--exec-plan` | 执行计划 YAML（多样本模式推荐） |
| `--test-case-ids` | 指定重跑的用例 ID 列表（逗号分隔，仅单样本模式生效） |

### `evalapp report`

生成或重新生成评测报告。

```bash
# 为最新一次评测重新生成报告
evalapp report

# 指定 Run ID
evalapp report --run-id <run_id>

# 生成含历史对比的报告
evalapp report --compare

# 指定工作区重新生成汇总报告
evalapp report --workspace <workspace_path>
```

### `evalapp export`

```bash
evalapp export --workspace ./workspace --output ./evaluation.json
# 仅在明确需要替换已有输出时加 --overwrite
```

`--workspace`、`--output` 必填；输出父目录须已存在，输出必须位于输入工作区之外（包括符号链接别名）。已有输出默认拒绝覆盖。成功时原子写入，读取权限或其他 I/O 错误时非零退出并保留原输出。损坏 JSON、编码或结构产生来源提示；没有可识别的结果或计划项则失败。

无需生成器插件；不运行评测、模型评分、报告生成或截图提取，不创建 manifest、命令历史、锁或源缓存。仅支持当前的逐样本 `sample_scores.json`、`scores.json`、`evaluation.json` 和工作区 `execution_manifest.json`。旧 `results/` 分片或仅有 `run_data.json` 的工作区不自动拼装，可显式加载 `EvalRun` 后使用下面的 API。

#### Python API 与 Schema

```python
from pathlib import Path
from evalapp.evaluation.results.models import EvalRun, PromptResult
from evalapp.evaluation.results.export import ResultExport, export_run
from evalapp.services.result_export import export_workspace

run = EvalRun(prompt_results=[PromptResult(
    sample_id="sample", prompt_id="prompt", platform="web",
    generator_name="example", generation_success=False,
)])
memory_result = export_run(run)
workspace_result = export_workspace(Path("./workspace"))
schema = ResultExport.model_json_schema()
payload = memory_result.model_dump(mode="json")
```

两个 API 均返回 `ResultExport`，不写文件；`export_run` 不读文件、不修改输入、不调用 `compute_summary`。已有内存模型默认值按当前对象复制；工作区历史记录不经过原生模型默认值补齐。

#### v1 固定字段

| 字段 | 契约 |
|------|------|
| `schema_version` / `exported_at` | `"1.0"` / 带时区的 UTC 导出时间 |
| `source` | `{kind, run_id, timestamp, native_summary, consistency}`；内存来源复制 run 元数据，工作区的 `run_id/timestamp/native_summary` 为 null，不拼跨批次信息 |
| `source.consistency` | 内存为 `{mode: "in_memory", change_detected: null}`；工作区为 `{mode: "best_effort", change_detected: boolean}` |
| `items[]` | `sample_id/prompt_id/platform/generator_name` 为 string 或 null，另含以下三个独立视图和 `execution`；空字符串原样保留 |
| `evaluation_snapshot` | 逐平台 `prompt_result`：`generation_success`、`success_rate/quality/experience` 原生完整对象、独立评测 `test_results`、`error_message/error_details`、`evidence`、`provenance` |
| `reported_scores` | `{data, provenance}`；`data` 是当前报告仲裁选中的平台分数字典，不冒充完整原生指标模型 |
| `evaluation_observation` | `evaluation.json` 的 `{test_results, error_message, error_details, provenance}`，不覆盖或拼接进快照 |
| `execution` | `{generate, evaluate, overall, provenance}`；仅复制 manifest 已记录状态，不从分数推断 |
| `execution_summary` | `{unit: "sample_platform", total, counts, provenance, interruption}`；仅统计可完整识别、无冲突的 manifest 项，结果目录额外项不加入计划分母 |
| `warnings[]` | `{code, message, sample_id, platform, file, pointer}`；后四项可空，不回显原始文件正文 |

所有包装字段始终存在；无对应来源时整个视图为 null。未知标量、未知数组为 null，明确空数组为 `[]`；原生 None、真实零分、失败计零及 `SKIPPED` 保留。用例保持顺序、原始 ID 和嵌套作用域，重复 ID 不去重；历史只有 passed 时不臆造 status。样本身份冲突分别保留来源并提示，不强行合并。

`evidence` 仅含 `project_path/e2e_report_path/artifact_path/h5_url` 四个可空引用；逐用例报告引用仍在 `test_results` 中。不展开 `process_data.raw`、私有 trace 或完整日志。`provenance` 固定为 `{file, pointer, updated_at, mtime_ns}`：相对工作区路径、JSON Pointer、来源原始时间字符串、读取时文件 mtime；内存的 file/mtime 为 null、pointer 为 `/prompt_results/<index>`。时间优先取平台值，缺失取文件顶层值；旧 naive 时间不强附 UTC。

报告分数沿用**整份样本文件**新鲜度仲裁：sample_scores 严格更新才采用，相等选 scores，缺少内容时间戳回退文件 mtime。不是每个平台各自选最新。快照旧、报告分数新、retest 用例更新时，三个视图各自保留来源，不用旧明细补新分数或重算 composite。

执行状态固定为 `pending/running/completed/failed/skipped/unknown`；缺失或无法识别为 unknown。counts 固定六键，非负整数且总和等于 total；manifest 缺失、损坏、身份不完整或冲突时 total/counts 为 null。执行 completed 不等于断言 passed，不生成“任一平台完成即样本完成”的计数。

`interruption` 为 `{suspected: boolean|null, evidence: [{reason, provenance}]}`；reason 为 `manifest_unfinished/manifest_terminal/run_unfinished/run_finished`。仅参考原始 manifest 与最新非 report run 的收尾记录：明确未终结优先 true；有效终结证据且无相关未知或损坏来源才可 false；缺证据为 null。它不证明进程死亡，内存转换始终为 `{suspected: null, evidence: []}`。不输出混合总分、新通过率或整体 passed。

warnings 首批代码：`missing_snapshot`、`invalid_json`、`invalid_structure`、`identity_conflict`、`source_mismatch`、`unknown_execution_status`、`source_changed`。不同来源时间不一致或无法证明对应时使用 source_mismatch。

#### 一致性与分享边界

工作区是尽力一致读取，不加源写锁、不暂停运行、不自动重试。参与读取的文件仅读取一次，检查读取前后及整轮结束时的 stat 指纹并重新发现候选集合。修改、替换、新增或消失产生 `source_changed`，保留本次成功读取的观察；`change_detected=false` 仅表示未检测到变化，不代表事务快照。

此导出用于**本地数据交换**，自由文本、指标明细、用例和证据路径可能含敏感信息，分享前必须人工脱敏；不会上传或打包证据文件。v1 小版本只增加向后兼容的可选字段，消费者允许忽略新增字段；删除、类型或语义变化须升级主版本。原生指标内部新增明细不改变包装语义。

#### 结构化错误兼容

`EvaluationError`（从 `evalapp.evaluation.results.models` 导入）包含 `origin/stage/code/message/raw_error_type`。origin 为 `generator/evaluator/environment/unknown`，stage 为 `generation/evaluation/build/install/launch/test/scoring/unknown`；原始类型缺失时为空字符串。首批 code：`no_test_cases`、`evaluation_exception`、`test_tool_unavailable`、`build_failed`、`install_failed`、`launch_failed`、`generation_failed`、`generation_not_ready`、`timeout`、`unknown`。

`PromptResult`、`ExecutionResult` 和单用例结果新增默认空的 `error_details`；原 `error_message`、`ProcessCollection.error_type`、FailureCategory、评分公式、失败计零和重试行为不变。仅有 generation_success=false 不推断生成器责任，普通 E2E 断言失败不推断执行器异常；超时依赖明确异常类型或错误代码。新 evaluation.json 还保留用例 status/duration 和错误，定向 retest 保留未重测用例，不写成完整原生快照。

### 全局参数

所有命令通用：

| 参数 | 说明 |
|------|------|
| `--config` | 配置文件路径（默认 `evalapp.yaml`） |
| `--verbose` | 详细输出 |
| `--stream-output` | 实时输出子进程日志 |

## 配置 Schema

`evalapp.yaml` 定义所有运行时配置。完整带注释模板参见 `evalapp.yaml.example`。

### 顶层字段

```yaml
platforms: [web, android, ios]   # 评测目标平台
results_dir: results              # 结果输出目录
```

### 模型配置

```yaml
models:
  e2e:
    api_key: ""       # 视觉模型 API Key（环境变量 MIDSCENE_MODEL_API_KEY）
    base_url: ""      # OpenAI 兼容协议地址（环境变量 MIDSCENE_MODEL_BASE_URL）
    name: ""          # 模型名称（环境变量 MIDSCENE_MODEL_NAME）
    family: ""        # 模型系列标识（环境变量 MIDSCENE_MODEL_FAMILY）
  aesthetics:
    api_key: ""       # 美观度 VL 模型 Key（环境变量 DASHSCOPE_API_KEY）
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    name: "qwen-vl-max"   #（环境变量 AESTHETICS_MODEL）
    family: ""
```

### AI UI 测试配置

```yaml
ai_ui_test:
  timeout: 300          # 单条用例超时秒数
  replan_limit: 20      # 单条用例最大重规划次数
```

### 构建配置

```yaml
build_app:
  script_path: tools/build_app/scripts/build_app.py
  timeout: 1800         # 构建超时秒数
  build_type: debug     # debug | release
  clean: false          # 清洁构建
  android_output_format: apk   # apk | aab
  ios_output_format: app       # app | ipa
```

### 安装配置

```yaml
install_app:
  script_path: tools/install_app/scripts/install_app.py
  timeout: 300
  device_id: null       # null = 自动检测
  auto_install: false
```

### 可选集成

```yaml
# 外部模型服务（可选，增强评测能力）
external_service:
  enabled: false
  api_key: ""           # 环境变量 EXTERNAL_SERVICE_API_KEY

# MCP 工具服务（可选）
mcp:
  enabled: false
  servers: []
```

### 报告配置

```yaml
report:
  auto_open: true       # 自动打开报告
  eval_version: "2.0"   # 评测版本号
```

## 扩展点

框架使用 Python entry points 进行插件发现。姊妹仓可注册能力而无需修改本仓代码。

### 生成器插件

Entry point 组：`evalapp.generators`

注册 `AppGenerator` 子类以提供代码生成能力：

```toml
# 在你的包的 pyproject.toml 中
[project.entry-points."evalapp.generators"]
my_generator = "my_package.generator:MyGenerator"
```

运行时通过 `evalapp.generators.get_generator(name)` 发现。

### CLI 命令插件

Entry point 组：`evalapp.commands`

注册额外的 CLI 子命令，自动挂载到 `evalapp` 命令组：

```toml
# 在你的包的 pyproject.toml 中
[project.entry-points."evalapp.commands"]
generate = "my_package.commands:generate_cmd"
design-samples = "my_package.commands:design_samples_cmd"
```

启动时自动发现并挂载。

## 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 一般错误（配置、文件缺失等） |
| 2 | CLI 用法错误（参数无效） |

## 样本 Schema

### `sample.yaml` 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `sample_id` | string | 唯一样本标识（与目录名一致） |
| `title` | string | 样本中文名 |
| `app_type` | string | 应用品类（传给美观度模型做品类感知评分） |
| `dataset_version` | string | 所属代际：`V1` 或 `V2` |
| `complexity` | string | 复杂度档位 |
| `requires_backend` | boolean | 是否需要真实后端 |
| `requires_auth` | boolean | 是否需要登录态 |
| `requirement` | string | 完整需求描述 |
| `pages` | list | 页面清单，含 `name`、`level`（L1/L2/L3）、`entry_from` |
| `navigation` | object | 导航结构（如 bottom tabs） |
| `core_functions` | list | 核心功能列表 |
| `constraints` | list | 工程约束 |
| `notes` | list | 验证重点 |

### 测试用例 Schema

```json
{
  "prompt_id": "SampleId",
  "platform": "default",
  "test_cases": [
    {
      "id": "TC002",
      "name": "用例名称",
      "description": "验证内容描述",
      "steps": [
        "操作 -> 预期: 结果"
      ],
      "expected_result": "整体预期结果",
      "priority": "P0",
      "category": "core_crud"
    }
  ]
}
```

#### 优先级

| 优先级 | 含义 |
|--------|------|
| `P0` | 最高 — 核心功能 |
| `P1` | 重要 — 次要功能 |
| `P2` | 可选 — 边缘场景 |

#### 分类

| 分类 | 说明 |
|------|------|
| `launch_check` | 应用启动验证 |
| `core_crud` | 核心 CRUD 操作 |
| `form_validation` | 表单输入与校验 |
| `navigation` | 页面导航与路由 |

历史优先级值（`high`、`medium`、`low`）加载时自动归一化。
