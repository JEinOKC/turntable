# Writes an invisible cursor theme: every cursor is a single transparent pixel.
import os, struct
d = os.path.expanduser("~/.icons/blank/cursors")
os.makedirs(d, exist_ok=True)
with open(os.path.expanduser("~/.icons/blank/index.theme"), "w") as f:
    f.write("[Icon Theme]\nName=blank\nComment=Invisible cursor for the turntable screen\n")
TYPE = 0xFFFD0002
image = struct.pack("<9I", 36, TYPE, 24, 1, 1, 1, 0, 0, 0) + struct.pack("<I", 0)
data = struct.pack("<4I", 0x72756358, 16, 0x10000, 1) + struct.pack("<3I", TYPE, 24, 16 + 12) + image
names = """default left_ptr arrow pointer hand1 hand2 text xterm ibeam watch wait progress left_ptr_watch
crosshair cross grab grabbing openhand closedhand move fleur all-scroll col-resize row-resize
n-resize s-resize e-resize w-resize ne-resize nw-resize se-resize sw-resize ew-resize ns-resize
nesw-resize nwse-resize not-allowed no-drop help question_arrow context-menu cell vertical-text
alias copy zoom-in zoom-out top_side bottom_side left_side right_side top_left_corner
top_right_corner bottom_left_corner bottom_right_corner sb_h_double_arrow sb_v_double_arrow""".split()
for n in names:
    with open(os.path.join(d, n), "wb") as f:
        f.write(data)
print(len(names), "cursors")
