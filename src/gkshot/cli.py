import argparse
import json
import sys
import tomllib
from pathlib import Path

from gkshot.adapters.gktools import DEFAULT_BASE_URL, GKToolsAdapter
from gkshot.models import Loadout
from gkshot.pipeline import run
from gkshot.recognition.manual import ManualAnnotationRecognizer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Screenshot → Loadout JSON → GKTools URL")
    parser.add_argument("--config", type=Path, help="Optional TOML config (explicit path)")
    commands = parser.add_subparsers(dest="command", required=True)
    url = commands.add_parser("url", help="Generate a GKTools URL from validated JSON")
    url.add_argument("loadout", type=Path)
    scan = commands.add_parser("scan", help="Recognize pixels offline and save a draft for review")
    scan.add_argument("screenshot", type=Path)
    scan.add_argument("--output", type=Path, required=True)
    scan.add_argument("--stage", type=int)
    scan.add_argument("--support-bonus", type=float, help="Ratio, e.g. 0.0472 = 4.72%%")
    scan.add_argument("--layout", type=Path)
    commands.add_parser("gui", help="Open the local screenshot review window")
    commands.add_parser("memory-gui", help="Open the owned-memory library")
    commands.add_parser("capture-gui", help="Standalone local raw screenshot collector")
    process = commands.add_parser("capture-process", help="Reprocess a saved capture session without the game")
    process.add_argument("session", type=Path)
    tooltip = commands.add_parser("card-detail-scan", help="Recognize an expanded card tooltip locally")
    tooltip.add_argument("screenshot", type=Path)
    tooltip.add_argument("--output", type=Path, required=True)
    tooltip.add_argument("--card-id", type=int, help="Optional expected card ID; a different title is rejected")
    attach = commands.add_parser("memory-detail", help="Read and attach one tooltip to an existing owned memory")
    attach.add_argument("memory_id")
    attach.add_argument("screenshot", type=Path)
    attach.add_argument("--library", type=Path)
    attach.add_argument("--slot", type=int, choices=range(1, 7), help="Explicit 1-based slot for repeated cards")
    for name in ("memory-scan", "memory-import"):
        command = commands.add_parser(name, help="Merge pages of ONE owned memory" if name == "memory-scan" else "Import ONE memory into the local library")
        command.add_argument("screenshots", type=Path, nargs="+")
        command.add_argument("--context", type=Path, help="Optional screenshot of the memory selected in the list")
        if name == "memory-scan":
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--library", type=Path)
            command.add_argument("--capture-id", help="Optional stable ID from the capture session")
    batch = commands.add_parser("memory-batch", help="Import explicitly grouped memories from a JSON manifest")
    batch.add_argument("manifest", type=Path)
    batch.add_argument("--library", type=Path)
    library_list = commands.add_parser("memory-list", help="List owned memories, with review status")
    library_list.add_argument("--library", type=Path)
    library_list.add_argument("--reviewed-only", action="store_true")
    library_list.add_argument("--ready-only", action="store_true", help="Include automatically resolved and human-reviewed memories")
    recognize = commands.add_parser("recognize", help="Run the screenshot pipeline using manual annotations")
    recognize.add_argument("screenshot", type=Path)
    recognize.add_argument("--annotations", type=Path, help="JSON with image_sha256 and loadout")
    recognize.add_argument("--output", type=Path, required=True, help="New Loadout JSON file; refuses overwrite")
    args = parser.parse_args(argv)
    try:
        base_url = DEFAULT_BASE_URL
        if args.config:
            config = tomllib.loads(args.config.read_text(encoding="utf-8-sig"))
            base_url = config.get("gktools", {}).get("base_url", base_url)
        adapter = GKToolsAdapter(base_url)
        if args.command == "gui":
            from gkshot.gui import launch
            launch(base_url)
        elif args.command == "memory-gui":
            from gkshot.memory_gui import launch
            launch()
        elif args.command == "capture-gui":
            from gkshot.capture.gui import launch
            launch()
        elif args.command == "capture-process":
            from gkshot.capture.process import process_session
            result = process_session(args.session, notify=lambda text: print(text, file=sys.stderr))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if any(row["errors"] for row in result["results"]) else 0
        elif args.command in ("card-detail-scan", "memory-detail"):
            from gkshot.recognition.card_detail import CardDetailScanner
            if args.command == "card-detail-scan" and args.output.exists():
                raise ValueError(f"Output already exists: {args.output}")
            scanner = CardDetailScanner()
            report = scanner.scan(args.screenshot, expected_card_id=getattr(args, "card_id", None))
            if args.command == "card-detail-scan":
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                print(f"{report['status']}: {report['token']!r}; saved: {args.output}")
                return 0 if report["status"] == "matched" else 2
            from gkshot.memory_library import MemoryLibrary, DEFAULT_LIBRARY
            result = MemoryLibrary(args.library or DEFAULT_LIBRARY).add_card_detail(
                args.memory_id, report, catalog=scanner.catalog, slot=args.slot - 1 if args.slot else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["detail_status"] in ("matched", "already-attached") else 2
        elif args.command.startswith("memory-"):
            from gkshot.memory_library import MemoryLibrary, DEFAULT_LIBRARY
            if args.command == "memory-list":
                records = MemoryLibrary(args.library or DEFAULT_LIBRARY).list(reviewed_only=args.reviewed_only,ready_only=args.ready_only)
                print(json.dumps([{"memory_id": r["memory_id"], "status": r["status"], "identity": r["report"]["identity"]} for r in records], ensure_ascii=False, indent=2))
                return 0
            from gkshot.recognition.memory import MemoryScanner
            if args.command == "memory-scan" and args.output.exists():
                raise ValueError(f"Output already exists: {args.output}")
            scanner = MemoryScanner()
            if args.command == "memory-batch":
                from gkshot.memory_batch import import_batch
                result = import_batch(args.manifest, scanner, MemoryLibrary(args.library or DEFAULT_LIBRARY))
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 2 if any("error" in row for row in result) else 0
            report = scanner.scan(args.screenshots, context_path=args.context)
            if args.command == "memory-scan":
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                print(f"Saved memory review draft: {args.output}")
            else:
                result = MemoryLibrary(args.library or DEFAULT_LIBRARY).import_report(report, capture_id=args.capture_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "scan":
            if args.output.exists():
                raise ValueError(f"Output already exists: {args.output}")
            from gkshot.recognition.automatic import ScreenshotScanner
            report = ScreenshotScanner(args.layout).scan(args.screenshot, stage_id=args.stage,
                                                         support_bonus=args.support_bonus)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            print(f"Saved review draft: {args.output}")
            print("Review stage, support bonus, cards and customizations in the GUI before exporting a URL.")
        elif args.command == "url":
            print(adapter.to_url(Loadout.load(args.loadout)))
        else:
            if not args.annotations:
                raise ValueError("Use 'scan' for automatic recognition or supply --annotations for manual pipeline mode.")
            result = run(args.screenshot, ManualAnnotationRecognizer(args.annotations), adapter)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result.recognition.loadout.to_dict(), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            for warning in result.recognition.warnings:
                print(warning, file=sys.stderr)
            print(result.url)
    except ImportError as error:
        print(f"Missing recognition dependency: {error}. Run setup.cmd, then launch.cmd.", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
