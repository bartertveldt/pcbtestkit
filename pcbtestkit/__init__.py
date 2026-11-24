"""PCB TestKit - production test & assembly toolkit for KiCad.

Library entry points::

    from pcbtestkit import Design, run_pipeline
    design = Design.load("my_board.kicad_pcb")
    result = run_pipeline(design, out_dir="production_test")

AI/agent entry points live in :mod:`pcbtestkit.agent` (tool registry),
:mod:`pcbtestkit.mcp_server` (Model Context Protocol server) and the
``pcbtestkit api`` command line.
"""
__version__ = "1.0.0"

from .project import Design  # noqa: E402,F401


def run_pipeline(design, out_dir=None, **kw):  # noqa: D401 - thin re-export
    from .pipeline import run_pipeline as _rp
    return _rp(design, out_dir=out_dir, **kw)
