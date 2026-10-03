"""把 Tesseract-OCR 目录加入当前用户 PATH（写 HKCU\\Environment，并广播 WM_SETTINGCHANGE）。"""
import ctypes
import winreg

TESS = r"C:\Program Files\Tesseract-OCR"

key = winreg.OpenKey(
    winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
)
try:
    path, vtype = winreg.QueryValueEx(key, "Path")
except FileNotFoundError:
    path, vtype = "", winreg.REG_EXPAND_SZ

entries = [e for e in path.split(";") if e.strip()]
norm = TESS.rstrip("\\").lower()
if any(e.rstrip("\\").lower() == norm for e in entries):
    print("already in user PATH")
else:
    entries.append(TESS)
    winreg.SetValueEx(key, "Path", 0, vtype, ";".join(entries))
    print("added to user PATH")
winreg.CloseKey(key)

HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
result = ctypes.c_ulong()
ctypes.windll.user32.SendMessageTimeoutW(
    HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
    SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
)
print("broadcast sent")

key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ)
final, _ = winreg.QueryValueEx(key, "Path")
winreg.CloseKey(key)
print("verify:", [e for e in final.split(";") if "Tesseract" in e])
