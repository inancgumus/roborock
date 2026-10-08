Control a Roborock vacuum from the terminal.

## Quick start

Start the server in its own terminal and leave it running.

The first time, it asks for your email and logs in with the code it emails you.

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

Commands are `<area> <verb>`.

- Run an area alone to list its commands, like `./roborock.py clean`.
- Add `-h` to a command to see what it needs.

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

The vacuum sometimes stops with "bumper stuck" even when the bumper is fine.

This clears the error and resumes the clean every time it happens.

It checks every second until you stop it with Ctrl-C:

```sh
./roborock.py nobumperstuck
```
