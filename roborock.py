#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["python-roborock", "keyring", "platformdirs"]
# ///
"""Control a Roborock vacuum.

Usage:
    ./roborock.py                        Start the server. Do this first, in its own terminal
    ./roborock.py <command>              Send a command to the server, listed below
    ./roborock.py -v ...                 Also show the log, which is kept in a file otherwise
"""

import argparse
import asyncio
import contextlib
import hmac
import inspect
import itertools
import json
import logging
import logging.handlers
import os
import plistlib
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

# This file is named roborock.py, so its folder would shadow the roborock library
sys.path = [p for p in sys.path if p != str(Path(__file__).resolve().parent)]

import keyring
from platformdirs import user_config_dir, user_log_dir
from roborock.data import UserData
from roborock.data.v1.v1_code_mappings import RoborockErrorCode, RoborockStateCode
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.devices.traits.v1.consumeable import ConsumableAttribute
from roborock.roborock_typing import RoborockCommand
from roborock.web_api import RoborockApiClient

CONFIG_FILE = Path(user_config_dir("roborock")) / "config.json"
SETTINGS = {"bumper": 'Resume the clean when the robot reports "bumper stuck"'}  # all on by default
SERVICE = "io.github.inancgumus.roborock"
HOST, PORT = (
    "127.0.0.1",
    int(os.environ.get("ROBORROCK_PORT", 47651)),
)  # the server only listens on this computer
API_URL = os.environ.get("ROBORROCK_API_URL")  # replaces Roborock's own servers, for testing


# Plain app_start would restart a room or zone clean from scratch
RESUME_COMMANDS = {
    2: RoborockCommand.RESUME_ZONED_CLEAN,
    3: RoborockCommand.RESUME_SEGMENT_CLEAN,
}


def bold(text):
    return f"\033[1m{text}\033[0m" if sys.stdout.isatty() else text


def error(text):
    red = sys.stderr.isatty()
    print(f"\033[1;31m{text}\033[0m" if red else text, file=sys.stderr, flush=True)


def fail(text):
    error(text)
    sys.exit(1)


def say(text):
    print(bold(text), flush=True)


@contextlib.asynccontextmanager
async def sweeping(text):
    if not sys.stdout.isatty():
        say(f"{text}...")
        yield
        return

    async def animate():
        width = 8
        for step in itertools.count():
            sweep = step % width
            print(
                f"\r\033[K{'  ' * sweep}🧹{'· ' * (width - sweep - 1)} {bold(text)}",
                end="",
                flush=True,
            )
            await asyncio.sleep(0.15)

    task = asyncio.create_task(animate())
    try:
        yield
    finally:
        task.cancel()
        print("\r\033[K", end="", flush=True)


def setup_logging(verbose):
    log_file = Path(user_log_dir("roborock")) / "roborock.log"
    log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handlers = [logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3)]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, handlers=handlers, format="%(asctime)s %(name)s %(message)s"
    )


async def login(email):
    client = RoborockApiClient(email, base_url=API_URL)
    await client.request_code()
    user_data = await client.code_login(input(bold("Code from email: ")).strip())
    keyring.set_password(
        "roborock", "session", json.dumps({"email": email, "user_data": user_data.as_dict()})
    )
    say("Logged in.")


async def resume(call=None, status=None):
    call = call or request  # the server passes its own connection
    if status is None:
        [status] = await call(RoborockCommand.GET_STATUS, None)
    mode = int(status["in_cleaning"] or 0)
    await call(RESUME_COMMANDS.get(mode, RoborockCommand.APP_START), None)


async def resume_if_stuck(call):
    [status] = await call(RoborockCommand.GET_STATUS, None)
    if status["error_code"] != RoborockErrorCode.bumper_stuck:
        return
    await call(RoborockCommand.RESOLVE_ERROR, {"error_code": status["error_code"]})
    if status["state"] in (RoborockStateCode.paused, RoborockStateCode.error):
        await resume(call, status)
    say(f"{time.strftime('%H:%M:%S')} Bumper stuck. Resumed the clean.")


def load_config():
    try:
        saved = json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        saved = {}
    return {name: saved.get(name, True) for name in SETTINGS}


async def watch(call):
    failing = False
    while True:
        try:
            if load_config()["bumper"]:
                await resume_if_stuck(call)
            failing = False
        except Exception:
            logging.exception("Could not check the robot")
            if not failing:
                error("Could not check the robot. Still trying. The log has the details.")
            failing = True
        await asyncio.sleep(1)


@contextlib.asynccontextmanager
async def connect():
    saved = json.loads(keyring.get_password("roborock", "session"))
    params = UserParams(saved["email"], UserData.from_dict(saved["user_data"]), base_url=API_URL)
    async with sweeping("Connecting to your account"):
        manager = await create_device_manager(params)
    try:
        async with sweeping("Looking for your vacuum"):
            vacuum = next(d for d in await manager.get_devices() if d.v1_properties)
        yield vacuum
    finally:
        await manager.close()


def params(cmd, values):
    if cmd.fixed is not None:
        return cmd.fixed
    if cmd.keys:
        fields = dict(zip(cmd.keys, values))
        return [fields] if cmd.boxed else fields
    return [
        part for value in values for part in (value if isinstance(value, list) else [value])
    ] or None


def sections():
    return [("", TOP), *AREAS.items()]


def usage(prefix, cmd):
    needs = (f"<{a.name}>" if a.default is None else f"[{a.name}]" for a in cmd.args)
    return " ".join(filter(None, [prefix, cmd.name, *needs]))


def section_lines(prefix, section):
    title, cmds = section
    width = max(len(usage(p, c)) for p, (_, cs) in sections() for c in cs)
    return [f"  {bold(title)}", *(f"    {usage(prefix, c):<{width}}  {c.text}" for c in cmds)]


def help_text():
    blocks = ("\n".join(section_lines(prefix, section)) for prefix, section in sections())
    return "\n\n".join([__doc__.rstrip(), "Commands:", *blocks])


class Parser(argparse.ArgumentParser):
    def error(self, message):
        if "invalid choice" in message:
            message = (
                f"unknown command {message.split(chr(39))[1]!r}. Run ./roborock.py -h to list them"
            )
        self.print_usage(sys.stderr)
        error(f"{self.prog}: error: {message}")
        sys.exit(2)


def build_parser():
    parser = Parser(prog="./roborock.py", add_help=False)
    top = parser.add_subparsers(dest="area", metavar="<command>", parser_class=Parser)
    for prefix, (title, cmds) in sections():
        parents = top
        if prefix:
            parents = top.add_parser(
                prefix, prog=f"./roborock.py {prefix}", description=title
            ).add_subparsers(dest="verb", metavar="<command>", parser_class=Parser)
        for cmd in cmds:
            prog = " ".join(filter(None, ["./roborock.py", prefix, cmd.name]))
            sub = parents.add_parser(cmd.name, prog=prog, description=cmd.text)
            for arg in cmd.args:
                optional = {"nargs": "?", "default": arg.default} if arg.default is not None else {}
                sub.add_argument(
                    arg.name, type=arg.parse, help=arg.help, metavar=arg.name, **optional
                )
    return parser


async def serve():
    try:
        _, writer = await asyncio.open_connection(HOST, PORT)
    except ConnectionRefusedError:
        pass
    else:
        writer.close()
        print(help_text())
        return
    if keyring.get_password("roborock", "session") is None:
        await login(input(bold("Roborock email: ")).strip())
    async with connect() as vacuum:
        command = vacuum.v1_properties.command
        secret = secrets.token_hex(
            16
        )  # keeps other programs on this computer from sending commands

        async def handle(reader, writer):
            try:
                request = json.loads(await reader.readline())
                if not hmac.compare_digest(request["secret"], secret):
                    raise PermissionError("wrong secret")
                result = await command.send(RoborockCommand(request["command"]), request["params"])
                reply = {"result": result}
                logging.info("Ran %s", request["command"])
            except Exception as err:
                logging.warning("Command failed: %s", err)
                reply = {"error": f"{type(err).__name__}: {err}"}
            with contextlib.suppress(ConnectionError):  # the client left before the reply
                writer.write(json.dumps(reply, default=str).encode() + b"\n")
                await writer.drain()
            writer.close()

        try:
            server = await asyncio.start_server(handle, HOST, PORT)
        except OSError as err:
            fail(f"Cannot listen on port {PORT}: {err}")
        keyring.set_password("roborock", "server", secret)
        say(f"🎉 Ready! Serving {vacuum.name}. Leave this running.")
        say("In another terminal, run ./roborock.py <command>, like ./roborock.py find.")
        if load_config()["bumper"]:
            say('It resumes the clean on its own when the robot reports "bumper stuck".')
        watcher = asyncio.create_task(watch(command.send))
        try:
            async with server:
                await server.serve_forever()
        finally:
            watcher.cancel()


def service_files():
    command = [
        shutil.which("uv") or fail("Cannot find uv on your PATH."),
        "run",
        "--script",
        str(Path(__file__).resolve()),
    ]
    if sys.platform == "darwin":
        return command, Path.home() / "Library/LaunchAgents" / f"{SERVICE}.plist"
    return command, Path.home() / ".config/systemd/user/roborock.service"


def install():
    if keyring.get_password("roborock", "session") is None:
        fail("Log in first. Run ./roborock.py once, then run install again.")
    command, file = service_files()
    log = Path(user_log_dir("roborock")) / "service.log"
    if sys.platform == "win32":
        subprocess.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                "roborock",
                "/SC",
                "ONLOGON",
                "/F",
                "/TR",
                subprocess.list2cmdline(command),
            ],
            check=True,
        )
        subprocess.run(["schtasks", "/Run", "/TN", "roborock"], check=True)
    elif sys.platform == "darwin":
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(
            plistlib.dumps(
                {
                    "Label": SERVICE,
                    "ProgramArguments": command,
                    "RunAtLoad": True,
                    "KeepAlive": {
                        "SuccessfulExit": False
                    },  # a second server exits cleanly and stays stopped
                    "StandardOutPath": str(log),
                    "StandardErrorPath": str(log),
                }
            )
        )
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE}"], capture_output=True
        )
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(file)], check=True)
    else:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            f"[Unit]\nDescription=Roborock server\n\n[Service]\nExecStart={shlex.join(command)}\n"
            "Restart=on-failure\n\n[Install]\nWantedBy=default.target\n"
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "roborock.service"], check=True)
    say("Installed. The server starts when you log in, and it is starting now.")


def uninstall():
    _, file = service_files()
    if sys.platform == "win32":
        subprocess.run(["schtasks", "/Delete", "/TN", "roborock", "/F"], check=True)
    elif sys.platform == "darwin":
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE}"], capture_output=True
        )
        file.unlink(missing_ok=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", "roborock.service"], check=True)
        file.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    say("Uninstalled. The server no longer starts when you log in.")


async def request(command, params):
    secret = keyring.get_password("roborock", "server")
    try:
        if secret is None:
            raise ConnectionRefusedError
        reader, writer = await asyncio.open_connection(HOST, PORT)
    except ConnectionRefusedError:
        fail(
            "No server is running. Start one in another terminal with ./roborock.py, then try again."
        )
    writer.write(
        json.dumps({"secret": secret, "command": command.value, "params": params}).encode() + b"\n"
    )
    reply = json.loads(await reader.readline())
    writer.close()
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply["result"]


async def send(command, params):
    try:
        print(await request(command, params))
    except RuntimeError as err:
        fail(str(err))


def configure(name=None, state=None):
    settings = load_config()
    if name:
        settings[name] = bool(state)
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(settings))
    width = max(map(len, SETTINGS))
    for setting, text in SETTINGS.items():
        print(f"{setting:<{width}}  {'on' if settings[setting] else 'off':<3}  {text}")


class Arg(NamedTuple):
    name: str
    parse: Callable[[str], object]
    help: str
    default: object = None  # makes the argument optional


class Cmd(NamedTuple):
    name: str
    command: str | None  # what the robot calls it, or None when this program handles it itself
    text: str
    args: tuple[Arg, ...] = ()
    keys: tuple[str, ...] = ()  # send the arguments as a dict with these keys
    boxed: bool = False  # wrap the dict in a list
    fixed: object = None  # send this instead of arguments
    run: Callable | None = None  # called with the arguments instead of sending a command


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
    "clean": (
        "Cleaning",
        [
            Cmd(
                "rooms",
                "app_segment_clean",
                "Clean chosen rooms",
                (
                    Arg(
                        "rooms",
                        numbers,
                        "Room numbers separated by commas, like 1,2. See: rooms list",
                    ),
                    Arg("times", int, "How many passes per room", 1),
                ),
                ("segments", "repeat"),
                True,
            ),
            Cmd("spot", "app_spot", "Clean a small area around the robot"),
            Cmd(
                "estimate",
                "app_get_clean_estimate_info",
                "Show the estimated time and area of this clean",
            ),
        ],
    ),
    "rooms": (
        "Rooms",
        [
            Cmd("list", "get_room_mapping", "List room numbers (the first number of each row)"),
            Cmd("order", "get_clean_sequence", "Show the order rooms are cleaned in"),
            Cmd("progress", "get_segment_status", "Show room cleaning progress"),
            Cmd("custom", "get_customize_clean_mode", "Show whether rooms use their own settings"),
        ],
    ),
    "maps": (
        "Floor maps",
        [
            Cmd("list", "get_multi_maps_list", "List the saved floor maps"),
            Cmd(
                "load",
                "load_multi_map",
                "Switch to another saved floor map",
                (Arg("map", int, "Map number, listed by: maps list"),),
            ),
            Cmd("status", "get_map_status", "Show the current map state"),
            Cmd("tidy", "get_map_beautification_status", "Show whether map cleanup is on"),
            Cmd("offline", "get_offline_map_status", "Show whether offline maps are on"),
        ],
    ),
    "suction": (
        "Suction power",
        [
            Cmd("show", "get_custom_mode", "Show the suction power"),
        ],
    ),
    "water": (
        "Water flow",
        [
            Cmd("show", "get_water_box_custom_mode", "Show the water flow level"),
        ],
    ),
    "mode": (
        "Cleaning mode",
        [
            Cmd("show", "get_clean_motor_mode", "Show suction, water and mop route together"),
        ],
    ),
    "carpet": (
        "Carpets",
        [
            Cmd("boost", "get_carpet_mode", "Show the carpet boost setting"),
            Cmd("mode", "get_carpet_clean_mode", "Show how carpets are cleaned"),
            Cmd("deep", "app_get_carpet_deep_clean_status", "Show whether carpet deep clean is on"),
        ],
    ),
    "obstacles": (
        "Obstacles",
        [
            Cmd("avoidance", "get_collision_avoid_status", "Show the obstacle avoidance setting"),
            Cmd(
                "dirt",
                "get_dirty_object_detect_status",
                "Show whether dirty object detection is on",
            ),
            Cmd(
                "furniture",
                "get_identify_furniture_status",
                "Show whether furniture detection is on",
            ),
            Cmd("gaps", "get_gap_deep_clean_status", "Show whether gap deep clean is on"),
            Cmd(
                "pets",
                "get_pet_supplies_deep_clean_status",
                "Show whether pet supplies deep clean is on",
            ),
        ],
    ),
    "floors": (
        "Floor types",
        [
            Cmd(
                "detect",
                "get_identify_ground_material_status",
                "Show whether floor type detection is on",
            ),
            Cmd(
                "adapt",
                "get_clean_follow_ground_material_status",
                "Show whether cleaning adapts to the floor type",
            ),
        ],
    ),
    "washing": (
        "Mop washing",
        [
            Cmd("start", "app_start_wash", "Wash the mop at the dock"),
            Cmd("stop", "app_stop_wash", "Stop washing the mop"),
            Cmd("mode", "get_wash_towel_mode", "Show the mop wash mode"),
            Cmd("smart", "get_smart_wash_params", "Show the smart mop wash settings"),
            Cmd("temperature", "get_wash_water_temperature", "Show the mop wash water temperature"),
            Cmd(
                "fluid", "get_auto_delivery_cleaning_fluid", "Show the auto cleaning fluid setting"
            ),
        ],
    ),
    "drying": (
        "Mop drying",
        [
            Cmd(
                "start",
                "app_set_dryer_status",
                "Dry the mop now",
                keys=("status",),
                fixed={"status": 1},
            ),
            Cmd(
                "stop",
                "app_set_dryer_status",
                "Stop drying the mop",
                keys=("status",),
                fixed={"status": 0},
            ),
            Cmd(
                "show", "app_get_dryer_setting", "Show whether the dock dries the mop after a clean"
            ),
            Cmd(
                "set",
                "app_set_dryer_setting",
                "Dry the mop after every clean, or not",
                (STATE,),
                ("status",),
            ),
        ],
    ),
    "dust": (
        "Dust emptying",
        [
            Cmd("start", "app_start_collect_dust", "Empty the bin into the dock"),
            Cmd("stop", "app_stop_collect_dust", "Stop emptying the bin"),
            Cmd("mode", "get_dust_collection_mode", "Show how often the dock empties the bin"),
            Cmd("auto", "get_dust_collection_switch_status", "Show whether auto-empty is on"),
        ],
    ),
    "status": (
        "Robot status",
        [
            Cmd("show", "get_status", "Show the robot's state, battery, errors and settings"),
            Cmd("serial", "get_serial_number", "Show the serial number"),
            Cmd("features", "get_fw_features", "Show the firmware feature list"),
            Cmd("support", "app_get_init_status", "Show what the robot supports"),
        ],
    ),
    "errors": (
        "Errors",
        [
            Cmd(
                "resolve",
                "resolve_error",
                "Clear an error, like tapping Resolved in the app",
                (Arg("code", int, "Error code, shown by: status show"),),
                ("error_code",),
            ),
        ],
    ),
    "history": (
        "Cleaning history",
        [
            Cmd(
                "list", "get_clean_summary", "Show total cleaning time, area, count and record ids"
            ),
            Cmd(
                "show",
                "get_clean_record",
                "Show one past clean",
                (Arg("id", int, "Record id, listed by: history list"),),
            ),
        ],
    ),
    "parts": (
        "Brushes and filters",
        [
            Cmd("show", "get_consumable", "Show how worn the brushes and filter are"),
            Cmd(
                "reset",
                "reset_consumable",
                "Reset a wear counter after replacing a part",
                (
                    Arg(
                        "part",
                        choice(*(part.value for part in ConsumableAttribute)),
                        "The part you replaced",
                    ),
                ),
            ),
        ],
    ),
    "brushes": (
        "Side brush",
        [
            Cmd("extend", "get_right_brush_stretch_status", "Show whether the side brush extends"),
            Cmd("tag", "get_stretch_tag_status", "Show the stretch tag setting"),
        ],
    ),
    "volume": (
        "Voice volume",
        [
            Cmd("show", "get_sound_volume", "Show the voice volume"),
            Cmd(
                "set",
                "change_sound_volume",
                "Set the voice volume",
                (Arg("volume", percent, "0 to 100"),),
            ),
        ],
    ),
    "voice": (
        "Voice pack",
        [
            Cmd("show", "get_current_sound", "Show the voice pack in use"),
            Cmd("progress", "get_sound_progress", "Show the voice pack download progress"),
        ],
    ),
    "lights": (
        "Lights",
        [
            Cmd("show", "get_led_status", "Show whether the status light is on"),
            Cmd("set", "set_led_status", "Turn the status light on or off", (STATE,)),
            Cmd("mic", "get_ap_mic_led_status", "Show whether the microphone light is on"),
        ],
    ),
    "locks": (
        "Child lock",
        [
            Cmd("show", "get_child_lock_status", "Show whether the child lock is on"),
            Cmd(
                "set",
                "set_child_lock_status",
                "Turn the child lock on or off",
                (STATE,),
                ("lock_status",),
            ),
        ],
    ),
    "quiet": (
        "Quiet hours",
        [
            Cmd("show", "get_dnd_timer", "Show the hours the robot stays silent"),
            Cmd("set", "set_dnd_timer", "Set the hours the robot stays silent", (FROM, UNTIL)),
            Cmd("off", "close_dnd_timer", "Turn quiet hours off"),
        ],
    ),
    "offpeak": (
        "Off-peak charging",
        [
            Cmd("show", "get_valley_electricity_timer", "Show the off-peak charging hours"),
            Cmd(
                "set",
                "set_valley_electricity_timer",
                "Charge only during these hours",
                (FROM, UNTIL),
            ),
            Cmd("off", "close_valley_electricity_timer", "Charge at any hour"),
        ],
    ),
    "schedules": (
        "Cleaning schedules",
        [
            Cmd("list", "get_timer", "Show the cleaning schedules"),
            Cmd("summary", "get_timer_summary", "Show a short list of the cleaning schedules"),
            Cmd("cloud", "get_server_timer", "Show the schedules saved in the cloud"),
        ],
    ),
    "network": (
        "Wi-Fi",
        [
            Cmd("show", "get_network_info", "Show the Wi-Fi name, signal and IP address"),
            Cmd("scan", "app_get_wifi_list", "List the Wi-Fi networks the robot sees"),
        ],
    ),
    "timezone": (
        "Timezone",
        [
            Cmd("show", "get_timezone", "Show the timezone"),
        ],
    ),
    "locale": (
        "Language and region",
        [
            Cmd("show", "app_get_locale", "Show the language and region"),
        ],
    ),
    "camera": (
        "Camera",
        [
            Cmd("show", "get_camera_status", "Show whether the camera is on"),
        ],
    ),
    "config": (
        "Settings",
        [
            Cmd("show", None, "Show the settings", run=configure),
            Cmd(
                "set",
                None,
                "Change a setting",
                (
                    Arg("name", choice(*SETTINGS), "One of: " + ", ".join(SETTINGS)),
                    STATE,
                ),
                run=configure,
            ),
        ],
    ),
}

# Commands that are a single word, listed before the areas
TOP = (
    "Everyday",
    [
        Cmd("find", "find_me", 'Say "I\'m over here"'),
        Cmd("start", "app_start", "Start cleaning"),
        Cmd("pause", "app_pause", "Pause cleaning"),
        Cmd("resume", None, "Continue a paused clean", run=resume),
        Cmd("stop", "app_stop", "Stop cleaning"),
        Cmd("charge", "app_charge", "Go back to the dock"),
        Cmd("install", None, "Start the server whenever you log in", run=install),
        Cmd("uninstall", None, "Stop starting the server when you log in", run=uninstall),
    ],
)


def main():
    argv = [arg for arg in sys.argv[1:] if arg not in ("-v", "--verbose")]
    setup_logging(verbose=len(argv) != len(sys.argv[1:]))
    if argv in (["-h"], ["--help"]):
        print(help_text())
        return
    try:
        if not argv:
            asyncio.run(serve())
            return
        args = build_parser().parse_args(argv)
        verb = getattr(args, "verb", None)
        if args.area in AREAS and not verb:
            print("\n".join(section_lines(args.area, AREAS[args.area])))
            return
        cmds = AREAS[args.area][1] if args.area in AREAS else TOP[1]
        cmd = next(c for c in cmds if c.name == (verb or args.area))
        values = [getattr(args, arg.name) for arg in cmd.args]
        if cmd.run is None:
            asyncio.run(send(RoborockCommand(cmd.command), params(cmd, values)))
        elif inspect.iscoroutinefunction(cmd.run):
            asyncio.run(cmd.run(*values))
        else:
            cmd.run(*values)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
