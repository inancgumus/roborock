#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["python-roborock", "keyring", "platformdirs", "pytest"]
# ///
"""Integration tests. They run the real server and the real commands against fake
Roborock servers that listen on real sockets. Run them with ./tests/test_roborock.py."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fakes import EMAIL_CODE, World, fresh_state

HERE = Path(__file__).resolve().parent
ROBORROCK = str(HERE.parent / "roborock.py")


def eventually(check, timeout=30, what="the condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Sandbox:
    """Its own home folder, keychain and port, pointed at the fake servers."""

    def __init__(self, world, folder):
        self.folder = folder
        self.home = folder / "home"
        self.home.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "ROBORROCK_"))}
        self.env.update(
            HOME=str(self.home),
            ROBORROCK_PORT=str(free_port()),
            ROBORROCK_API_URL=world.cloud.url,
            PYTHON_KEYRING_BACKEND="fake_keyring.FileKeyring",
            PYTHONPATH=str(HERE),
            TEST_KEYRING_FILE=str(folder / "keyring.json"),
            NO_COLOR="1",
        )

    def run(self, *args, **options):
        return subprocess.run([ROBORROCK, *args], env=self.env, capture_output=True, text=True, timeout=60, **options)

    def start_server(self, *args, answers="me@example.com\n" + EMAIL_CODE + "\n"):
        return Server(self, args, answers)


class Server:
    def __init__(self, sandbox, args, answers):
        self.process = subprocess.Popen([ROBORROCK, *args], env=sandbox.env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.output = []
        threading.Thread(target=self.collect, daemon=True).start()
        self.process.stdin.write(answers)
        self.process.stdin.flush()

    def collect(self):
        for line in self.process.stdout:
            self.output.append(line)

    @property
    def text(self):
        return "".join(self.output)

    def wait_until_ready(self):
        eventually(lambda: "Ready!" in self.text or self.process.poll() is not None, what="the server to be ready")
        assert "Ready!" in self.text, self.text

    def stop(self):
        self.process.terminate()
        self.process.wait(20)


@pytest.fixture(scope="module")
def world():
    world = World().start()
    yield world
    world.stop()


@pytest.fixture(scope="module")
def sandbox(world, tmp_path_factory):
    return Sandbox(world, tmp_path_factory.mktemp("module"))


@pytest.fixture(scope="module")
def server(sandbox):
    server = sandbox.start_server()
    server.wait_until_ready()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def fresh_robot(world):
    world.vacuum.state.clear()
    world.vacuum.state.update(fresh_state())
    world.vacuum.requests.clear()
    yield


def sent(world, method):
    return [params for name, params in world.vacuum.requests if name == method]


def test_first_start_logs_in_with_the_emailed_code(world, server, sandbox):
    assert world.cloud.codes_requested == ["me@example.com"]
    assert "Logged in." in server.text
    assert "Serving Fakey" in server.text
    assert world.cloud.unknown_requests == []


def test_start_does_not_show_library_logs(server):
    assert "MQTT" not in server.text


def test_status_shows_what_the_robot_reports(server, sandbox):
    result = sandbox.run("status", "show")
    assert result.returncode == 0, result.stderr
    assert "'battery': 87" in result.stdout


def test_volume_round_trip(server, sandbox, world):
    assert sandbox.run("volume", "show").stdout.strip() == "[30]"
    assert sandbox.run("volume", "set", "40").returncode == 0
    assert world.vacuum.state["volume"] == 40
    assert sandbox.run("volume", "show").stdout.strip() == "[40]"


@pytest.mark.parametrize("command, state", [
    (["start"], 5), (["pause"], 10), (["stop"], 3), (["charge"], 8),
])
def test_everyday_commands_move_the_robot(server, sandbox, world, command, state):
    world.vacuum.state["state"] = 6
    assert sandbox.run(*command).returncode == 0
    assert world.vacuum.state["state"] == state


def test_find_asks_the_robot_to_speak(server, sandbox, world):
    assert sandbox.run("find").returncode == 0
    assert sent(world, "find_me") == [[]]


@pytest.mark.parametrize("command, method, params", [
    (["clean", "rooms", "1,2"], "app_segment_clean", [{"segments": [1, 2], "repeat": 1}]),
    (["clean", "rooms", "2", "3"], "app_segment_clean", [{"segments": [2], "repeat": 3}]),
    (["quiet", "set", "22:30", "08:00"], "set_dnd_timer", [22, 30, 8, 0]),
    (["locks", "set", "on"], "set_child_lock_status", {"lock_status": 1}),
    (["lights", "set", "off"], "set_led_status", [0]),
    (["drying", "start"], "app_set_dryer_status", {"status": 1}),
    (["drying", "stop"], "app_set_dryer_status", {"status": 0}),
])
def test_commands_send_what_the_robot_expects(server, sandbox, world, command, method, params):
    assert sandbox.run(*command).returncode == 0
    assert sent(world, method) == [params]


def test_rooms_list(server, sandbox):
    assert "[1, '100', 1]" in sandbox.run("rooms", "list").stdout


def test_a_command_the_robot_does_not_know_fails_in_red(server, sandbox):
    result = sandbox.run("schedules", "list")
    assert result.returncode == 1
    assert "not recognized" in result.stderr


def test_bad_input_is_rejected_before_reaching_the_robot(server, sandbox, world):
    result = sandbox.run("volume", "set", "500")
    assert result.returncode == 2
    assert "use a number from 0 to 100" in result.stderr
    assert sent(world, "change_sound_volume") == []


def test_unknown_command(server, sandbox):
    result = sandbox.run("nope")
    assert result.returncode == 2
    assert "unknown command 'nope'" in result.stderr


def test_help_lists_every_section(sandbox):
    result = sandbox.run("-h")
    assert result.returncode == 0
    for text in ("Everyday", "clean rooms <rooms> [times]", "Settings", "config set <name> <on|off>"):
        assert text in result.stdout


def test_an_area_alone_lists_its_commands(sandbox):
    result = sandbox.run("clean")
    assert result.stdout.splitlines()[0].strip() == "Cleaning"
    assert "clean spot" in result.stdout
    assert "Everyday" not in result.stdout


def test_command_help_explains_the_input(sandbox):
    result = sandbox.run("quiet", "set", "-h")
    assert "Start time, like 22:00" in result.stdout


def test_commands_say_how_to_start_a_server_when_none_runs(world, tmp_path):
    result = Sandbox(world, tmp_path).run("status", "show")
    assert result.returncode == 1
    assert "No server is running" in result.stderr


def test_starting_again_lists_the_help(server, sandbox):
    result = sandbox.run()
    assert result.returncode == 0
    assert "Commands:" in result.stdout


def test_logs_always_go_to_a_file(server, sandbox):
    sandbox.run("volume", "show")
    logs = list(sandbox.home.rglob("roborock.log"))
    assert logs, "no log file under the home folder"
    eventually(lambda: "Ran get_sound_volume" in logs[0].read_text(), what="the command in the log")
    assert "Starting MQTT session" in logs[0].read_text()


def test_dash_v_also_prints_the_logs(world, tmp_path):
    sandbox = Sandbox(world, tmp_path)
    verbose = sandbox.start_server("-v")
    try:
        verbose.wait_until_ready()
        assert "Starting MQTT session" in verbose.text
    finally:
        verbose.stop()


def test_wrong_code_fails_the_login(world, tmp_path):
    sandbox = Sandbox(world, tmp_path)
    failed = sandbox.start_server(answers="me@example.com\n000000\n")
    eventually(lambda: failed.process.poll() is not None, what="the server to give up")
    assert failed.process.returncode != 0
    assert "Ready!" not in failed.text


def bumper_stuck(world, mode=3):
    world.vacuum.state.update(state=12, error_code=2, in_cleaning=mode)


def test_bumper_stuck_is_cleared_and_the_clean_resumes(server, world):
    bumper_stuck(world)
    eventually(lambda: world.vacuum.state["state"] == 5, what="the clean to resume")
    assert world.vacuum.state["error_code"] == 0
    assert sent(world, "resolve_error") == [{"error_code": 2}]
    assert sent(world, "resume_segment_clean") == [[]]


def test_a_zone_clean_resumes_as_a_zone_clean(server, world):
    bumper_stuck(world, mode=2)
    eventually(lambda: world.vacuum.state["state"] == 5, what="the clean to resume")
    assert sent(world, "resume_zoned_clean") == [[]]


def test_other_errors_are_left_alone(server, world):
    world.vacuum.state.update(state=12, error_code=1)
    time.sleep(4)
    assert sent(world, "resolve_error") == []


def test_turning_bumper_off_stops_the_loop(server, sandbox, world):
    assert sandbox.run("config", "set", "bumper", "off").returncode == 0
    try:
        bumper_stuck(world)
        time.sleep(4)
        assert world.vacuum.state["error_code"] == 2
        assert sent(world, "resolve_error") == []
    finally:
        sandbox.run("config", "set", "bumper", "on")
    eventually(lambda: world.vacuum.state["state"] == 5, what="the clean to resume once it is on again")


def test_config_shows_settings(sandbox):
    result = sandbox.run("config", "show")
    assert "bumper" in result.stdout and "on" in result.stdout
    assert "off" in sandbox.run("config", "set", "bumper", "off").stdout
    assert "on" in sandbox.run("config", "set", "bumper", "on").stdout


def test_resume_continues_a_paused_room_clean(server, sandbox, world):
    world.vacuum.state.update(state=10, in_cleaning=3)
    assert sandbox.run("resume").returncode == 0
    assert world.vacuum.state["state"] == 5
    assert sent(world, "resume_segment_clean") == [[]]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", *sys.argv[1:]]))
