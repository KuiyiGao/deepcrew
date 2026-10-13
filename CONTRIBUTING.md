# Contributing

Thanks for helping. Three kinds of help matter most right now.

## Report what broke

Open an issue with the bug form. Paste the output of `deepcrew check` in your project, and say which leader you use: the
Claude desktop app, Claude Code in a terminal, or Codex. Cover API keys and private paths before you paste a log.

An install that fails is the most useful report of all. It is fixed first.

## Send a better prompt

How a crew splits a job lives in prompts, and the best ones come from real runs. Better wording for the prompts deepcrew
sends, in `deepcrew/prompts/basic/`, is welcome, with a before and an after on the same job: what the team page counted and
what changed.

## Share a run

The run report form asks what the crew did, the setup and team size, the tokens the team page counted, and how it went. Runs
like these are how the presets and the prompts get better.

## Code

For a change to the code, open an issue first and say what you want to change and why, so we can agree on the shape before
you write it. Then:

```bash
git clone https://github.com/KuiyiGao/deepcrew
cd deepcrew
python3 -m pip install -e ".[dev]"
make verify
```

`make verify` runs the linter and the tests. The tests need no API key and no network: members talk to a stand-in model on
this computer. For a change to the Claude Code mod in `mod/`, `make mod-check` runs its checks and tests with your Claude Code
(2.1.287 or newer). Keep pull requests small, one change each, with a test that fails before it and passes after.

## Conduct

Be kind and assume good faith. No harassment, no personal attacks. Report anything that crosses the line with the Report
content button on the comment, the issue or the pull request, and it will be handled.
