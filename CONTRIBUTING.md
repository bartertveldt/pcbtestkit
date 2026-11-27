# Contributing

Thanks for helping. Issues and pull requests are welcome, especially new fab class systems and boards on
which a check is wrong.

## Set up

```bash
git clone https://github.com/bartertveldt/pcbtestkit && cd pcbtestkit
python3 -m unittest discover -s tests        # no dependencies to install
python3 -m pcbtestkit --help
```

KiCad 9+ is needed only for renders, DRC/ERC and the video; ffmpeg only for the video. To try your change
inside KiCad: `python3 install.py --version 10.0 --no-ipc`, then *Tools > External Plugins > Refresh Plugins*.

## Rules

[AGENTS.md](AGENTS.md) lists the rules; the important ones:

- Standard library only, Python 3.9 compatible (KiCad's bundled Python), relative imports inside the plugin.
- No absolute paths or personal data in generated files.
- Agent tools never raise; they return `{"ok": false, "error": {...}}`.
- After changing CLI options, tools or config defaults: `python3 tools/gen_docs.py`.

## Before you open a pull request

```bash
python3 -m unittest discover -s tests
python3 tools/gen_docs.py --check
python3 tools/build_pcm.py --check
python3 -m pcbtestkit classes validate
```

Add a test for new behaviour and a line in `CHANGELOG.md`.

## Adding a fab or standard

A class system is a JSON file; no code. See [docs/CLASS_SYSTEMS.md](docs/CLASS_SYSTEMS.md#adding-a-system-about-15-minutes-for-a-typical-fab-page).
Put it in `pcbtestkit/classsys/systems/<id>.json` with `source`, `verified` (date you checked the source) and
test vectors at the class boundaries, and add a row to the table in `docs/CLASS_SYSTEMS.md`.

## Regenerating the examples

`docs/examples/` holds complete outputs for four boards, a test station and a test environment (a catalog of 128
boards). Regenerate them only when outputs change visibly:

```bash
python3 tools/regen_examples.py              # about four hours, needs KiCad 9+ and ffmpeg
python3 tools/regen_examples.py --no-video   # under an hour
python3 tools/regen_examples.py --only environment --environment-from DIR   # publish an earlier catalog build
```

The catalog's outputs for every board come to about 3 GB, so the repository keeps only its environment and station
level and the boards that `docs/examples/environment/.gitignore` lists (its `!` lines). The script reads that list:
it copies, films and links only those boards, and git leaves out any other board folder copied in by hand. To keep
another board, add a line for its folder there. `TestPublishedExamples` checks that the pages link only to files
that are there, that the catalog has exactly the listed boards with their videos, and that no file holds a local
path or the state of a run.

## Security

Please report vulnerabilities privately, as [SECURITY.md](SECURITY.md) describes, not in an issue or a pull
request. Don't attach board files you are not allowed to publish, and leave no personal data or credentials in
commits: secret scanning with push protection is on for this repository.

## Releasing

See [docs/PUBLISHING.md](docs/PUBLISHING.md).
