#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["python-roborock", "platformdirs", "keyring"]
# ///
"""Control a Roborock vacuum.

Usage:
    ./roborock.py                        Start the server, logging in with an emailed code if needed.
                                         Do this first, in its own terminal
    ./roborock.py nobumperstuck          Resume each time it reports "bumper stuck"
    ./roborock.py find                   Say "I'm over here"
    ./roborock.py start                  Start cleaning
    ./roborock.py pause                  Pause cleaning
    ./roborock.py resume                 Continue a paused clean
    ./roborock.py stop                   Stop cleaning
    ./roborock.py charge                 Go back to the dock
    ./roborock.py <command>              Send any other command below
"""
import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

# This file is named roborock.py, so its folder would shadow the roborock library
sys.path = [p for p in sys.path if p != str(Path(__file__).resolve().parent)]

import keyring
from platformdirs import user_runtime_dir
from roborock.data import UserData
from roborock.data.v1.v1_code_mappings import RoborockErrorCode, RoborockStateCode
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.devices.traits.v1.consumeable import ConsumableAttribute
from roborock.roborock_typing import RoborockCommand
from roborock.web_api import RoborockApiClient

SOCKET_FILE = Path(user_runtime_dir("roborock")) / "server.sock"


class Arg(NamedTuple):
    name: str
    parse: Callable[[str], object]
    help: str
    default: object = None  # makes the argument optional


class Cmd(NamedTuple):
    verb: str
    command: str  # what the robot calls it
    text: str
    args: tuple[Arg, ...] = ()
    keys: tuple[str, ...] = ()  # send the arguments as a dict with these keys
    boxed: bool = False  # wrap the dict in a list
    fixed: object = None  # send this instead of arguments


def on_off(text):
    if text.lower() not in ("on", "off"):
        raise argparse.ArgumentTypeError("use on or off")
    return int(text.lower() == "on")


def clock(text):
    try:
        return [int(part) for part in time.strptime(text, "%H:%M")[3:5]]
    except ValueError:
        raise argparse.ArgumentTypeError("use HH:MM, like 22:30") from None


def numbers(text):
    try:
        return [int(part) for part in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("use numbers separated by commas, like 16,17") from None


def percent(text):
    if not text.isdigit() or int(text) > 100:
        raise argparse.ArgumentTypeError("use a number from 0 to 100")
    return int(text)


def choice(*options):
    def parse(text):
        if text not in options:
            raise argparse.ArgumentTypeError(f"use one of: {', '.join(options)}")
        return text

    return parse


STATE = Arg("on|off", on_off, "Turn it on or off")
FROM = Arg("from", clock, "Start time, like 22:00")
UNTIL = Arg("until", clock, "End time, like 08:00")

AREAS = {
    "clean": ("Cleaning", [
        Cmd("rooms", "app_segment_clean", "Clean chosen rooms", (
            Arg("rooms", numbers, "Room numbers separated by commas, like 1,2. See: rooms list"),
            Arg("times", int, "How many passes per room", 1),
        ), ("segments", "repeat"), True),
        Cmd("spot", "app_spot", "Clean a small area around the robot"),
        Cmd("estimate", "app_get_clean_estimate_info", "Show the estimated time and area of this clean"),
    ]),
    "rooms": ("Rooms", [
        Cmd("list", "get_room_mapping", "List room numbers (the first number of each row)"),
        Cmd("order", "get_clean_sequence", "Show the order rooms are cleaned in"),
        Cmd("progress", "get_segment_status", "Show room cleaning progress"),
        Cmd("custom", "get_customize_clean_mode", "Show whether rooms use their own settings"),
    ]),
    "maps": ("Floor maps", [
        Cmd("list", "get_multi_maps_list", "List the saved floor maps"),
        Cmd("load", "load_multi_map", "Switch to another saved floor map", (
            Arg("map", int, "Map number, listed by: maps list"),
        )),
        Cmd("status", "get_map_status", "Show the current map state"),
        Cmd("tidy", "get_map_beautification_status", "Show whether map cleanup is on"),
        Cmd("offline", "get_offline_map_status", "Show whether offline maps are on"),
    ]),
    "suction": ("Suction power", [
        Cmd("show", "get_custom_mode", "Show the suction power"),
    ]),
    "water": ("Water flow", [
        Cmd("show", "get_water_box_custom_mode", "Show the water flow level"),
    ]),
    "mode": ("Cleaning mode", [
        Cmd("show", "get_clean_motor_mode", "Show suction, water and mop route together"),
    ]),
    "carpet": ("Carpets", [
        Cmd("boost", "get_carpet_mode", "Show the carpet boost setting"),
        Cmd("mode", "get_carpet_clean_mode", "Show how carpets are cleaned"),
        Cmd("deep", "app_get_carpet_deep_clean_status", "Show whether carpet deep clean is on"),
    ]),
    "obstacles": ("Obstacles", [
        Cmd("avoidance", "get_collision_avoid_status", "Show the obstacle avoidance setting"),
        Cmd("dirt", "get_dirty_object_detect_status", "Show whether dirty object detection is on"),
        Cmd("furniture", "get_identify_furniture_status", "Show whether furniture detection is on"),
        Cmd("gaps", "get_gap_deep_clean_status", "Show whether gap deep clean is on"),
        Cmd("pets", "get_pet_supplies_deep_clean_status", "Show whether pet supplies deep clean is on"),
    ]),
    "floors": ("Floor types", [
        Cmd("detect", "get_identify_ground_material_status", "Show whether floor type detection is on"),
        Cmd("adapt", "get_clean_follow_ground_material_status", "Show whether cleaning adapts to the floor type"),
    ]),
    "washing": ("Mop washing", [
        Cmd("start", "app_start_wash", "Wash the mop at the dock"),
        Cmd("stop", "app_stop_wash", "Stop washing the mop"),
        Cmd("mode", "get_wash_towel_mode", "Show the mop wash mode"),
        Cmd("smart", "get_smart_wash_params", "Show the smart mop wash settings"),
        Cmd("temperature", "get_wash_water_temperature", "Show the mop wash water temperature"),
        Cmd("fluid", "get_auto_delivery_cleaning_fluid", "Show the auto cleaning fluid setting"),
    ]),
    "drying": ("Mop drying", [
        Cmd("start", "app_set_dryer_status", "Dry the mop now", keys=("status",), fixed={"status": 1}),
        Cmd("stop", "app_set_dryer_status", "Stop drying the mop", keys=("status",), fixed={"status": 0}),
        Cmd("show", "app_get_dryer_setting", "Show whether the dock dries the mop after a clean"),
        Cmd("set", "app_set_dryer_setting", "Dry the mop after every clean, or not", (STATE,), ("status",)),
    ]),
    "dust": ("Dust emptying", [
        Cmd("start", "app_start_collect_dust", "Empty the bin into the dock"),
        Cmd("stop", "app_stop_collect_dust", "Stop emptying the bin"),
        Cmd("mode", "get_dust_collection_mode", "Show how often the dock empties the bin"),
        Cmd("auto", "get_dust_collection_switch_status", "Show whether auto-empty is on"),
    ]),
    "status": ("Robot status", [
        Cmd("show", "get_status", "Show the robot's state, battery, errors and settings"),
        Cmd("serial", "get_serial_number", "Show the serial number"),
        Cmd("features", "get_fw_features", "Show the firmware feature list"),
        Cmd("support", "app_get_init_status", "Show what the robot supports"),
    ]),
    "errors": ("Errors", [
        Cmd("resolve", "resolve_error", "Clear an error, like tapping Resolved in the app", (
            Arg("code", int, "Error code, shown by: status show"),
        ), ("error_code",)),
    ]),
    "history": ("Cleaning history", [
        Cmd("list", "get_clean_summary", "Show total cleaning time, area, count and record ids"),
        Cmd("show", "get_clean_record", "Show one past clean", (
            Arg("id", int, "Record id, listed by: history list"),
        )),
    ]),
    "parts": ("Brushes and filters", [
        Cmd("show", "get_consumable", "Show how worn the brushes and filter are"),
        Cmd("reset", "reset_consumable", "Reset a wear counter after replacing a part", (
            Arg("part", choice(*(part.value for part in ConsumableAttribute)), "The part you replaced"),
        )),
    ]),
    "brushes": ("Side brush", [
        Cmd("extend", "get_right_brush_stretch_status", "Show whether the side brush extends"),
        Cmd("tag", "get_stretch_tag_status", "Show the stretch tag setting"),
    ]),
    "volume": ("Voice volume", [
        Cmd("show", "get_sound_volume", "Show the voice volume"),
        Cmd("set", "change_sound_volume", "Set the voice volume", (Arg("volume", percent, "0 to 100"),)),
    ]),
    "voice": ("Voice pack", [
        Cmd("show", "get_current_sound", "Show the voice pack in use"),
        Cmd("progress", "get_sound_progress", "Show the voice pack download progress"),
    ]),
    "lights": ("Lights", [
        Cmd("show", "get_led_status", "Show whether the status light is on"),
        Cmd("set", "set_led_status", "Turn the status light on or off", (STATE,)),
        Cmd("mic", "get_ap_mic_led_status", "Show whether the microphone light is on"),
    ]),
    "locks": ("Child lock", [
        Cmd("show", "get_child_lock_status", "Show whether the child lock is on"),
        Cmd("set", "set_child_lock_status", "Turn the child lock on or off", (STATE,), ("lock_status",)),
    ]),
    "quiet": ("Quiet hours", [
        Cmd("show", "get_dnd_timer", "Show the hours the robot stays silent"),
        Cmd("set", "set_dnd_timer", "Set the hours the robot stays silent", (FROM, UNTIL)),
        Cmd("off", "close_dnd_timer", "Turn quiet hours off"),
    ]),
    "offpeak": ("Off-peak charging", [
        Cmd("show", "get_valley_electricity_timer", "Show the off-peak charging hours"),
        Cmd("set", "set_valley_electricity_timer", "Charge only during these hours", (FROM, UNTIL)),
        Cmd("off", "close_valley_electricity_timer", "Charge at any hour"),
    ]),
    "schedules": ("Cleaning schedules", [
        Cmd("list", "get_timer", "Show the cleaning schedules"),
        Cmd("summary", "get_timer_summary", "Show a short list of the cleaning schedules"),
        Cmd("cloud", "get_server_timer", "Show the schedules saved in the cloud"),
    ]),
    "network": ("Wi-Fi", [
        Cmd("show", "get_network_info", "Show the Wi-Fi name, signal and IP address"),
        Cmd("scan", "app_get_wifi_list", "List the Wi-Fi networks the robot sees"),
    ]),
    "timezone": ("Timezone", [
        Cmd("show", "get_timezone", "Show the timezone"),
    ]),
    "locale": ("Language and region", [
        Cmd("show", "app_get_locale", "Show the language and region"),
    ]),
    "camera": ("Camera", [
        Cmd("show", "get_camera_status", "Show whether the camera is on"),
    ]),
}

ALIASES = {
    "find": ("find_me", "Say \"I'm over here\""),
    "start": ("app_start", "Start cleaning"),
    "pause": ("app_pause", "Pause cleaning"),
    "stop": ("app_stop", "Stop cleaning"),
    "charge": ("app_charge", "Go back to the dock"),
}

# Plain app_start would restart a room or zone clean from scratch
RESUME_COMMANDS = {
    2: RoborockCommand.RESUME_ZONED_CLEAN,
    3: RoborockCommand.RESUME_SEGMENT_CLEAN,
}


async def login(email):
    client = RoborockApiClient(email)
    await client.request_code()
    user_data = await client.code_login(input("Code from email: ").strip())
    keyring.set_password("roborock", "session", json.dumps({"email": email, "user_data": user_data.as_dict()}))
    print("Logged in.")


async def resume(status=None):
    if status is None:
        [status] = await request(RoborockCommand.GET_STATUS, None)
    mode = int(status["in_cleaning"] or 0)
    await request(RESUME_COMMANDS.get(mode, RoborockCommand.APP_START), None)


async def resume_if_stuck():
    [status] = await request(RoborockCommand.GET_STATUS, None)
    if status["error_code"] != RoborockErrorCode.bumper_stuck:
        return
    logging.warning("Bumper stuck, continuing")
    await request(RoborockCommand.RESOLVE_ERROR, {"error_code": status["error_code"]})
    if status["state"] in (RoborockStateCode.paused, RoborockStateCode.error):
        await resume(status)


@contextlib.asynccontextmanager
async def connect():
    saved = json.loads(keyring.get_password("roborock", "session"))
    params = UserParams(saved["email"], UserData.from_dict(saved["user_data"]))
    manager = await create_device_manager(params)
    try:
        yield next(d for d in await manager.get_devices() if d.v1_properties)
    finally:
        await manager.close()


async def run():
    logging.info("Watching for bumper stuck.")
    while True:
        try:
            await resume_if_stuck()
        except RuntimeError:
            logging.exception("Failed, retrying in 1s")
        await asyncio.sleep(1)


def params(cmd, values):
    if cmd.fixed is not None:
        return cmd.fixed
    if cmd.keys:
        fields = dict(zip(cmd.keys, values))
        return [fields] if cmd.boxed else fields
    return [part for value in values for part in (value if isinstance(value, list) else [value])] or None


def area_lines(area):
    bold, plain = ("\033[1m", "\033[0m") if sys.stdout.isatty() else ("", "")
    title, cmds = AREAS[area]
    lines = [f"\n  {bold}{title}{plain}"]
    for cmd in cmds:
        usage = " ".join([area, cmd.verb, *(f"<{a.name}>" if a.default is None else f"[{a.name}]" for a in cmd.args)])
        lines.append(f"    {usage:<44} {cmd.text}")
    return lines


def help_text():
    return "\n".join([__doc__, "Other commands:", *(line for area in AREAS for line in area_lines(area))])


class Parser(argparse.ArgumentParser):
    def error(self, message):
        if "invalid choice" in message:
            message = f"unknown command {message.split(chr(39))[1]!r}. Run ./roborock.py -h to list them"
        super().error(message)


def build_parser():
    parser = Parser(prog="./roborock.py", add_help=False)
    areas = parser.add_subparsers(dest="area", metavar="<command>", parser_class=Parser)
    areas.add_parser("nobumperstuck", description='Resume each time it reports "bumper stuck"')
    areas.add_parser("resume", description="Continue a paused clean")
    for name, (_, text) in ALIASES.items():
        areas.add_parser(name, description=text)
    for area, (title, cmds) in AREAS.items():
        verbs = areas.add_parser(area, prog=f"./roborock.py {area}", description=title).add_subparsers(
            dest="verb", metavar="<command>", parser_class=Parser)
        for cmd in cmds:
            sub = verbs.add_parser(cmd.verb, prog=f"./roborock.py {area} {cmd.verb}", description=cmd.text)
            for arg in cmd.args:
                optional = {"nargs": "?", "default": arg.default} if arg.default is not None else {}
                sub.add_argument(arg.name, type=arg.parse, help=arg.help, metavar=arg.name, **optional)
    return parser


async def serve():
    try:
        _, writer = await asyncio.open_unix_connection(SOCKET_FILE)
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    else:
        writer.close()
        sys.exit("A server is already running. Run ./roborock.py -h to see the commands.")
    if keyring.get_password("roborock", "session") is None:
        await login(input("Roborock email: ").strip())
    async with connect() as vacuum:
        command = vacuum.v1_properties.command

        async def handle(reader, writer):
            try:
                request = json.loads(await reader.readline())
                result = await command.send(RoborockCommand(request["command"]), request["params"])
                reply = {"result": result}
            except Exception as err:
                reply = {"error": f"{type(err).__name__}: {err}"}
            with contextlib.suppress(ConnectionError):  # the client left before the reply
                writer.write(json.dumps(reply, default=str).encode() + b"\n")
                await writer.drain()
            writer.close()

        SOCKET_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        SOCKET_FILE.unlink(missing_ok=True)
        previous = os.umask(0o177)  # only this user may connect
        try:
            server = await asyncio.start_unix_server(handle, SOCKET_FILE)
        finally:
            os.umask(previous)
        print(
            f"Serving {vacuum.name}. Leave this running.\n"
            "In another terminal, run ./roborock.py <command>, like ./roborock.py find.\n"
            "Commands find this server on their own. ./roborock.py -h lists them."
        )
        try:
            async with server:
                await server.serve_forever()
        finally:
            SOCKET_FILE.unlink(missing_ok=True)


async def request(command, params):
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET_FILE)
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit("No server is running. Start one in another terminal with ./roborock.py, then try again.")
    writer.write(json.dumps({"command": command.value, "params": params}).encode() + b"\n")
    reply = json.loads(await reader.readline())
    writer.close()
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply["result"]


async def send(command, params):
    try:
        print(await request(command, params))
    except RuntimeError as err:
        sys.exit(str(err))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if sys.argv[1:] == []:
        asyncio.run(serve())
        return
    if sys.argv[1:] in (["-h"], ["--help"]):
        print(help_text())
        return
    args = build_parser().parse_args()
    if args.area in AREAS and not args.verb:
        print("\n".join(area_lines(args.area)))
        return
    try:
        if args.area == "nobumperstuck":
            asyncio.run(run())
        elif args.area == "resume":
            asyncio.run(resume())
        elif args.area in ALIASES:
            asyncio.run(send(RoborockCommand(ALIASES[args.area][0]), None))
        else:
            cmd = next(c for c in AREAS[args.area][1] if c.verb == args.verb)
            values = [getattr(args, arg.name) for arg in cmd.args]
            asyncio.run(send(RoborockCommand(cmd.command), params(cmd, values)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
