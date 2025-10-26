# rthook_modules_path.py
import os, sys
base = getattr(sys, "_MEIPASS", os.path.dirname(sys.argv[0]))
modules_dir = os.path.join(base, "modules")
if os.path.isdir(modules_dir) and modules_dir not in sys.path:
    sys.path.insert(0, modules_dir)
