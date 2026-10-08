"""只读结果导出 CLI：唯一写入目标为用户指定的源目录外文件。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import click

from ..services.result_export import export_workspace


def _output_path(workspace: Path, output: Path, overwrite: bool) -> Path:
    target = output.resolve()
    if target.is_relative_to(workspace):
        raise ValueError("输出不能位于输入工作区内（包括符号链接别名）")
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    else:
        if not overwrite:
            raise FileExistsError("输出已存在；如需覆盖请显式传入 --overwrite")
    return target


def _atomic_output(target: Path, content: str, overwrite: bool):
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, target)
        else:
            # 原子创建，防止检查后出现的同名文件被覆盖。
            os.link(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@click.command("export")
@click.option("--workspace", required=True, type=click.Path(path_type=Path, file_okay=False), help="当前格式的输入工作区（只读）")
@click.option("--output", required=True, type=click.Path(path_type=Path, dir_okay=False), help="源工作区外的 JSON 输出路径")
@click.option("--overwrite", is_flag=True, help="允许覆盖已有输出文件")
def export_cmd(workspace: Path, output: Path, overwrite: bool):
    """导出 v1 本地交换 JSON；包含自由文本与本地路径，分享前须人工脱敏。"""
    try:
        workspace = workspace.resolve(strict=True)
        target = _output_path(workspace, output, overwrite)
        result = export_workspace(workspace)
        if _output_path(workspace, output, overwrite) != target:
            raise ValueError("输出路径在导出期间发生变化")
        _atomic_output(target, result.model_dump_json(indent=2), overwrite)
    except (OSError, ValueError) as exc:
        # 不回显源文件正文或异常中的潜在自由文本。
        raise click.ClickException(f"导出失败（{type(exc).__name__}）：请检查输入格式、路径、权限及覆盖选项。") from exc
    click.echo(f"已导出 {len(result.items)} 条来源观察，{len(result.warnings)} 条提示。分享前请人工脱敏。")
