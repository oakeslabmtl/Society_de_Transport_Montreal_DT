import argparse
import logging
import math
import re
import sys
import time
from pathlib import Path
from recording import NothingToForecast, RecordingError
from recording.paths import DATASETS_DIR, RECORDINGS_DIR, REPLAYS_DIR, ROOT, ROUTE_STOPS_FILE, display_path, repo_relative
from recording.store import DEFAULT_POLL_INTERVAL, OCCUPANCY_LABELS, TIMEZONE, RecordingStore, sort_routes
from recording.timeline import (format_clock, format_duration, format_span, local_datetime, parse_local_time, select_window,
                                start_option)

PROGRAM = "python -m recording"
LOCATION_MODEL = "location"  # forecast --model value of the location model (recording.location_model)

DESCRIPTION = """\
Record STM bus positions and turn them into replays for the 3D viewer (no RabbitMQ, MongoDB or Docker needed).

  1. record      poll the STM API into a .sqlite recording (needs STM_API_KEY)
     import-csv  or convert a MongoDB CSV export (e.g. datasets/20250301-buses.csv) into a recording
  2. info        see what a recording contains and its best time windows
  3. export      write a replay .json (default: visualization/replays/, bundled into the demo build)
  4. forecast    optional: add occupancy forecasts to a replay, for the viewer's bus chart (needs Timestral,
                 or --model location: the location model, numpy only)

  route-stops    the stops of every bus line and direction, highlighted by the viewer for the selected bus
                 (from the static GTFS in datasets/; run it again when the GTFS is updated)
"""

EXAMPLES = f"""\
examples (from the software/ folder; from the repo root use `python software/recording ...` and drop `../` from the paths):
  {PROGRAM} record --routes "24,51,55,80,129,165" --minutes 60 --name weekday-downtown
  {PROGRAM} info ../recordings/weekday-downtown.sqlite
  {PROGRAM} export ../recordings/weekday-downtown.sqlite --minutes 60 --name "Weekday afternoon, downtown"
  ..\\.venv-forecast\\Scripts\\{PROGRAM} forecast ../visualization/replays/weekday-downtown_1610-1710.json   (see forecast --help)
  {PROGRAM} import-csv ../datasets/20250301-buses.csv
  {PROGRAM} route-stops
"""


def main(argv:list[str]=None) -> int:
    # UTF-8 output even when redirected to a file or a pipe (Windows would use the ANSI code page)
    for stream, errors in ((sys.stdout, "replace"), (sys.stderr, "backslashreplace")):
        try:
            stream.reconfigure(encoding="utf-8", errors=errors)
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 0
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.handler(args) or 0
    except RecordingError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    formatter = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(prog=program_name(), description=DESCRIPTION, epilog=EXAMPLES, formatter_class=formatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="show debug logs")
    commands = parser.add_subparsers(title="commands", metavar="COMMAND")

    record = commands.add_parser(
        "record", parents=[common], formatter_class=formatter, help="poll the STM API into a recording",
        description="Poll the STM vehicle positions API and store every poll in a .sqlite recording.\n"
                    "Needs only the STM_API_KEY environment variable. Stop early with Ctrl+C (what was recorded is kept).",
        epilog=f"example (from the software/ folder):\n  {PROGRAM} record --routes \"24,51,55,80,129,165\" --minutes 60 --name \"weekday-rush\"")
    record.add_argument("--routes", default="", help='bus routes to keep, e.g. "24,51,55,80,129,165" (default: all routes); '
                                                     'the 3D map covers downtown Montreal only')
    record.add_argument("--minutes", type=float, help="how long to record (default: until Ctrl+C)")
    record.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL, help="seconds between polls (default: %(default)g)")
    record.add_argument("--name", help="recording name (default: stm_<YYYY-MM-DD_HHMM>_routes-<routes>)")
    record.add_argument("--out", help="output .sqlite path (default: recordings/<name>.sqlite at the repo root)")
    record.add_argument("--export", action="store_true", help="also export the whole recording as a replay when done")
    record.add_argument("--force", action="store_true", help="replace the output file if it already exists")
    record.set_defaults(handler=run_record)

    import_csv = commands.add_parser(
        "import-csv", parents=[common], formatter_class=formatter, help="convert a MongoDB CSV export into a recording",
        description="Convert a CSV export of the live messages (one row per bus position, dotted column names) into a recording.\n"
                    "Poll times come from the MongoDB '_id' column (insert time), else a 'poll_time' column (unix seconds),\n"
                    "else positions are grouped by 'vehicle.timestamp' into --interval buckets (approximate).\n"
                    "Values get the types of the live messages; empty cells are dropped; unknown columns are kept as text.",
        epilog=f"example (from the software/ folder):\n  {PROGRAM} import-csv ../datasets/20250301-buses.csv --name 2025-03-01_saturday")
    import_csv.add_argument("csv", metavar="CSV", help="the CSV file")
    import_csv.add_argument("--name", help="recording name (default: the CSV file name)")
    import_csv.add_argument("--out", help="output .sqlite path (default: recordings/<name>.sqlite at the repo root)")
    import_csv.add_argument("--cluster-gap", type=float, default=5.0,
                            help="rows inserted at most this many seconds apart are one poll (default: %(default)g)")
    import_csv.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL,
                            help="poll interval of the original recording, in seconds (default: %(default)g)")
    import_csv.add_argument("--force", action="store_true", help="replace the output file if it already exists")
    import_csv.set_defaults(handler=run_import_csv)

    info = commands.add_parser(
        "info", parents=[common], formatter_class=formatter, help="summarize a recording",
        description="Time span, gaps, vehicles, routes, occupancy and the best time windows to export.",
        epilog=f"example (from the software/ folder):\n  {PROGRAM} info ../recordings/20250301-buses.sqlite")
    info.add_argument("recording", metavar="RECORDING", help="the .sqlite recording")
    info.add_argument("--minutes", type=float, default=60, help="length of the suggested windows (default: %(default)g)")
    info.set_defaults(handler=run_info)

    export = commands.add_parser(
        "export", parents=[common], formatter_class=formatter, help="write a replay .json for the 3D viewer",
        description="Write a replay file for the Godot viewer.\n"
                    "  no --start/--minutes   the whole recording\n"
                    "  --minutes N            the best gap-free N minutes (most bus positions)\n"
                    "  --start T --minutes N  N minutes from T (local time of the recording, HH:MM or 'YYYY-MM-DD HH:MM')\n"
                    "  --start T              from T to the end\n"
                    "Failed polls are skipped (the replay has a gap there).",
        epilog=f"examples (from the software/ folder):\n  {PROGRAM} export ../recordings/x.sqlite --minutes 60 --name \"Saturday afternoon\"\n"
               f"  {PROGRAM} export ../recordings/x.sqlite --start 15:30 --minutes 10 --routes \"24,51\"")
    export.add_argument("recording", metavar="RECORDING", help="the .sqlite recording")
    export.add_argument("--start", help="window start, local time: HH:MM (on the recording's date) or 'YYYY-MM-DD HH:MM'")
    export.add_argument("--minutes", type=float, help="window length in minutes")
    export.add_argument("--routes", help='only these routes, e.g. "24,51" (default: all recorded routes)')
    export.add_argument("--name", help="title shown in the viewer's replay list (default: date, time and routes)")
    export.add_argument("--out", help="output .json path (default: visualization/replays/<recording>_<HHMM>-<HHMM>.json)")
    export.add_argument("--force", action="store_true", help="replace the output file if it already exists")
    export.add_argument("--forecast", action="store_true",
                        help="then add occupancy forecasts with the default settings of `forecast` (needs Timestral)")
    export.set_defaults(handler=run_export)

    forecast = commands.add_parser(
        "forecast", parents=[common], formatter_class=formatter,
        help="add occupancy forecasts to a replay (Timestral, or the location model)",
        description="Precompute occupancy forecasts with Timestral (a time-series model by Mistral AI) and store them in the\n"
                    "replay, for the chart shown when a bus is selected in the viewer (the demo exe runs no model).\n"
                    "Every --every-minutes of replay time, each bus with --min-history-minutes of occupancy history gets a\n"
                    "forecast of the next --horizon-minutes, made only from what was observed until then. With Timestral,\n"
                    "each forecast point is then calibrated on the earlier forecasts whose outcome is known (walk-forward):\n"
                    "how much to trust the model's predicted change, and how far to widen the 80% range so that each end\n"
                    "held 90% of past outcomes (so that it holds at least 80%).\n"
                    "The file is rewritten in place; an existing forecast is replaced.\n"
                    "Needs Timestral and PyTorch, best in their own environment (from the repo root, see the README):\n"
                    "  uv venv .venv-forecast --python 3.12\n"
                    "  uv pip install --python .venv-forecast -r software\\recording\\requirements-forecast.in\n"
                    "--model location uses the location model instead: a logistic regression that also knows where each\n"
                    "bus is (trip position, metro stations ahead, the bus ahead on its line), learned from the replay\n"
                    "itself as it goes (walk-forward, no change until it has enough outcomes). It needs numpy only, and\n"
                    "the stop lists of route-stops for stations and the bus ahead.",
        epilog="examples (from the software/ folder):\n"
               "  ..\\.venv-forecast\\Scripts\\python -m recording forecast ../visualization/replays/<replay>.json\n"
               "  python -m recording forecast ../visualization/replays/<replay>.json --model location")
    # Defaults: those of recording.forecast (not imported here: it needs numpy)
    forecast.add_argument("replay", metavar="REPLAY", help="the replay .json (rewritten in place)")
    forecast.add_argument("--model", default="28M", help="Timestral size: 5M, 28M, 123M or 209M (downloaded once: 21 MB "
                                                         "to 837 MB), a Hugging Face repo id or a model folder, or "
                                                         f"'{LOCATION_MODEL}' for the location model (a model folder "
                                                         f"named {LOCATION_MODEL}: .\\{LOCATION_MODEL}) "
                                                         "(default: %(default)s)")
    forecast.add_argument("--horizon-minutes", type=float, default=10,
                          help="how far ahead each forecast goes (default: %(default)g)")
    forecast.add_argument("--every-minutes", type=float, default=1,
                          help="replay time between two forecasts of a bus (default: %(default)g)")
    forecast.add_argument("--min-history-minutes", type=float, default=2,
                          help="occupancy history a bus needs before its first forecast (default: %(default)g)")
    forecast.add_argument("--no-calibration", action="store_true",
                          help="store Timestral's raw output (no walk-forward calibration)")
    forecast.add_argument("--offline", action="store_true",
                          help="only use a Timestral model already in the Hugging Face cache")
    forecast.add_argument("--device", default="cpu", help='PyTorch device (default: %(default)s); "cuda" needs a CUDA build '
                                                          'of PyTorch (on Windows, the requirements install the CPU-only one)')
    forecast.set_defaults(handler=run_forecast)

    route_stops = commands.add_parser(
        "route-stops", parents=[common], formatter_class=formatter, help="write the stops of every bus line for the viewer",
        description="Write the stops of every bus route and direction of a static GTFS, for the viewer to highlight the stops\n"
                    "of the selected bus's line. They are keyed by route and direction_id, which the replays give for each\n"
                    "bus (their trip ids change with every GTFS). A direction lists the stops of all its trips: those of its\n"
                    "most common stop sequence in order, then the stops served only by other trips (short turns, detours).\n"
                    "The GTFS is a folder with routes.txt, trips.txt, stops.txt and stop_times.txt or stop_times.zip (like\n"
                    "datasets/), or a GTFS .zip (like the STM's gtfs_stm.zip). Only bus routes (route_type 3) are kept.\n"
                    "The output file is replaced if it exists.",
        epilog=f"examples (from the software/ folder):\n  {PROGRAM} route-stops\n"
               f"  {PROGRAM} route-stops --gtfs ../gtfs_stm.zip")
    route_stops.add_argument("--gtfs", help="GTFS folder or .zip (default: datasets/ at the repo root)")
    route_stops.add_argument("--out", help="output .json path (default: visualization/assets/route_stops.json)")
    route_stops.set_defaults(handler=run_route_stops)
    return parser


def run_record(args) -> int:
    from recording.recorder import record, stm_fetcher

    routes = parse_routes(args.routes)
    if any(not route.isdigit() for route in routes):
        raise RecordingError(f'STM route ids are numbers, e.g. --routes "24,51,55,80,129,165" (got "{args.routes}").')
    if args.interval <= 0 or (args.minutes is not None and args.minutes <= 0):
        raise RecordingError("--interval and --minutes must be greater than 0.")

    fetch, url = stm_fetcher([int(route) for route in routes], timeout=min(10.0, args.interval))
    started = local_datetime(time.time(), TIMEZONE)
    name = args.name or f"stm_{started:%Y-%m-%d_%H%M}_" + (f"routes-{'-'.join(routes)}" if routes else "all-routes")
    out = Path(args.out) if args.out else RECORDINGS_DIR / f"{safe_file_name(name)}.sqlite"
    store = RecordingStore.create(out, {"source": "stm-api", "source_detail": url, "route_ids": routes,
                                        "poll_interval_s": args.interval, "name": name}, overwrite=args.force)

    duration = f"for {format_duration(args.minutes * 60)}" if args.minutes else "until you press Ctrl+C"
    print(f"Recording {'routes ' + ', '.join(routes) if routes else 'all routes'} every {args.interval:g} s {duration}")
    print(f"  -> {display_path(out)}")
    try:
        result = record(store, fetch, args.interval, args.minutes,
                        on_poll=lambda poll: print(progress_line(poll, args.minutes), flush=True))
    finally:
        store.close()

    if result.polls - result.errors == 0:
        if result.polls == 0:
            out.unlink(missing_ok=True)
            print("Nothing was recorded.")
        else:
            print(f"All {result.polls} polls failed; nothing to replay. The file is kept for `info`: {display_path(out)}")
        return 1

    status = "Stopped with Ctrl+C" if result.interrupted else "Done"
    print(f"\n{status}: {result.polls} polls in {format_duration(result.elapsed)} "
          f"({result.polls - result.errors} ok, {result.errors} failed), {len(result.vehicles)} distinct vehicles.")
    print(f"Saved to {display_path(out)}")
    if args.export:
        return run_export(build_parser().parse_args(["export", str(out)] + (["--force"] if args.force else [])))
    info_hint = f'`{program_name()} info "{display_path(out)}"` shows what was recorded'
    print_next_step("turn it into a replay for the 3D viewer", f'{program_name()} export "{display_path(out)}" --name "My recording"',
                    f"(add --minutes 60 to keep only the best hour; {info_hint})" if result.elapsed >= 3600 else f"({info_hint})")
    print("  Then `forecast` can add occupancy forecasts to the replay (optional, needs Timestral).")
    return 0


def run_import_csv(args) -> int:
    from recording.csv_import import read_csv_polls

    if args.interval <= 0 or args.cluster_gap < 0:
        raise RecordingError("--interval must be greater than 0 and --cluster-gap at least 0.")
    csv_path = Path(args.csv)
    data = read_csv_polls(csv_path, args.cluster_gap, args.interval)
    name = args.name or csv_path.stem
    out = Path(args.out) if args.out else RECORDINGS_DIR / f"{safe_file_name(name)}.sqlite"
    store = RecordingStore.create(out, {"source": "csv-import", "source_detail": repo_relative(csv_path),
                                        "route_ids": data.routes, "poll_interval_s": args.interval, "name": name},
                                  overwrite=args.force)
    try:
        for poll_time, entities in data.polls:
            store.add_poll(poll_time, entities, commit=False)
        store.commit()
        vehicles = store.vehicle_count()
    except BaseException:
        store.close()
        out.unlink(missing_ok=True)
        raise
    store.close()

    how = {"_id": "MongoDB _id insert times", "poll_time": "the poll_time column",
           "vehicle.timestamp": f"vehicle.timestamp in {args.interval:g}-s buckets"}[data.mode]
    print(f"Imported {data.rows:,} rows from {csv_path.name} -> {len(data.polls):,} polls (grouped by {how}), "
          f"{vehicles} vehicles, routes {', '.join(data.routes) or '-'}.")
    if data.duplicates:
        print(f"  {data.duplicates} duplicate position(s) of a vehicle within one poll were dropped (kept the newest).")
    print(f"Saved to {display_path(out)}")
    print_next_step("check it and find the best hour", f'{program_name()} info "{display_path(out)}"')
    return 0


def run_info(args) -> int:
    from recording.info import describe

    with RecordingStore.open(args.recording) as store:
        lines, best = describe(store, minutes=args.minutes)
        tz = store.meta["timezone"]
        ok = store.polls("ok")
    print("\n".join(lines))
    if best:
        print_next_step(f"export the best {args.minutes:g} minutes as a replay",
                        f'{program_name()} export "{display_path(args.recording)}" --minutes {args.minutes:g} --name "My replay"',
                        f"(or pick the time yourself: --start {start_option(best.start_time, tz, ok)} --minutes {args.minutes:g})")
    elif ok:
        print_next_step("export the whole recording as a replay",
                        f'{program_name()} export "{display_path(args.recording)}" --name "My replay"')
    return 0


def run_export(args) -> int:
    from recording.replay import build_replay, default_replay_file_name, write_replay

    with RecordingStore.open(args.recording) as store:
        meta = store.meta
        tz = meta["timezone"]
        polls = store.polls("ok")
        start = parse_local_time(args.start, tz, polls) if args.start and polls else None
        window, warnings = select_window(polls, meta["poll_interval_s"], tz, start, args.minutes)
        routes = parse_routes(args.routes) if args.routes else None
        if routes:
            recorded = [route for route in store.route_counts() if route]
            unknown = [route for route in routes if route not in recorded]
            if len(unknown) == len(routes):
                raise RecordingError(f"None of the routes {', '.join(routes)} are in this recording "
                                     f"(it has {', '.join(sort_routes(recorded))}).")
            if unknown:
                warnings.append(f"Route(s) {', '.join(unknown)} are not in this recording.")
        result = build_replay(store, window, args.name, routes)

    out = Path(args.out) if args.out else REPLAYS_DIR / default_replay_file_name(args.recording, window, tz)
    if out.suffix.lower() != ".json":
        raise RecordingError(f"--out must end with .json (the viewer only looks for *.json files): {out}")
    replaced = out.exists()
    if replaced and not args.force:
        raise RecordingError(f"{display_path(out)} already exists. Choose another --out, or add --force to replace it.")
    size = write_replay(result.replay, out)

    for warning in warnings:
        print(f"warning: {warning}")
    print(f'Exported "{result.replay["name"]}"')
    print(f"  window:   {format_span(window, tz)} ({format_duration(window.duration)}), "
          f"{result.frames} frames, {result.vehicles} vehicles, routes {', '.join(map(str, result.replay['routes']))}")
    print(f"  file:     {display_path(out)} ({size / 1e6:.1f} MB){' - replaced the previous file' if replaced else ''}")
    if args.forecast:
        return forecast_exported_replay(out)
    print("\nNext steps:")
    if out.resolve().parent == REPLAYS_DIR.resolve():
        print("  - It is in visualization/replays/, so the next demo build includes it: run .\\build_demo.bat at the repo root.")
    else:
        print("  - To bundle it in the demo, put it in visualization/replays/ and run .\\build_demo.bat at the repo root.")
    print("  - No rebuild needed: copy it into the replays folder next to \"STM DT Demo.exe\" in an unzipped demo;")
    print("    it shows up in the viewer's replay list.")
    print("  - Optional: add occupancy forecasts for the viewer's bus chart (with the forecast environment, see forecast --help):")
    print(f'      {forecast_program()} forecast "{display_path(out)}"')
    return 0


def forecast_exported_replay(out:Path) -> int:
    """export --forecast: the replay is already written; if the forecast fails, it stays without one (exit code 2)."""
    print()
    try:
        return run_forecast(build_parser().parse_args(["forecast", str(out.resolve())]))
    except RecordingError as error:
        print(f"error: {error}", file=sys.stderr)
        print(f"\nThe replay was written without a forecast: {display_path(out)}")
        if not isinstance(error, NothingToForecast):
            print_next_step("add the forecast later with the forecast environment's Python, once the error above is "
                            "fixed (see forecast --help)", f'{forecast_program()} forecast "{display_path(out)}"')
        return 2


def run_forecast(args) -> int:
    # numpy and Timestral are only needed here: the other commands work without them
    from recording.forecast import ForecastSettings, plan_forecast, read_replay

    if args.horizon_minutes <= 0 or args.every_minutes <= 0 or args.min_history_minutes < 0:
        raise RecordingError("--horizon-minutes and --every-minutes must be greater than 0, --min-history-minutes at least 0.")
    location = args.model.strip().lower() == LOCATION_MODEL
    if location and args.no_calibration:
        raise RecordingError("--no-calibration is for Timestral: the location model's forecast is not calibrated afterwards.")
    path = Path(args.replay)
    replay = read_replay(path)
    plan = plan_forecast(replay, ForecastSettings(horizon=args.horizon_minutes * 60, origin_every=args.every_minutes * 60,
                                                  min_history=args.min_history_minutes * 60))
    settings = plan.settings
    print(f'Forecasting the occupancy in "{replay.get("name", path.stem)}"')
    print(f"  plan:     {plan.vehicles} buses at {len(plan.origins)} times (every {_minutes_text(settings.origin_every)}, "
          f"after {_minutes_text(settings.min_history)} of history), {_minutes_text(settings.horizon)} ahead, "
          f"a point every {settings.point_every:g} s")
    return (run_location_forecast if location else run_timestral_forecast)(args, path, replay, plan)


def run_timestral_forecast(args, path:Path, replay:dict, plan) -> int:
    from recording.forecast import MODEL_SIZES, CalibrationSettings, compute_forecast, load_forecaster, model_name, resolve_model

    model_id = resolve_model(args.model)
    download = MODEL_SIZES.get(args.model.strip().upper())

    def loading():      # once Timestral is imported: a missing one is reported without this line
        print(f"Loading {model_name(model_id)} ({model_id}, {args.device}); the first run downloads it"
              f"{f' ({download})' if download else ''} to the Hugging Face cache...", flush=True)

    started = time.monotonic()
    forecaster = load_forecaster(args.model, args.device, args.offline, on_loading=loading)
    print(f"  loaded in {time.monotonic() - started:.1f} s", flush=True)

    started = time.monotonic()
    calibration = CalibrationSettings(enabled=not args.no_calibration)
    result = compute_forecast(plan, forecaster, on_progress=forecast_progress(replay, plan), calibration=calibration)
    elapsed = time.monotonic() - started
    raw = result.metrics_raw
    needs = f"{calibration.min_points} past outcomes from {calibration.min_buses} buses"
    if raw:
        details = [f"  calibrated: {_percent_text(result.forecast['calibration']['calibrated_share'])} of the stored points "
                   f"(a forecast point is calibrated once it has {needs})"]
    elif calibration.enabled:
        details = [f"  calibrated: none (no forecast point had {needs}): the raw model output is stored"]
    else:
        details = []
    if result.skipped:
        details.append(f"warning: {result.skipped} forecast(s) with NaN or infinite values were dropped.")
    # Scores of the stored forecast, of the raw model output when it was calibrated, and of "no change"
    columns = [("calibrated", result.metrics), (f"raw {forecaster.model_name}", raw)] if raw \
        else [(forecaster.model_name, result.metrics)]
    return save_forecast(path, replay, result, f"{forecaster.model_name} ({forecaster.model}), {elapsed:.0f} s", details,
                         columns)


def run_location_forecast(args, path:Path, replay:dict, plan) -> int:
    """forecast --model location: the location model, trained on the replay itself (recording.location_model)."""
    from recording.location_model import MODEL_NAME, LocationModelSettings, compute_location_forecast, load_route_stops

    route_stops = load_route_stops(ROUTE_STOPS_FILE)
    if route_stops is None:
        print(f"  note: no stop lists ({display_path(ROUTE_STOPS_FILE)}): metro stations and the bus ahead are unknown "
              f"(run route-stops)")
    settings = LocationModelSettings()
    print(f"Training the {MODEL_NAME.lower()} on the replay as it goes (walk-forward; numpy only, no Timestral)...",
          flush=True)
    started = time.monotonic()
    result = compute_location_forecast(plan, replay, route_stops, settings, on_progress=forecast_progress(replay, plan))
    elapsed = time.monotonic() - started

    training = result.forecast["training"]
    needs = (f"a forecast point needs {settings.min_points:,} past outcomes from {settings.min_buses} buses, "
             f"{settings.min_points_per_point} at its horizon and {settings.min_points_per_status} at the bus's status")
    if len(result.forecast["levels"]) < 2:
        details = ["  learned:  nothing - the replay has a single occupancy status: there is no change to learn"]
    elif training["final"] is None:
        details = [f"  learned:  nothing - every stored point is \"no change\" ({needs}): use a longer replay or more routes"]
    else:
        details = [f"  learned:  the model made {_percent_text(training['model_share'])} of the stored points, the others "
                   f"are \"no change\"\n            ({needs})"]
    details.append(f"  position: trip position known for {_percent_text(training['position_known'])} of the forecasts "
                   f"(direction and stop sequence),\n            stop on the line for "
                   f"{_percent_text(training['stop_known'])} (for metro stations and the bus ahead)")
    if training["position_known"] < 0.5:
        details.append("  note: most buses have no vehicle.trip.direction_id or vehicle.current_stop_sequence (e.g. a "
                       "replay imported\n        from a CSV): the model cannot use where they are on their trip")
    elif training["stop_known"] < 0.5 and route_stops is not None:
        details.append("  note: most buses have no vehicle.stop_id from their line's stop list: metro stations and the bus "
                       "ahead are\n        unknown (run route-stops again if the GTFS changed)")
    return save_forecast(path, replay, result, f"{MODEL_NAME} (logistic regression), {elapsed:.0f} s", details,
                         [(MODEL_NAME, result.metrics)])


def forecast_progress(replay:dict, plan):
    """on_progress of a forecast that starts now: a line every 10 forecast times, with the time spent so far."""
    started, width, tz = time.monotonic(), len(str(len(plan.origins))), replay.get("timezone", TIMEZONE)

    def progress(done:int, total:int, t:float, buses:int):
        if done % 10 == 0 or done == total:
            print(f"  [{done:>{width}}/{total}] {format_clock(replay['start_time'] + t, tz)}  {buses:>4} buses  "
                  f"{time.monotonic() - started:5.1f} s", flush=True)
    return progress


def save_forecast(path:Path, replay:dict, result, model:str, details:list[str], columns:list[tuple[str, dict]]) -> int:
    """Stores the forecast in the replay file, then prints what was stored, the model's own details, the scores of each
    (name, metrics) column and of "no change", and the next step."""
    from recording.forecast import set_forecast
    from recording.replay import write_replay

    previous = set_forecast(replay, result.forecast)
    size = write_replay(replay, path)
    replaced = f" - replaced the existing forecast (model {previous.get('model_name', '?')})" if isinstance(previous, dict) else ""
    print(f"\nSaved the forecast in {display_path(path)} ({size / 1e6:.1f} MB){replaced}")
    print(f"  model:    {model} for {result.origins} forecast times")
    print(f"  stored:   {result.vehicles} buses, {result.forecasts:,} forecasts, {result.points:,} points")
    for line in details:
        print(line)

    metrics = result.metrics
    ahead = f"  at +{_minutes_text(result.forecast['horizon'])}"
    print(f"\n  {'':<18}" + "".join(f"{name:>22}" for name, _ in columns) + f"{chr(34) + 'no change' + chr(34):>14}")
    for label, key, baseline, text in (("right", "accuracy", "accuracy_last_value", _accuracy_text),
                                       (ahead, "accuracy_at_horizon", "accuracy_last_value_at_horizon", _accuracy_text),
                                       ("error (steps)", "mae", "mae_last_value", _level_text),
                                       (ahead, "mae_at_horizon", "mae_last_value_at_horizon", _level_text),
                                       ("80% range holds", "coverage", "accuracy_last_value", _percent_text)):
        print(f"  {label:<18}" + "".join(f"{text(values[key]):>22}" for _, values in columns)
              + f"{text(metrics[baseline]):>14}")
    scale = ", ".join(OCCUPANCY_LABELS.get(status, str(status)) for status in result.forecast["levels"])
    print(f"\n  On {metrics['evaluated_points']:,} points with an observed status. The scale is the statuses found, one step "
          f"apart:\n  {scale}.\n  \"Right\": the median rounded to a status is the observed one. \"No change\" repeats the "
          f"last observed status\n  (its 80% range would have zero width): occupancy changes rarely, so it is hard to beat.")
    if path.resolve().parent == REPLAYS_DIR.resolve():
        print_next_step("rebuild the demo so that it includes the forecast", ".\\build_demo.bat   (at the repo root)",
                        '(or, without a rebuild, copy the file into the replays folder next to "STM DT Demo.exe" in an unzipped demo)')
    else:
        print_next_step("see it in the viewer (no rebuild needed)",
                        'copy the file into the replays folder next to "STM DT Demo.exe" in an unzipped demo',
                        "(or put it in visualization\\replays\\ and run .\\build_demo.bat at the repo root to bundle it)")
    return 0


def run_route_stops(args) -> int:
    from recording.route_stops import build_route_stops, write_route_stops

    gtfs = Path(args.gtfs) if args.gtfs else DATASETS_DIR
    out = Path(args.out) if args.out else ROUTE_STOPS_FILE
    if out.suffix.lower() != ".json":
        raise RecordingError(f"--out must end with .json: {out}")
    print(f"Reading the bus routes of {display_path(gtfs)}")
    started = time.monotonic()

    def progress(rows:int, fraction:float | None):
        done = "    " if fraction is None else f"{100 * fraction:3.0f}%"
        print(f"  [{done}] {rows:>11,} stop times  {time.monotonic() - started:5.1f} s", flush=True)

    result = build_route_stops(gtfs, on_progress=progress)
    replaced = out.exists()
    size = write_route_stops(result.data, out)

    print(f"\nSaved the stops of {result.routes} bus routes ({result.directions} directions) in {display_path(out)} "
          f"({size / 1e6:.1f} MB){' - replaced the previous file' if replaced else ''}")
    print(f"  read:     {result.trips:,} bus trips ({result.patterns:,} distinct stop patterns), "
          f"{result.stop_times:,} stop times in {time.monotonic() - started:.0f} s")
    print(f"  stops:    {result.stops:,}")
    if out.resolve() == ROUTE_STOPS_FILE.resolve():
        print_next_step("rebuild the demo so that it includes them", ".\\build_demo.bat   (at the repo root)")
    else:
        print_next_step("let the viewer use them", f"copy the file to {display_path(ROUTE_STOPS_FILE)}",
                        "(then run .\\build_demo.bat at the repo root to bundle it in the demo)")
    return 0


def parse_routes(text:str) -> list[str]:
    """'121, 45 140' -> ['121', '45', '140']; '' or 'all' -> [] (all routes)."""
    routes = [route for route in re.split(r"[,;\s]+", text or "") if route]
    if [route.lower() for route in routes] == ["all"]:
        return []
    return list(dict.fromkeys(routes))


def safe_file_name(name:str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "recording"


def progress_line(poll, minutes:float=None) -> str:
    elapsed = _clock_text(poll.elapsed) + (f"/{_clock_text(minutes * 60)}" if minutes else "")
    head = f"[{poll.number:>4}] {format_clock(poll.poll_time, TIMEZONE, seconds=True)}  {elapsed:>11}"
    if poll.error:
        return f"{head}  FAILED: {poll.error} - recording continues"
    routes = "  ".join(f"{route}:{poll.route_counts[route]}" for route in sort_routes(poll.route_counts))
    return f"{head}  {poll.vehicle_count:>4} vehicles  {routes}"


def program_name() -> str:
    """How to start this tool from the current folder: `python -m recording` in software/, else via the folder path."""
    package = Path(__file__).resolve().parent
    if Path.cwd().resolve() == package.parent:
        return PROGRAM
    path = display_path(package)
    return f'python "{path}"' if " " in path else f"python {path}"


def forecast_program() -> str:
    """program_name() run by the forecast environment's Python (.venv-forecast at the repo root, see the README)."""
    python = display_path(ROOT / ".venv-forecast" / "Scripts" / "python")
    return (f'"{python}"' if " " in python else python) + program_name().removeprefix("python")


def print_next_step(what:str, command:str, note:str=None):
    print(f"\nNext step - {what}:")
    print(f"  {command}")
    if note:
        print(f"  {note}")


def _minutes_text(seconds:float) -> str:
    """600 -> '10 min', 90 -> '1.5 min', 30 -> '30 s'"""
    return f"{seconds / 60:g} min" if seconds >= 60 else f"{seconds:g} s"


def _level_text(value:float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _percent_text(value:float | None, decimals:int=0) -> str:
    """0.725 -> '73%': half up, as the viewer rounds (Python's format rounds half to even: '72%')."""
    if value is None:
        return "n/a"
    scale = 10 ** decimals
    return f"{math.floor(100 * scale * value + 0.5) / scale:.{decimals}f}%"


def _accuracy_text(value:float | None) -> str:
    """With one decimal: 74.0% and 74.3% must not both read 74%."""
    return _percent_text(value, 1)


def _clock_text(seconds:float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"
