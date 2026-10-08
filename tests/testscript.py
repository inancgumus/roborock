"""Run .txtar scripts like Go's testscript.

A script is a list of commands followed by files, each introduced by a line like
`-- name --`. The files are written to the script's work folder before it runs.

    exec roborock status show     run a program, and fail if it exits with an error
    ! exec roborock nope          run a program, and fail if it succeeds
    exec roborock &               run a program in the background
    stdin login.txt               feed a file to the next program
    stdout 'regexp'               the last program printed it on stdout
    stderr 'regexp'               the last program printed it on stderr
    await 'regexp'                wait until the background program prints it
    kill                          stop the background program
    exists FILE | grep REGEXP FILE | sleep SECONDS
    vacuum set KEY VALUE          change what the fake robot reports
    vacuum await KEY VALUE        wait until the robot's state has that value
    vacuum sent METHOD [JSON]     the robot received that request (with those params)
    vacuum clear                  forget the requests so far
    cloud asked EMAIL             the fake cloud was asked to email a code to EMAIL

A leading `!` flips any command. A leading `[darwin]` or `[linux]` runs it on that system only.
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from fakes import World

HERE = Path(__file__).resolve().parent
ROBORROCK = HERE.parent / "roborock.py"


class Failure(AssertionError):
    pass


def split(line, env):
    """Split a line into words. Single quotes keep text as is, other text expands $VARIABLES."""
    words, word, quote, started = [], "", None, False
    i = 0
    while i < len(line):
        char = line[i]
        if quote:
            if char == quote:
                quote = None
            elif char == "$" and quote == '"':
                name = re.match(r"\$(\w+)", line[i:])
                word += env.get(name.group(1), "") if name else char
                i += len(name.group(0)) - 1 if name else 0
            else:
                word += char
        elif char in "'\"":
            quote, started = char, True
        elif char.isspace():
            if started or word:
                words.append(word)
            word, started = "", False
        elif char == "$":
            name = re.match(r"\$(\w+)", line[i:])
            word += env.get(name.group(1), "") if name else char
            i += len(name.group(0)) - 1 if name else 0
        else:
            word += char
        i += 1
    if quote:
        raise Failure(f"unterminated quote in: {line}")
    if started or word:
        words.append(word)
    return words


def parse(text):
    lines, files, current = [], {}, None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.fullmatch(r"-- (.+) --", line)
        if marker:
            current = files.setdefault(marker.group(1), [])
        elif current is not None:
            current.append(line)
        else:
            lines.append((number, line))
    return lines, {name: "\n".join(content) + "\n" for name, content in files.items()}


class Background:
    def __init__(self, args, env, cwd, stdin):
        self.process = subprocess.Popen(
            args,
            env=env,
            cwd=cwd,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.output = []
        threading.Thread(target=self.collect, daemon=True).start()

    def collect(self):
        for line in self.process.stdout:
            self.output.append(line)

    @property
    def text(self):
        return "".join(self.output)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(20)
        except subprocess.TimeoutExpired:
            self.process.kill()


class Script:
    def __init__(self, path, folder):
        self.path = Path(path)
        self.work = folder / "work"
        self.home = folder / "home"
        self.work.mkdir()
        self.home.mkdir()
        self.world = World().start()
        self.stdout = self.stderr = ""
        self.stdin = None
        self.background = []
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        (self.work / "roborock").symlink_to(ROBORROCK)
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("XDG_", "ROBORROCK_"))
        }
        self.env.update(
            HOME=str(self.home),
            WORK=str(self.work),
            NO_COLOR="1",
            ROBORROCK_PORT=str(port),
            ROBORROCK_API_URL=self.world.cloud.url,
            PYTHON_KEYRING_BACKEND="fake_keyring.FileKeyring",
            PYTHONPATH=str(HERE),
            TEST_KEYRING_FILE=str(folder / "keyring.json"),
            SERVICE_LOG=str(self.work / "service.log"),
            PATH=os.pathsep.join([str(HERE / "bin"), str(self.work), os.environ["PATH"]]),
        )

    def run(self):
        lines, files = parse(self.path.read_text())
        for name, content in files.items():
            (self.work / name).write_text(content)
        try:
            for number, line in lines:
                if line.strip() and not line.lstrip().startswith("#"):
                    self.line = (number, line)
                    self.command(split(line, self.env))
        except Failure as err:
            raise Failure(self.report(str(err))) from None
        finally:
            for process in self.background:
                process.stop()
            self.world.stop()

    def report(self, message):
        number, line = self.line
        shown = [f"{self.path.name}:{number}: {line.strip()}", message]
        if self.stdout:
            shown.append(f"[stdout]\n{self.stdout}")
        if self.stderr:
            shown.append(f"[stderr]\n{self.stderr}")
        for process in self.background:
            shown.append(f"[background]\n{process.text}")
        return "\n".join(shown)

    def command(self, words):
        negate = False
        while words and re.fullmatch(r"\[!?\w+\]", words[0]):
            condition = words.pop(0)[1:-1]
            if (sys.platform == condition.lstrip("!")) == condition.startswith("!"):
                return
        if words[:1] == ["!"]:
            negate = True
            words = words[1:]
        name, args = words[0], words[1:]
        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            raise Failure(f"unknown command {name!r}")
        handler(negate, args)

    def check(self, negate, ok, message):
        if bool(ok) == negate:
            raise Failure(("unexpected: " if negate else "") + message)

    def cmd_exec(self, negate, args):
        background = args[-1:] == ["&"]
        args = args[:-1] if background else args
        stdin, self.stdin = self.stdin, None
        if background:
            self.background.append(
                Background(args, self.env, self.work, stdin or subprocess.DEVNULL)
            )
            return
        try:
            done = subprocess.run(
                args,
                env=self.env,
                cwd=self.work,
                stdin=stdin or subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError:
            raise Failure(f"{args[0]}: command not found") from None
        self.stdout, self.stderr = done.stdout, done.stderr
        self.check(negate, done.returncode == 0, f"exit status {done.returncode}")

    def cmd_stdin(self, negate, args):
        self.stdin = open(self.work / args[0])

    def cmd_stdout(self, negate, args):
        self.check(
            negate, re.search(args[0], self.stdout, re.M), f"stdout does not match {args[0]!r}"
        )

    def cmd_stderr(self, negate, args):
        self.check(
            negate, re.search(args[0], self.stderr, re.M), f"stderr does not match {args[0]!r}"
        )

    def cmd_await(self, negate, args):
        process = self.background[-1]
        self.wait_for(
            lambda: re.search(args[0], process.text, re.M) or process.process.poll() is not None
        )
        self.check(
            negate,
            re.search(args[0], process.text, re.M),
            f"the background program did not print {args[0]!r}",
        )

    def cmd_kill(self, negate, args):
        process = self.background.pop()
        process.stop()
        self.stdout = process.text

    def cmd_exists(self, negate, args):
        self.check(
            negate,
            (self.work / args[0]).exists() or Path(args[0]).exists(),
            f"{args[0]} does not exist",
        )

    def cmd_grep(self, negate, args):
        text = (
            Path(args[1]).read_text()
            if Path(args[1]).is_absolute()
            else (self.work / args[1]).read_text()
        )
        self.check(negate, re.search(args[0], text, re.M), f"{args[1]} does not match {args[0]!r}")

    def cmd_sleep(self, negate, args):
        time.sleep(float(args[0]))

    @staticmethod
    def wait_for(check, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if check():
                return True
            time.sleep(0.1)
        return False

    def cmd_vacuum(self, negate, args):
        action, rest = args[0], args[1:]
        vacuum = self.world.vacuum
        if action == "set":
            vacuum.state[rest[0]] = json.loads(rest[1])
        elif action == "await":
            ok = self.wait_for(lambda: vacuum.state[rest[0]] == json.loads(rest[1]))
            self.check(negate, ok, f"{rest[0]} is {vacuum.state[rest[0]]!r}, not {rest[1]}")
        elif action == "sent":
            want = [json.loads(rest[1])] if len(rest) > 1 else None
            seen = [
                params
                for method, params in vacuum.requests
                if method == rest[0] and want in (None, [params])
            ]
            self.check(
                negate,
                seen,
                f"the robot did not get {rest[0]} {rest[1:]}; it got {vacuum.requests}",
            )
        elif action == "clear":
            vacuum.requests.clear()
        else:
            raise Failure(f"unknown vacuum action {action!r}")

    def cmd_cloud(self, negate, args):
        self.check(
            negate,
            args[1] in self.world.cloud.codes_requested,
            f"the cloud was not asked to email {args[1]}",
        )
