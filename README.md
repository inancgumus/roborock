# roborock

`supervisor.py` controls a Roborock vacuum from the terminal.

My vacuum stops when it reports "bumper stuck", even when the bumper is fine. The supervisor watches for that error, clears it, and resumes the cleaning where it stopped.

It needs [uv](https://docs.astral.sh/uv/), which installs the dependencies on the first run.

```sh
./supervisor.py login you@example.com  # log in once with the emailed code
./supervisor.py nobumperstuck          # resume after each "bumper stuck"
./supervisor.py find                   # say "I'm over here"
./supervisor.py start                  # start, pause, stop, or charge
./supervisor.py -h                     # list every command
```

The login token is saved in `~/.roborock-autoresume.json`.
