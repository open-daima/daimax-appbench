# API Reference

Complete reference for the `evalapp` CLI commands, configuration schema, and extension points.

## CLI Commands

### Command Overview

| Command | Description |
|---------|-------------|
| `evalapp evaluate` | Evaluate artifacts or workspace code (build → install → E2E → score → report) |
| `evalapp retest` | Re-run E2E tests and regenerate report |
| `evalapp report` | Generate or regenerate report for a completed evaluation |
| `evalapp export` | Read-only v1 JSON export for local data exchange, without rescoring |
| `evalapp history` | View workspace execution history |
| `evalapp migrate-workspace` | Migrate legacy workspace directory structures |

### `evalapp evaluate`

The primary command for running evaluations. Supports three modes:

#### Artifact Mode (Recommended)

Evaluate a pre-built artifact directly:

```bash
# Web application (URL)
evalapp evaluate --url <url> --sample-ids <id> [OPTIONS]

# Android APK
evalapp evaluate --apk <path> --sample-ids <id> [OPTIONS]

# iOS application
evalapp evaluate --app <path> --sample-ids <id> [OPTIONS]

# Source project (requires --platform)
evalapp evaluate --project <dir> --platform <platform> --sample-ids <id> [OPTIONS]
```

#### Batch Mode

Evaluate multiple samples from a dataset directory:

```bash
evalapp evaluate --workspace <path> --samples-dir <dir> --platform <platform> [OPTIONS]
```

#### Execution Plan Mode

Fine-grained control over evaluation order and scope:

```bash
evalapp evaluate --workspace <path> --exec-plan <file> [OPTIONS]
```

#### Parameters

| Parameter | Description |
|-----------|-------------|
| `--url` | Deployed web application URL (artifact mode) |
| `--apk` | Pre-built Android APK path (artifact mode) |
| `--app` | Pre-built iOS .app path (artifact mode) |
| `--project` | Source code project directory (artifact mode, requires `--platform`) |
| `--output` | Results output directory (artifact mode, default: `./eval_output`) |
| `--workspace` | Existing workspace directory path (optional in artifact mode) |
| `--samples-dir` | Sample dataset directory (batch mode, mutually exclusive with `--exec-plan`) |
| `--exec-plan` | Execution plan YAML path (mutually exclusive with `--samples-dir`) |
| `--platform` | Target platform: `web` / `android` / `ios` |
| `--sample-ids` | Comma-separated sample ID list (defaults to all samples in plan) |
| `--sample-id` | Single sample ID (deprecated, use `--sample-ids`) |
| `--generator` | Generator name (default: inferred from workspace directory name) |
| `--workers` | Concurrent evaluation threads (default: 1) |
| `--show-browser` | Show browser UI (headed mode); default is headless |
| `--no-uninstall` | Keep app installed after evaluation |
| `--no-open-report` | Don't auto-open report in browser |
| `--wait-generate` | Pipeline mode: wait for generation per sample before evaluating |

#### Platform Auto-Detection

| Artifact Flag | Inferred Platform |
|---------------|-------------------|
| `--url` | `web` |
| `--apk` | `android` |
| `--app` | `ios` |
| `--project` | Must specify `--platform` explicitly |

### `evalapp retest`

Re-run E2E tests for previously evaluated samples without rebuilding or reinstalling.

#### Single-Sample Mode

```bash
evalapp retest --workspace <path> --sample-id <id> --platform <platform> [OPTIONS]
```

#### Multi-Sample Mode

```bash
evalapp retest --workspace <path> --sample-ids <id1>,<id2> --exec-plan <file> [OPTIONS]
```

#### Parameters

| Parameter | Description |
|-----------|-------------|
| `--workspace` | Workspace path (required) |
| `--sample-id` | Single sample ID (single-sample mode) |
| `--sample-ids` | Comma-separated sample IDs (multi-sample mode) |
| `--platform` | Target platform (required in single-sample mode) |
| `--exec-plan` | Execution plan YAML (recommended for multi-sample mode) |
| `--test-case-ids` | Comma-separated test case IDs to re-run (single-sample mode only) |

### `evalapp report`

Generate or regenerate evaluation reports.

```bash
# Regenerate report for latest evaluation
evalapp report

# Specify a particular run
evalapp report --run-id <run_id>

# Generate comparison report with history
evalapp report --compare

# Regenerate summary for a workspace
evalapp report --workspace <workspace_path>
```

### `evalapp export`

```bash
evalapp export --workspace ./workspace --output ./evaluation.json
```

Both options are required. The output parent must exist; the output must be outside the input workspace, including symlink aliases. Existing output is rejected unless `--overwrite` is explicit. Output is atomic; permission and other I/O errors fail without replacing existing output. Invalid JSON, encoding or structure produces source warnings. No recognizable result or planned item is an error.

No generator plugin is required. Export does not evaluate, score, generate reports, extract screenshots, create/recover manifests, or write command history, source locks or caches. Current per-sample `sample_scores.json`, `scores.json`, `evaluation.json` and workspace `execution_manifest.json` are supported. Legacy `results/` shards and run_data-only workspaces are not assembled automatically; load an `EvalRun` explicitly and use the Python API instead.

#### Python API and Schema

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

Both APIs return `ResultExport` without writing files. `export_run` does not read files, mutate its input or call `compute_summary`. It copies the current in-memory model values, including existing defaults. Workspace records are not loaded through native model defaults.

#### Fixed v1 Fields

| Field | Contract |
|-------|----------|
| `schema_version` / `exported_at` | `"1.0"` / timezone-aware UTC export time |
| `source` | `{kind, run_id, timestamp, native_summary, consistency}`; run metadata is copied only from EvalRun; workspace run_id/timestamp/native_summary are null |
| `source.consistency` | Memory: `{mode: "in_memory", change_detected: null}`; workspace: `{mode: "best_effort", change_detected: boolean}` |
| `items[]` | Nullable string identities `sample_id/prompt_id/platform/generator_name`, the three views below, and `execution`; empty strings are preserved |
| `evaluation_snapshot` | Per-platform `prompt_result`: `generation_success`, full native `success_rate/quality/experience` objects, independent `test_results`, `error_message/error_details`, `evidence`, `provenance` |
| `reported_scores` | `{data, provenance}`; current report-selected platform scores, not a full native metric model |
| `evaluation_observation` | `{test_results, error_message, error_details, provenance}` from evaluation.json, never merged into the snapshot |
| `execution` | `{generate, evaluate, overall, provenance}`, copied only from recorded manifest state |
| `execution_summary` | `{unit: "sample_platform", total, counts, provenance, interruption}`; identifiable, conflict-free manifest items only; extra result-directory observations do not enlarge the planned denominator |
| `warnings[]` | `{code, message, sample_id, platform, file, pointer}`; the last four fields are nullable; messages do not echo source contents |

All wrapper fields are present. A missing source makes its entire view null. Unknown scalars/lists are null; explicitly recorded empty lists remain `[]`. Native nulls, real zero scores, failure-zero policy and `SKIPPED` remain distinct. Tests keep input order, original IDs and sample/platform scope; duplicate IDs are not deduplicated, and passed-only legacy cases do not gain invented status fields. Identity conflicts keep separate source observations with warnings.

`evidence` contains only nullable `project_path/e2e_report_path/artifact_path/h5_url` references; per-test report references stay in test_results. No process_data.raw, private traces or full logs are expanded. `provenance` is `{file, pointer, updated_at, mtime_ns}`: workspace-relative file, JSON Pointer, original timestamp and read-time file mtime. Memory uses null file/mtime and `/prompt_results/<index>`. Platform timestamps take priority over file-level timestamps; naive historical timestamps are not assigned UTC.

Report arbitration remains **whole-sample-file** based: sample_scores wins only when strictly newer; ties choose scores, and missing content timestamps fall back to mtime. It does not select each platform's newest record independently. An old snapshot, newer report scores and retest observations keep separate provenance; export never fills new scores with old details or recomputes composite scores.

Execution states are `pending/running/completed/failed/skipped/unknown`; missing/unrecognized states become unknown. Counts have exactly these six non-negative keys and sum to total. Missing/corrupt manifests, incomplete identities or conflicts yield null total/counts. Completed execution does not imply passed assertions, and completing one platform never becomes a sample-level completion claim.

`interruption` is `{suspected: boolean|null, evidence: [{reason, provenance}]}` with reasons `manifest_unfinished/manifest_terminal/run_unfinished/run_finished`. Raw manifest and latest non-report run finalization records are used: positive unfinished evidence wins; valid terminal evidence allows false only without relevant unknown/corrupt sources; absent evidence means null. This is not proof of process death. Memory always returns `{suspected: null, evidence: []}`. No mixed aggregate score, new pass rate or overall passed flag is derived.

Warning codes: `missing_snapshot`, `invalid_json`, `invalid_structure`, `identity_conflict`, `source_mismatch`, `unknown_execution_status`, `source_changed`. Different timestamps or unprovable source correspondence produce source_mismatch.

#### Consistency and Sharing

Workspace reads are best effort, not directory-wide transactions. No source write lock, process pause or automatic retry is used. Each participating file is read once; stat fingerprints are checked before/after reads and at completion, with candidate rediscovery. Modification, replacement, addition or disappearance produces source_changed while retaining successful observations. `change_detected=false` means only that no change was detected.

This is **local data exchange**, not a public-sharing format: free text, metric details, tests and evidence paths may contain sensitive information and require manual redaction before sharing. Nothing is uploaded or bundled. v1 minor versions may add backward-compatible optional fields; consumers may ignore new fields. Removal, type or semantic changes require a major version. New native metric details do not change wrapper semantics.

#### Structured Error Compatibility

`EvaluationError` (imported from `evalapp.evaluation.results.models`) has `origin/stage/code/message/raw_error_type`. Origins: `generator/evaluator/environment/unknown`. Stages: `generation/evaluation/build/install/launch/test/scoring/unknown`. Missing raw_error_type is an empty string. Initial codes: `no_test_cases`, `evaluation_exception`, `test_tool_unavailable`, `build_failed`, `install_failed`, `launch_failed`, `generation_failed`, `generation_not_ready`, `timeout`, `unknown`.

PromptResult, ExecutionResult and individual test results gain a default-empty error_details list. Existing error_message, ProcessCollection.error_type, FailureCategory, scoring, failure-zero and retry behavior remain unchanged. generation_success=false alone does not assign generator responsibility; assertion failure alone is not an executor exception. Timeout requires a known exception type or explicit code. New evaluation.json records preserve status/duration/errors; targeted retests retain untested cases without writing a partial result as a full native snapshot.

### Global Parameters

Available for all commands:

| Parameter | Description |
|-----------|-------------|
| `--config` | Configuration file path (default: `evalapp.yaml`) |
| `--verbose` | Enable verbose output |
| `--stream-output` | Stream subprocess logs in real-time |

## Configuration Schema

The `evalapp.yaml` file defines all runtime configuration. See `evalapp.yaml.example` for a complete annotated template.

### Top-Level Fields

```yaml
platforms: [web, android, ios]   # Target platforms
results_dir: results              # Results output directory
```

### Models Configuration

```yaml
models:
  e2e:
    api_key: ""       # Vision model API key (env: MIDSCENE_MODEL_API_KEY)
    base_url: ""      # OpenAI-compatible endpoint (env: MIDSCENE_MODEL_BASE_URL)
    name: ""          # Model name (env: MIDSCENE_MODEL_NAME)
    family: ""        # Model family identifier (env: MIDSCENE_MODEL_FAMILY)
  aesthetics:
    api_key: ""       # Aesthetics VL model key (env: DASHSCOPE_API_KEY)
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    name: "qwen-vl-max"   # (env: AESTHETICS_MODEL)
    family: ""
```

### AI UI Test Configuration

```yaml
ai_ui_test:
  timeout: 300          # Per-test-case timeout in seconds
  replan_limit: 20      # Max replanning attempts per test case
```

### Build Configuration

```yaml
build_app:
  script_path: tools/build_app/scripts/build_app.py
  timeout: 1800         # Build timeout in seconds
  build_type: debug     # debug | release
  clean: false          # Clean build
  android_output_format: apk   # apk | aab
  ios_output_format: app       # app | ipa
```

### Install Configuration

```yaml
install_app:
  script_path: tools/install_app/scripts/install_app.py
  timeout: 300
  device_id: null       # null = auto-detect
  auto_install: false
```

### Optional Integrations

```yaml
# External model service (optional, advanced capabilities)
external_service:
  enabled: false
  api_key: ""           # env: EXTERNAL_SERVICE_API_KEY

# MCP tool servers (optional)
mcp:
  enabled: false
  servers: []
```

### Report Configuration

```yaml
report:
  auto_open: true       # Auto-open report in browser
  eval_version: "2.0"   # Evaluation version identifier
```

## Extension Points

The framework uses Python entry points for plugin discovery. Sister repositories can register capabilities without modifying this codebase.

### Generator Plugins

Entry point group: `evalapp.generators`

Register an `AppGenerator` subclass to provide code generation capabilities:

```toml
# In your package's pyproject.toml
[project.entry-points."evalapp.generators"]
my_generator = "my_package.generator:MyGenerator"
```

Generators are discovered at runtime via `evalapp.generators.get_generator(name)`.

### CLI Command Plugins

Entry point group: `evalapp.commands`

Register additional CLI subcommands that mount to the `evalapp` command group:

```toml
# In your package's pyproject.toml
[project.entry-points."evalapp.commands"]
generate = "my_package.commands:generate_cmd"
design-samples = "my_package.commands:design_samples_cmd"
```

Commands are auto-discovered and mounted at startup.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error (configuration, missing files, etc.) |
| 2 | CLI usage error (invalid arguments) |

## Sample Dataset Schema

### `sample.yaml` Fields

| Field | Type | Description |
|-------|------|-------------|
| `sample_id` | string | Unique sample identifier (matches directory name) |
| `title` | string | Human-readable sample name |
| `app_type` | string | Application category (passed to aesthetics model) |
| `dataset_version` | string | Dataset generation: `V1` or `V2` |
| `complexity` | string | Complexity tier |
| `requires_backend` | boolean | Whether real backend is required |
| `requires_auth` | boolean | Whether authentication is required |
| `requirement` | string | Full requirement description |
| `pages` | list | Page definitions with `name`, `level` (L1/L2/L3), `entry_from` |
| `navigation` | object | Navigation structure (e.g., bottom tabs) |
| `core_functions` | list | Core function descriptions |
| `constraints` | list | Engineering constraints |
| `notes` | list | Verification emphasis points |

### Test Case Schema

```json
{
  "prompt_id": "SampleId",
  "platform": "default",
  "test_cases": [
    {
      "id": "TC002",
      "name": "Test case name",
      "description": "What this test verifies",
      "steps": [
        "Action -> Expected: Result"
      ],
      "expected_result": "Overall expected outcome",
      "priority": "P0",
      "category": "core_crud"
    }
  ]
}
```

#### Priority Levels

| Priority | Meaning |
|----------|---------|
| `P0` | Highest — core functionality |
| `P1` | Important — secondary features |
| `P2` | Nice-to-have — edge cases |

#### Categories

| Category | Description |
|----------|-------------|
| `launch_check` | Application startup verification |
| `core_crud` | Core CRUD operations |
| `form_validation` | Form input and validation |
| `navigation` | Page navigation and routing |

Legacy priority values (`high`, `medium`, `low`) are automatically normalized on load.
