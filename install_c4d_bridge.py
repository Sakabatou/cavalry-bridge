# Cavalry Bridge setup - run from Extensions > User Scripts (installed there
# as "Cavalry Bridge Setup") or the Script Manager.
#
# Creates a "Cavalry Bridge" null carrying the Python Tag from
# c4d_bridge_tag.py, and links the currently selected objects into the tag's
# Objects list (C4D -> Cavalry).
#
# Shapes sent from Cavalry need no setup here: they arrive as Cavalry Spline
# objects from the cavalry_bridge plugin.
import os
import sys

import c4d

TAG_CODE_FILE = "c4d_bridge_tag.py"


def _add_ud(tag, dtype, name, value, gui=None):
    bc = c4d.GetCustomDataTypeDefault(dtype)
    bc[c4d.DESC_NAME] = name
    bc[c4d.DESC_SHORT_NAME] = name
    if gui is not None:
        bc[c4d.DESC_CUSTOMGUI] = gui
    desc_id = tag.AddUserData(bc)
    tag[desc_id] = value
    return desc_id


def build(doc, code, objects):
    null = c4d.BaseObject(c4d.Onull)
    null.SetName("Cavalry Bridge")
    tag = c4d.BaseTag(c4d.Tpython)
    tag.SetName("Cavalry Bridge")
    tag[c4d.TPYTHON_CODE] = code
    null.InsertTag(tag)

    # IDs must come out as 1..4 to match UD_* in the tag code.
    _add_ud(tag, c4d.DTYPE_BOOL, "Enabled", True)
    _add_ud(tag, c4d.DTYPE_STRING, "Host", "127.0.0.1")
    _add_ud(tag, c4d.DTYPE_LONG, "Port", 1234)
    inex = c4d.InExcludeData()
    for obj in objects:
        inex.InsertObject(obj, 1)
    _add_ud(tag, c4d.CUSTOMDATATYPE_INEXCLUDE_LIST, "Objects", inex)

    doc.StartUndo()
    doc.InsertObject(null)
    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, null)
    doc.EndUndo()
    return null, tag


def _tag_code_path():
    """c4d_bridge_tag.py ships inside the cavalry_bridge plugin folder; during
    development it sits next to this file."""
    candidates = [os.path.join(os.path.dirname(os.path.abspath(__file__)), TAG_CODE_FILE)]
    server = sys.modules.get("cavalry_bridge_server")
    if server is not None:
        candidates.insert(0, os.path.join(os.path.dirname(server.__file__), TAG_CODE_FILE))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def main():
    path = _tag_code_path()
    if path is None:
        c4d.gui.MessageDialog("Cavalry Bridge: the cavalry_bridge plugin isn't installed.\n"
                              "Run the installer and restart Cinema 4D.")
        return
    with open(path, "r", encoding="utf-8") as f:
        code = f.read()
    objects = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_TOPLEVEL)
    build(doc, code, objects)
    c4d.EventAdd()


if __name__ == "__main__":
    main()
