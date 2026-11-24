"""KiCad PCB-editor action plugin (SWIG API: KiCad 6, 7, 8, 9 and 10)."""
from __future__ import annotations

import os

import pcbnew
import wx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PCBTestKitPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "PCB TestKit - production test and jigs"
        self.category = "Manufacturing"
        self.description = ("Find production / life-cycle weak points, export a good-unit spec and test scheme, "
                            "generate a pogo-pin fixture board and a 3D-printable solder jig.")
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(ROOT, "resources", "icon.png")
        self.dark_icon_file_name = os.path.join(ROOT, "resources", "icon_dark.png")

    def Run(self):
        board = pcbnew.GetBoard()
        path = board.GetFileName() if board else ""
        parent = wx.FindWindowByName("PcbFrame")
        if not path or not os.path.exists(path):
            wx.MessageBox("Save the board first - PCB TestKit reads the saved .kicad_pcb file.", "PCB TestKit")
            return
        from .dialog import MainDialog
        try:
            dlg = MainDialog(parent, path)
        except Exception:
            import traceback
            wx.MessageBox(traceback.format_exc(), "PCB TestKit - could not read the board", wx.ICON_ERROR)
            return
        dlg.ShowModal()
        dlg.Destroy()
