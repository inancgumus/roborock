# Agent instructions

`roborock.py` is a single file that runs a server and sends commands to it.

## Keep the README current

Update `README.md` in the same commit as any change users can see. That covers commands, settings, setup and behavior.

- Keep the current format. No title, short sections, code blocks for commands, tables for lists.
- Add new commands to the table only when they matter for a first read. The full list lives in `./roborock.py -h`.
- Remove text that no longer matches the code.

## Writing

Use the `write` skill for the README and every other piece of prose, if it is available.

- Short sentences in plain words. Say it the way you would to a teammate.
- Keep each sentence short enough to fit on one line when rendered, about 85 characters.
- No long dashes and no `---` separators.
- Leave out the history of how something was built.

## Output

Never print the login token, the server secret or the robot's serial number.
Commands that read them must not echo them.

## Tests

Run `./tests/test_roborock.py` before every commit.

- Write integration tests only. No unit tests and no mocks.
- Run the real server and the real commands against the fake servers in `tests/fakes.py`.
- Add a test with every change in behavior, in the same commit.

## Commits

Use the `git-commit` skill if it is available. Keep each commit to one change, with its README update.
