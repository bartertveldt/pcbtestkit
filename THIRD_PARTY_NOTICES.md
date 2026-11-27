# Third-party notices

## The software

PCB TestKit (the KiCad plugin, the CLI, the MCP server and the test runner) is under the [MIT licence](LICENSE).
It contains no third-party code: it uses only the Python standard library, and the packages declare no
dependencies. Each release has an SBOM (`pcbtestkit-<version>.cdx.json`) that records this.

PCB TestKit calls these programs if you have them installed. It doesn't ship or change any of them:

| Program | Used for | Licence |
| ------- | -------- | ------- |
| [KiCad](https://www.kicad.org) and `kicad-cli` | renders, DRC/ERC, 3D shapes, the video frames | GPL-3.0-or-later |
| [ffmpeg](https://ffmpeg.org) | joining video frames into MP4 files | LGPL-2.1-or-later, or GPL depending on the build |
| Python and wxPython (KiCad's own) | the plugin dialogs, video captions | PSF licence, wxWindows Library Licence |
| [kicad-python](https://pypi.org/project/kicad-python/) (optional, `pip install pcbtestkit[ipc]`) | KiCad 9+ IPC-API entry point | MIT |

The runner can use `pyserial` (BSD-3-Clause) and `smbus2` (MIT) on the tester if you install them; they are
optional and not shipped.

## Generated files

Files that PCB TestKit generates from your board belong to you: no licence of PCB TestKit's applies to them. They may
contain shapes derived from KiCad's library 3D models. [KiCad's library licence](https://www.kicad.org/libraries/license/)
(CC BY-SA 4.0 with an exception for designs and generated files) does not bind such files to its share-alike
terms.

## The examples

The example outputs in [`docs/examples/`](docs/examples/) are mostly made from KiCad's demo boards. Each one keeps its
board's licence and credits its authors; see [docs/examples/LICENSES.md](docs/examples/LICENSES.md).
