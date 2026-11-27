# PCB TestKit catalog: upgrade options

Now: 119 of 128 boards fit, 382 boards per full load; worth EUR 54,139, costs EUR 16,816 (stations EUR 2,806, programmers EUR 1,140, fixtures EUR 12,870).

Make one: `pcbtestkit environment upgrade DIR --option U1 [--option U3 ...]`, a path: `--path value` (or `fit`, `batch`; `--steps N` for its first N), your own: `--add station-1:can+1,ain+8` or what you need: `--fit board_a,board_b --batch board_c=6 [--budget 500]`. It shows what it gains and costs; `--apply` builds it (the stations keep every pin they have, so the fixtures you have still plug in).

## Single changes, best first

| id | change | boards that fit | boards per load | worth | cost | net | notes |
|---|---|---:|---:|---:|---:|---:|---|
| U1 | supplies up to 48 V (now 24 V) | +3 | +11 | EUR 1,486 | EUR 1,065 | EUR 421 | fits hv_driver_01, hv_driver_02, hv_driver_03 |
| U2 | station-1: 8 more analog inputs (56 -> 64) | +0 | +2 | EUR 210 | EUR 240 | EUR -30 | bigger batches: interf_u 1->2, tinytapeout-demo 1->2; the station keeps its pins: the fixtures you have still plug in |
| U3 | station-1: 12 more analog inputs (56 -> 68) | +0 | +2 | EUR 210 | EUR 270 | EUR -60 | bigger batches: interf_u 1->2, tinytapeout-demo 1->2; the station keeps its pins: the fixtures you have still plug in |
| U4 | station-1: 16 more analog inputs (56 -> 72) | +0 | +2 | EUR 210 | EUR 300 | EUR -90 | bigger batches: interf_u 1->2, tinytapeout-demo 1->2; the station keeps its pins: the fixtures you have still plug in |
| U5 | station-1: 1 more I2C (4 -> 5) | +0 | +2 | EUR 67 | EUR 276 | EUR -209 | bigger batches: sensor_05 4->5, sensor_17 4->5; more I2C than limits.i2c (4); the station keeps its pins: the fixtures you have still plug in |
| U6 | station-1: 1 more UART (4 -> 5) | +0 | +3 | EUR 101 | EUR 411 | EUR -310 | bigger batches: sensor_13 4->5, sensor_21 4->5, sensor_23 4->5; more UART than limits.uart (4); the station keeps its pins: the fixtures you have still plug in |
| U7 | station-1: 1 more RS-485 (2 -> 3) | +0 | +9 | EUR 552 | EUR 960 | EUR -408 | bigger batches: rs485_io_01 2->3, rs485_io_03 2->3, rs485_io_05 2->3, rs485_io_06 2->3 +5; more RS-485 than limits.rs485 (2); the station keeps its pins: the fixtures you have still plug in |
| U8 | station-1: 1 more USB (2 -> 3) | +0 | +9 | EUR 552 | EUR 970 | EUR -418 | bigger batches: usb_dongle_01 2->3, usb_dongle_02 2->3, usb_dongle_03 2->3, usb_dongle_04 2->3 +5; more USB than limits.usb (2); the station keeps its pins: the fixtures you have still plug in |
| U9 | station-1: 1 more switched supplies (8 -> 9) | +0 | +2 | EUR 35 | EUR 510 | EUR -475 | bigger batches: sensor_12 8->9, usb_dongle_07 8->9; more supplies than limits.supplies (8); the station keeps its pins: the fixtures you have still plug in |
| U10 | station-1: plate 300 x 250 mm (now 300 x 220 mm) | +0 | +28 | EUR 1,004 | EUR 2,330 | EUR -1,326 | bigger batches: sensor_04 4->8, sensor_25 4->8, gateway_01 2->4, gateway_08 2->4 +13; the station keeps its pins: the fixtures you have still plug in |
| U11 | station-1: plate 350 x 250 mm (now 300 x 220 mm) | +0 | +45 | EUR 1,595 | EUR 4,016 | EUR -2,421 | bigger batches: sensor_04 4->8, sensor_25 4->8, StickHub 6->8, ecc83-pp 4->6 +24; the station keeps its pins: the fixtures you have still plug in |
| U12 | station-1: plate 400 x 300 mm (now 300 x 220 mm) | +0 | +79 | EUR 2,923 | EUR 5,532 | EUR -2,609 | bigger batches: ecc83-pp_v2 4->10, CM5_MINIMA_3 3->8, ecc83-pp 4->8, sensor_04 4->8 +34; the station keeps its pins: the fixtures you have still plug in |

## Path: best value (`--path value`)

1. supplies up to 48 V (now 24 V): +3 boards, +11 per load, net EUR 421

In all: +3 boards that fit, +11 boards per load, costs EUR 1,065, net EUR 421; then 122 of 128 boards fit.

## Path: most boards (`--path fit`)

1. supplies up to 48 V (now 24 V): +3 boards, +11 per load, net EUR 421

In all: +3 boards that fit, +11 boards per load, costs EUR 1,065, net EUR 421; then 122 of 128 boards fit.

## Path: biggest batches (`--path batch`)

1. station-1: plate 400 x 300 mm (now 300 x 220 mm): +0 boards, +79 per load, net EUR -2,609
2. supplies up to 48 V (now 24 V): +3 boards, +12 per load, net EUR 450
3. station-1: 1 more RS-485 (2 -> 3): +0 boards, +11 per load, net EUR -495
4. station-1: 1 more USB (2 -> 3): +0 boards, +11 per load, net EUR -504
5. station-1: 1 more Ethernet (2 -> 3): +0 boards, +7 per load, net EUR -350
6. station-1: 1 more RS-485 (3 -> 4): +0 boards, +11 per load, net EUR -856
7. station-1: 1 more USB (3 -> 4): +0 boards, +11 per load, net EUR -866
8. station-1: 1 more Ethernet (3 -> 4): +0 boards, +7 per load, net EUR -579

In all: +3 boards that fit, +149 boards per load, costs EUR 10,262, net EUR -2,764; then 122 of 128 boards fit.

---

- Estimates from the board profiles: a build checks every board with its real fixture layout.
- Worth: a board type that fits counts EUR 300.0 (value_per_board) x its priority, a batch of n 1 + 0.35 x log2(n) of that; costs are rough list prices (spec costs).
