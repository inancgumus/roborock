# roborock

`roborock.py` controls a Roborock vacuum from the terminal.

My vacuum stops when it reports "bumper stuck", even when the bumper is fine. The supervisor watches for that error, clears it, and resumes the cleaning where it stopped.

It needs [uv](https://docs.astral.sh/uv/), which installs the dependencies on the first run.

```sh
./roborock.py login you@example.com  # log in once with the emailed code
./roborock.py nobumperstuck          # resume after each "bumper stuck"
./roborock.py find                   # say "I'm over here"
./roborock.py start                  # start, pause, stop, or charge
./roborock.py -h                     # list every command
```

The login token is saved in `~/.roborock-autoresume.json`.
