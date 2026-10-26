# PyInstaller recipe for the standalone wakit app:  pyinstaller packaging/wakit.spec
# One file per platform; double-clicked it opens the GUI, with arguments it is the CLI.
import os

root = os.path.dirname(SPECPATH)  # the repository
src = os.path.join(root, "src")

a = Analysis(
    [os.path.join(SPECPATH, "wakit_app.py")],
    pathex=[src],
    datas=[(os.path.join(src, "wakit", "web", "index.html"), "wakit/web")],
    # cli.py imports the commands by name at run time, so list them for PyInstaller
    hiddenimports=[
        "wakit.crypt", "wakit.db_merge", "wakit.doctor", "wakit.device", "wakit.export_import", "wakit.from_ios", "wakit.gui",
        "wakit.ios_backup", "wakit.ios_stickers", "wakit.sticker_merge", "wakit.to_ios"],
    excludes=["tkinter", "unittest", "pydoc_data"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="wakit", console=True, upx=False)
