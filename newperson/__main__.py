"""命令行入口。

python -m newperson run        启动机器人
python -m newperson check      检查配置/人设/照片索引（不联网）
python -m newperson simulate   假时钟模拟 N 天的回复时机（不联网）
python -m newperson plan       调模型生成今天的日程并打印（联网）
"""

from __future__ import annotations

import argparse
import sys


def cmd_run(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_check(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_simulate(args: argparse.Namespace) -> int:
    """--days N --seed S --messages-per-day M：随机时刻发消息，打印作息状态、热度、notice/reply 时间与 reason。"""
    raise NotImplementedError


def cmd_plan(args: argparse.Namespace) -> int:
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newperson", description="接入 Discord 的虚拟人物")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="启动机器人")
    sub.add_parser("check", help="检查配置")
    sim = sub.add_parser("simulate", help="模拟回复时机")
    sim.add_argument("--days", type=int, default=2)
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--messages-per-day", type=int, default=6)
    sub.add_parser("plan", help="生成今日日程")
    args = parser.parse_args(argv)
    return {"run": cmd_run, "check": cmd_check, "simulate": cmd_simulate, "plan": cmd_plan}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
