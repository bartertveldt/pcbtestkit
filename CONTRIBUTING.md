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

`docs/examples/` holds complete outputs for three boards. Regenerate them only when outputs change visibly
(they are about 50 MB):

```bash
python3 tools/regen_examples.py              # about 35 min, needs KiCad 9+ and ffmpeg
python3 tools/regen_examples.py --no-video   # a few minutes
```

## Security

Please report vulnerabilities privately, as [SECURITY.md](SECURITY.md) describes, not in an issue or a pull
request. Don't attach board files you are not allowed to publish, and leave no personal data or credentials in
commits: secret scanning with push protection is on for this repository.

## Releasing

See [docs/PUBLISHING.md](docs/PUBLISHING.md).
