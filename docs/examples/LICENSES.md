# Licences of the example outputs

PCB TestKit's own code and documentation are under the [MIT licence](../../LICENSE). Most of the example outputs
in this folder were made from **other people's boards**: the demo projects that come with KiCad. A board's guide,
report, renders, video, fixture, jigs, nest and clamp parts are derived from that board, so each folder below is
shared under **the licence of the board it was made from**, with credit to its authors. PCB TestKit generated the
derived files; nobody changed the boards themselves.

| Folder | Made from | Authors | Licence of the board and of these outputs |
| ------ | --------- | ------- | ----------------------------------------- |
| `sample_sensor/` | The sample board made by `examples/make_sample.py` | PCB TestKit contributors | MIT |
| `pic_programmer/` | KiCad demo *pic_programmer* | The KiCad project | GPL-3.0-or-later. The demo has no licence file of its own; it is part of KiCad's source tree, which is under the GNU GPL v3 or later |
| `video/` | KiCad demo *video* | The KiCad project | GPL-3.0-or-later, as above |
| `tiny_tapeout/` | KiCad demo *tiny_tapeout*: the Tiny Tapeout 4+ demo board | Pat Deegan, Psychogenic Technologies (2023) | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) |
| `station/sample_sensor/` | The sample board | PCB TestKit contributors | MIT |
| `station/StickHub/` | KiCad demo *stickhub* | Stefan Hamminga | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) with the author's *Commercial-Use Exemption 1.0.0* (see the demo's `LICENSE.md`). Not for commercial use unless that exemption applies |
| `station/RoyalBlue54L-Feather/` | KiCad demo *royalblue54L_feather* | Lords Boards (modified by the KiCad project, 2025) | [CERN-OHL-P-2.0](https://cern-ohl.web.cern.ch/) |
| `station/pic_programmer/` | KiCad demo *pic_programmer* | The KiCad project | GPL-3.0-or-later |
| `station/` (station board, page, video) | `examples/make_station.py`; the video also shows the four boards above | PCB TestKit contributors | MIT for the station itself. Frames that show a board are under that board's licence |
| `environment/` (pages, tables, station boards, videos) | 110 boards made by `examples/make_collection.py`, and the KiCad demos listed below | PCB TestKit contributors | MIT, except what is derived from a demo board (its rows, renders and frames), which is under that board's licence |

The test environment's catalog also profiles these KiCad demos. Its pages list them, and its station videos may
show them:

| KiCad demo | Authors | Licence |
| ---------- | ------- | ------- |
| *cm5_minima* (CM5 MINIMA rev 3) | piecol | [CERN-OHL-S-2.0](https://cern-ohl.web.cern.ch/) |
| *openair-max* (One-Air-Max) | its authors (see the demo's `LICENSE.txt`) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) |
| *jetson-agx-thor-baseboard* | Antmicro (2025-2026) | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) |
| *royalblue54L_feather* (both boards) | Lords Boards | CERN-OHL-P-2.0 |
| *stickhub* | Stefan Hamminga | CC BY-NC-SA 4.0 with the Commercial-Use Exemption 1.0.0 |
| *tiny_tapeout* | Pat Deegan, Psychogenic Technologies | Apache-2.0 |
| *complex_hierarchy*, *ecc83*, *interf_u*, *kit-dev-coldfire-xilinx_5213*, *microwave*, *multichannel*, *pic_programmer*, *sonde xilinx*, *video*, *vme-wren* | The KiCad project | GPL-3.0-or-later (part of KiCad's source tree) |

The full licence texts come with each demo in KiCad's `demos` folder. KiCad installs it, and it is also in
[KiCad's source](https://gitlab.com/kicad/code/kicad/-/tree/master/demos).

## 3D models

The renders, videos and the parts' shapes in the jigs come from the 3D models of
[KiCad's libraries](https://www.kicad.org/libraries/license/). Those libraries are under CC BY-SA 4.0 with an
exception: designs that use the libraries, and files generated from them, are not bound by the share-alike terms.
No library model is copied into this repository. The files in `pcbtestkit-models/` (pogo pins, nests, clamps) are
made by PCB TestKit and are under the MIT licence.

## If you reuse these files

Keep the credit and the licence of the board a file was made from. For the share-alike licences (GPL, CC BY-SA,
CC BY-NC-SA, CERN-OHL-S), share what you derive from them under the same licence. Don't use the StickHub outputs
commercially unless the author's exemption covers you.
