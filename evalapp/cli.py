"""EvalApp CLI - main entry point."""

from __future__ import annotations

import click
from rich.console import Console

from .config import Config, get_config, load_config, set_config
from .utils.logging import setup_logging, get_logger

logger = get_logger(__name__)
console = Console()


def _load_config(config_path: str | None) -> Config:
    """加载配置。

    · 未传 ``--config``：复用全局单例（其他模块同样调用 ``get_config()`` 获取）。
    · 传入显式路径：调用内部 ``load_config()``，然后反向注入到单例缓存，
      以保证全局只存在一份 Config 实例。
    """
    if config_path is None:
        return get_config()
    cfg = load_config(config_path)
    set_config(cfg)
    return cfg


@click.group()
@click.option("--config", "config_path", default=None, help="配置文件路径")
@click.option("--verbose", is_flag=True, help="详细输出")
@click.option("--stream-output", is_flag=True, help="实时输出子进程日志")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, verbose: bool, stream_output: bool) -> None:
    """EvalApp - AI App Generator evaluation framework."""
    setup_logging(verbose)
    ctx.ensure_object(dict)
    config = _load_config(config_path)
    if stream_output or verbose:
        config.stream_output = True
    ctx.obj["config"] = config
    ctx.obj["verbose"] = verbose


# 注册子命令（生成类命令由生成仓 daimax-appbench-gen 经 entry point 注册回来）
from .commands.evaluate import evaluate_cmd, retest_cmd
from .commands.reporting import report
from .commands.workspace import migrate_workspace_cmd
from .commands.history import history_cmd
from .commands.exporting import export_cmd

main.add_command(evaluate_cmd)
main.add_command(report)
main.add_command(migrate_workspace_cmd)
main.add_command(retest_cmd)
main.add_command(history_cmd)
main.add_command(export_cmd)

# 第三方插件命令（如生成仓提供的 generate / design-samples / run）：
# 通过 entry point 组 "evalapp.commands" 自动发现并注册。
try:
    import importlib.metadata as _importlib_metadata

    for _ep in _importlib_metadata.entry_points(group="evalapp.commands"):
        try:
            main.add_command(_ep.load())
        except Exception as _e:  # noqa: BLE001 - 插件加载失败不影响核心命令
            logger.debug("插件命令 %s 加载失败: %s", _ep.name, _e)
except Exception as _e:  # noqa: BLE001
    logger.debug("entry point 扫描失败（不影响核心命令）: %s", _e)

# deploy 命令（如果存在）
try:
    from .deploy import deploy_group
    main.add_command(deploy_group)
except ImportError as e:
    logger.debug("deploy 子命令未加载（可选功能）: %s", e)


if __name__ == "__main__":
    main()
