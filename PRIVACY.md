# Privacy

**In short:** PCB TestKit runs on your computer and works only with files you give it. It has no accounts, no
telemetry, no analytics and no crash reporting. It sends nothing anywhere. The project and its maintainers never
receive your designs, your test results or anything about you.

## What PCB TestKit reads

The KiCad project you open: its board, schematic, project file and `pcbtestkit.json`. It also reads the 3D models
those files refer to, through KiCad, and the KiCad settings it needs to find them (for example your 3D model
folders).

## What it writes, and where

| Where | What | Shared when |
| ----- | ---- | ----------- |
| `pcbtestkit.json` next to your project | Your settings for the board and an audit log of changes made through the agent tools. Each entry holds the time, what changed, the reason given, and who made it (`ai` by default, or the name passed with `--actor`). | When you share or commit the project folder |
| The output folder (`production_test/` by default) | The guide, report, fixture, jigs, runner, renders and video | When you share the folder |
| `~/.pcbtestkit/` (or `PCBTESTKIT_HOME`) | Settings for every project (the remembered test environment, power options) and caches (3D shapes, board profiles), which you can delete at any time | Never: it stays on your computer |
| `runner/results/` on the tester | Every board's serial number, measurements, verdict and the time of the test | When you copy the results somewhere |

The generated files hold only your design data: the board's name, title block, net names, part references and
values, and the results that PCB TestKit computed. They never hold your user name or the folders on your computer:
files refer to each other by relative names, and tests check this on every change.

Your KiCad title block may contain names, a company or contact details, and the pages show that title block. Look at
it before you share the outputs outside your organisation.

## Network

PCB TestKit opens no network connections. The generated pages load nothing from the internet: their
Content-Security-Policy blocks it, so you can open them offline and nobody can track who looks at them. Three things
you set up yourself may use your network:

- a **facility driver** (`facility_driver.py`) that talks to your tester, for example over TCP. It connects only to
  the address you put in `hardware.json`;
- **KiCad's Plugin and Content Manager**, when you install or update the plugin through it (KiCad's own service);
- the **maintainer tools** in `tools/` (for example `build_pcm.py --submission`, which downloads KiCad's public
  package list). The plugin and the CLI never run these.

## AI agents

When you connect an AI assistant through the MCP server or the CLI's `api` command, PCB TestKit answers that assistant
on your computer. The assistant's client may then send those answers, which include design data such as net names,
part values and findings, to its AI provider. That happens under your agreement with that provider, and PCB TestKit
has no part in it. If a design is confidential, use an assistant your organisation has approved for it, or don't
connect one: everything also works without AI.

## GitHub

Issues, pull requests and security reports on GitHub are covered by
[GitHub's Privacy Statement](https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement).
Only attach board files you are allowed to publish. To report something confidential, use a private security report
(see [SECURITY.md](SECURITY.md)).

## Personal data and the GDPR

The maintainers don't collect or receive personal data from anyone who uses PCB TestKit, so for that use they are
not a controller or a processor under the GDPR (Regulation (EU) 2016/679) or similar laws. If you record personal data
with it yourself (operator names in test steps, names in a title block), that data stays in your own files, and you
decide what happens to it.

## Changes

This notice changes only together with the software, and every change is listed in the [changelog](CHANGELOG.md).
For questions, open an [issue](https://github.com/bartertveldt/pcbtestkit/issues).
