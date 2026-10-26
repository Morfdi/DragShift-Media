# PyInstaller spec. ONEFILE=1 -> portable single exe; otherwise a folder for the installer.
import os
import sys
from PyInstaller.utils.hooks import collect_all

onefile = os.environ.get("ONEFILE") == "1"
heif_datas, heif_bins, heif_hidden = collect_all("pillow_heif")

# ffmpeg / ffprobe: bundled from dragshift/assets/ffmpeg/.
# Added via binaries (not datas) so PyInstaller keeps the exec bit on POSIX and
# re-signs them for macOS notarization. Destination "assets/ffmpeg" matches the
# first path that media._bundled() checks inside _MEIPASS.
_ff_dir = os.path.join("dragshift", "assets", "ffmpeg")
_ff_bins = []
if os.path.isdir(_ff_dir):
    for _f in os.listdir(_ff_dir):
        if _f.lower() in ("ffmpeg.exe", "ffprobe.exe", "ffmpeg", "ffprobe"):
            _ff_bins.append((os.path.join(_ff_dir, _f), "assets/ffmpeg"))

a = Analysis(
    ["run.py"],
    datas=[("dragshift/assets", "dragshift/assets")] + heif_datas,
    binaries=heif_bins + _ff_bins,
    hiddenimports=["win32com.client", "PySide6.QtNetwork", "AppKit", "Quartz", "objc"] + heif_hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf", "pandas", "yaml", "PySide6.QtSql",
              "PySide6.QtTest", "PySide6.QtOpenGL", "PySide6.QtSvgWidgets", "PySide6.QtDesigner",
              "matplotlib", "scipy"],
)

DROP = ("Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6WebEngine", "Qt63D", "Qt6Multimedia", "Qt6Charts",
        "Qt6VirtualKeyboard", "Qt6Designer", "Qt6ShaderTools", "Qt6Sql", "Qt6Test", "opengl32sw",
        "Qt6OpenGL", "Qt6DataVisualization", "Qt6Positioning", "Qt6Sensors", "Qt6SerialPort",
        "Qt6Bluetooth", "Qt6Nfc", "Qt6RemoteObjects", "Qt6Scxml", "Qt6SpatialAudio", "Qt6TextToSpeech",
        "Qt6WebChannel", "Qt6WebSockets", "Qt6Svg", "qtquick", "qml",
        "libgtk-3", "libgdk-3", "libqgtk3", "libatk-")
a.binaries = [b for b in a.binaries if not any(d.lower() in b[0].lower() for d in DROP)]

# Drop everything under dragshift/assets/ffmpeg/ — it already got in via binaries with
# the right dest ("assets/ffmpeg"), which is where media._bundled() looks. Keeping the
# datas copy would duplicate ~200 MB and lose the exec bit on POSIX.
a.datas = [d for d in a.datas
           if "translations" not in d[0].replace("\\", "/")
           and not d[0].replace("\\", "/").startswith("dragshift/assets/ffmpeg/")]

pyz = PYZ(a.pure)
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

common = dict(name="DragShift", console=False, upx=False)
if IS_MAC:
    common.update(icon="dragshift/assets/dragshift.icns")
elif IS_LINUX:
    common.update(name="dragshift")       # icon comes from the .desktop file / hicolor theme
else:
    common.update(icon="dragshift/assets/dragshift.ico", version="installer/version_info.txt")

if IS_MAC:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, argv_emulation=False, **common)
    coll = COLLECT(exe, a.binaries, a.datas, name="DragShift", upx=False)
    app = BUNDLE(
        coll,
        name="DragShift.app",
        icon="dragshift/assets/dragshift.icns",
        bundle_identifier="com.dragshift.app",
        info_plist={
            "CFBundleName": "DragShift",
            "CFBundleDisplayName": "DragShift",
            "CFBundleShortVersionString": "1.1.0",
            "CFBundleVersion": "1.1.0",
            "LSUIElement": True,                 # menu-bar app, no Dock icon
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Any file",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["public.item", "public.content", "public.data"],
            }],
        },
    )
elif IS_LINUX:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, name="dragshift", upx=False)
elif onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, name="DragShift", upx=False)