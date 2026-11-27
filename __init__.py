"""KiCad plugin entry point (SWIG action plugins, KiCad 6-10).

KiCad imports this package from its scripting/plugins folder and we register
the toolbar action. Outside KiCad (no pcbnew module) nothing happens, so the
folder can also be used as a normal Python project.
"""
try:
    import pcbnew  # noqa: F401
    _IN_KICAD = True
except ImportError:
    _IN_KICAD = False

if _IN_KICAD:
    try:
        from .plugin.action_plugin import PCBTestKitEnvironmentPlugin, PCBTestKitPlugin
        PCBTestKitPlugin().register()
        PCBTestKitEnvironmentPlugin().register()
    except Exception:  # never break KiCad's plugin loading
        import logging
        import traceback
        logging.getLogger("pcbtestkit").error("PCB TestKit failed to load:\n%s", traceback.format_exc())
