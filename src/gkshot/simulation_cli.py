import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from gkshot.models import Loadout
from gkshot.simulation import atomic_json, compare, compose_memories, read_ready_memory, list_ready_memories
from gkshot.adapters.local_engine import SimulationError


def main(argv=None):
    parser = argparse.ArgumentParser(description="GKTools 离线真实模拟 / 同场候选比较")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("compare", help="一个或多个完整 Loadout JSON")
    run.add_argument("loadouts", nargs="+", type=Path)
    run.add_argument("--trials", type=int, default=100)
    run.add_argument("--seed", type=int, default=610397104)
    run.add_argument("--timeout", type=float, default=300, help="每个候选超时秒数")
    run.add_argument("--output", type=Path, default=Path("work/simulation/result.json"))
    run.add_argument("--cache", type=Path, default=Path("work/simulation/cache"))
    run.add_argument("--no-cache", action="store_true")
    compose = sub.add_parser("compose", help="只读指定记忆库，合成明确主副记忆")
    compose.add_argument("--library", type=Path, required=True)
    compose.add_argument("--main", required=True)
    compose.add_argument("--sub", required=True)
    compose.add_argument("--stage", required=True, type=int)
    compose.add_argument("--support-bonus", required=True, type=float, help="比例，例如 0.0472")
    compose.add_argument("--output", type=Path, required=True)
    sub.add_parser("gui", help="打开本地候选比较窗口")
    web = sub.add_parser('web', help='打开本地网页模拟器')
    web.add_argument('--port', type=int, default=8765)
    web.add_argument('--no-browser', action='store_true')
    memories = sub.add_parser('memories', help='只读列出可用主副记忆的 ID 和属性')
    memories.add_argument('--library', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'web':
            from gkshot.simulation_web import launch
            launch(port=args.port, open_browser=not args.no_browser)
            return 0
        if args.command == 'memories':
            print('记忆ID\t人物版本ID\tVo/Da/Vi/体力')
            for memory_id, draft in list_ready_memories(args.library):
                print(f"{memory_id}\t{draft['p_idol_id']}\t" + '/'.join(str(draft['parameters'][k]) for k in ('vocal','dance','visual','stamina')))
            return 0
        if args.command == "gui":
            from gkshot.simulation_gui import launch
            launch()
            return 0
        if args.command == "compose":
            if args.main == args.sub:
                raise ValueError("主副记忆不能使用同一个持有副本")
            result = compose_memories(read_ready_memory(args.library, args.main),
                read_ready_memory(args.library, args.sub), stage_id=args.stage, support_bonus=args.support_bonus)
            atomic_json(args.output, result.to_dict())
            print(f"已保存完整编成：{args.output}")
            return 0
        candidates = [(str(p.resolve()), Loadout.load(p)) for p in args.loadouts]
        protected = {p.resolve() for p in args.loadouts}
        if args.output.resolve() in protected:
            raise ValueError("结果路径不能覆盖输入 Loadout")
        cancel = threading.Event()
        previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
        try:
            report = compare(candidates, trials=args.trials, seed=args.seed, timeout=args.timeout,
                cache=None if args.no_cache else args.cache, cancel=cancel,
                progress=lambda name, n, total: print(f"\r{Path(name).name}: {n}/{total}    ", end="", file=sys.stderr))
        finally:
            signal.signal(signal.SIGINT, previous)
        atomic_json(args.output, report)
        print("\n候选\t平均分\t标准差\t次数\t最低\t最高")
        for name in report["ranking"]:
            entry = next(r for r in report["results"] if r["name"] == name)
            s = entry["summary"]
            deviation = "-" if s["sample_stddev"] is None else f'{s["sample_stddev"]:.2f}'
            print(f'{Path(name).name}\t{s["mean"]:.2f}\t{deviation}\t{s["trials"]}\t{s["min"]}\t{s["max"]}')
        for entry in report["results"]:
            if entry["status"] == "error":
                print(f'{entry["name"]}: {entry["error"]}', file=sys.stderr)
        print(f'{report["note"]}\n状态：{report["status"]}；完整结果：{args.output}')
        return 130 if report["status"] == "cancelled" else 1 if report["status"] != "complete" else 0
    except (ValueError, OSError, SimulationError, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
