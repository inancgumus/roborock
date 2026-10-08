#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["python-roborock"]
# ///
"""Control a Roborock vacuum.

Usage:
    ./supervisor.py login you@example.com  Log in with an emailed code
    ./supervisor.py nobumperstuck          Resume each time it reports "bumper stuck"
    ./supervisor.py find                   Say "I'm over here"
    ./supervisor.py start                  Start cleaning
    ./supervisor.py pause                  Pause cleaning
    ./supervisor.py stop                   Stop cleaning
    ./supervisor.py charge                 Go back to the dock
    ./supervisor.py list                   List every other command
    ./supervisor.py <command>              Send any command from the list
"""
import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path

from roborock.data import UserData
from roborock.data.v1.v1_code_mappings import RoborockErrorCode, RoborockStateCode
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.roborock_typing import RoborockCommand
from roborock.web_api import RoborockApiClient

SESSION_FILE = Path.home() / ".roborock-autoresume.json"

# Plain app_start would restart a room or zone clean from scratch
RESUME_COMMANDS = {
    2: RoborockCommand.RESUME_ZONED_CLEAN,
    3: RoborockCommand.RESUME_SEGMENT_CLEAN,
}


async def login(email):
    client = RoborockApiClient(email)
    await client.request_code()
    user_data = await client.code_login(input("Code from email: ").strip())
    SESSION_FILE.write_text(json.dumps({"email": email, "user_data": user_data.as_dict()}))
    SESSION_FILE.chmod(0o600)  # holds account tokens
    print("Logged in.")


async def resume_if_stuck(status, command):
    await status.refresh()
    if status.error_code != RoborockErrorCode.bumper_stuck:
        return
    logging.warning("Bumper stuck, continuing")
    await status.resolve_error()
    if status.state in (RoborockStateCode.paused, RoborockStateCode.error):
        mode = int(status.in_cleaning or 0)
        await command.send(RESUME_COMMANDS.get(mode, RoborockCommand.APP_START))


@contextlib.asynccontextmanager
async def connect():
    saved = json.loads(SESSION_FILE.read_text())
    params = UserParams(saved["email"], UserData.from_dict(saved["user_data"]))
    manager = await create_device_manager(params)
    try:
        yield next(d for d in await manager.get_devices() if d.v1_properties)
    finally:
        await manager.close()


async def run():
    async with connect() as vacuum:
        status = vacuum.v1_properties.status
        command = vacuum.v1_properties.command
        logging.info("Watching %s.", vacuum.name)
        while True:
            try:
                await resume_if_stuck(status, command)
            except Exception:
                logging.exception("Failed, retrying in 1s")
            await asyncio.sleep(1)


def to_command(name):
    names = {c.value for c in RoborockCommand}
    name = {"find": "find_me"}.get(name, name)
    if name not in names and f"app_{name}" in names:
        name = f"app_{name}"
    if name not in names:
        sys.exit(f"Unknown command {name!r}. Run ./supervisor.py list.")
    return RoborockCommand(name)


async def send(command):
    async with connect() as vacuum:
        print(await vacuum.v1_properties.command.send(command))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    args = sys.argv[1:]
    try:
        if args[:1] == ["login"] and len(args) == 2:
            asyncio.run(login(args[1]))
        elif args == ["nobumperstuck"]:
            asyncio.run(run())
        elif args == ["list"]:
            print("\n".join(sorted(c.value for c in RoborockCommand)))
        elif len(args) == 1 and args[0] not in ("-h", "--help", "help"):
            asyncio.run(send(to_command(args[0])))
        else:
            print(__doc__)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
