Control a Roborock vacuum from the terminal, and resume it automatically when it reports "bumper stuck".

## Requirements

- [uv](https://docs.astral.sh/uv/), which installs the dependencies on the first run
- A Roborock account with one vacuum

## Quick start

Start the server in its own terminal and leave it running. It logs in with an emailed code the first time and keeps one connection to the vacuum open.

```sh
./roborock.py
```

Then send commands from any other terminal. They find the server on their own.

```sh
./roborock.py find                 # the vacuum says "I'm over here"
./roborock.py clean rooms 1,2      # clean rooms 1 and 2
./roborock.py volume set 40
./roborock.py quiet set 22:00 08:00
```

## Commands

Commands are `<area> <verb>`. Run an area alone to list its commands, or add `-h` to a command to see what it needs.

| Command | What it does |
| --- | --- |
| `./roborock.py -h` | List every command |
| `./roborock.py start` | Start cleaning |
| `./roborock.py pause` | Pause cleaning |
| `./roborock.py resume` | Continue a paused clean |
| `./roborock.py stop` | Stop cleaning |
| `./roborock.py charge` | Go back to the dock |
| `./roborock.py clean spot` | Clean a small area around the robot |
| `./roborock.py status show` | Show the robot's state, battery and errors |
| `./roborock.py rooms list` | List room numbers |
| `./roborock.py drying start` | Dry the mop |
| `./roborock.py parts show` | Show how worn the brushes and filter are |

## Bumper stuck

The vacuum sometimes stops with "bumper stuck" even when the bumper is fine. This clears the error and resumes the clean, checking every second until you stop it with Ctrl-C:

```sh
./roborock.py nobumperstuck
```

## Files

| File | Holds |
| --- | --- |
| `~/.roborock-autoresume.json` | The login token |
| `~/.roborock-supervisor.sock` | The socket commands use to reach the server. Only your user can connect |
