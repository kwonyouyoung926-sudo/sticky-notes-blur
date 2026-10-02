# -*- coding: utf-8 -*-
"""StickyLock — Windows 스티커 메모를 블러로 가리고, 비밀번호를 입력한 메모만 보여 줍니다.

설치할 것도, 인터넷 연결도 필요 없습니다. 비밀번호는 이 컴퓨터의
%APPDATA%\\StickyLock\\config.json 에 해시로만 저장됩니다.

사용법은 README.md 를, 설정은 아래 "설정" 부분을 보세요.
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox

# ───────────────────────── 설정 (여기만 바꾸면 됩니다) ─────────────────────────
AUTO_LOCK_SECONDS = 60       # 열린 메모를 이 시간(초) 동안 안 쓰면 자동으로 다시 잠금 (0 = 끄기)
MIN_PASSWORD_LEN = 2         # 비밀번호 최소 길이
MAX_FAILS = 5                # 연속으로 틀릴 수 있는 횟수
FAIL_COOLDOWN = 30           # 횟수를 넘기면 기다려야 하는 시간(초)

BLUR_TINT = 0x66F2F2F2       # 메모를 덮는 블러 색 (0xAABBGGRR: 앞 2자리가 진하기)
GLASS_TINT = 0x50505050      # 잠금 버튼(유리) 색
GLASS_HOVER = 0x80404040     # 잠금 버튼에 마우스를 올렸을 때

FONT = "Malgun Gothic"       # 글꼴
# ──────────────────────────────────────────────────────────────────────────────

POLL_MS = 120                # 메모 창 위치를 확인하는 간격
IDLE_POLL_MS = 250           # 스티커 메모가 꺼져 있을 때
FAST_POLL_MS = 30            # 툴바로 메모를 막 켠 직후

NOTE_TITLES = {"스티커 메모", "Sticky Notes"}
NOTES_EXE = "microsoft.notes.exe"
NOTES_APP_ID = "Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe!App"

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "StickyLock")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

CARD_BG, TEXT, SUB, ACCENT, ERROR = "#FFFFFF", "#2B2B2B", "#6B6B6B", "#4A6CF7", "#D14343"

# ═══════════════════════════════ 윈도우 API ═══════════════════════════════
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 고해상도 화면에서 위치가 어긋나지 않도록
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

user32, kernel32, dwmapi = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.dwmapi
for fn in ("GetParent", "GetForegroundWindow", "GetAncestor", "GetWindow"):
    getattr(user32, fn).restype = wt.HWND
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
SetWindowLongPtr = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
SetWindowLongPtr.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]

GWLP_HWNDPARENT, GA_ROOT, GW_HWNDPREV = -8, 2, 3
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
SWP_KEEP = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED, DWMWA_CORNER = 9, 14, 33
EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


class _Accent(ctypes.Structure):
    _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                ("color", ctypes.c_uint), ("anim", ctypes.c_int)]


class _CompAttr(ctypes.Structure):
    _fields_ = [("attr", ctypes.c_int), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]


def apply_glass(hwnd, tint):
    """창 뒤쪽을 실시간으로 흐리게 만든다 (윈도우 10/11 아크릴). 모서리도 둥글게."""
    accent = _Accent(4, 2, tint, 0)   # 4 = ACCENT_ENABLE_ACRYLICBLURBEHIND
    attr = _CompAttr(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent))
    user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(attr))
    round_corners(hwnd)


def round_corners(hwnd):
    dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_CORNER, ctypes.byref(ctypes.c_int(2)), 4)


def hwnd_of(window):
    """tkinter 창의 윈도우 핸들."""
    window.update_idletasks()
    return user32.GetParent(window.winfo_id())


def window_rect(hwnd):
    """눈에 보이는 창 영역 (그림자 같은 여백 제외)."""
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _text_of(hwnd, fn):
    buf = ctypes.create_unicode_buffer(256)
    fn(hwnd, buf, 256)
    return buf.value


def window_title(hwnd):
    return _text_of(hwnd, user32.GetWindowTextW)


def window_class(hwnd):
    return _text_of(hwnd, user32.GetClassNameW)


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_exe_cache = {}


def process_exe(pid):
    """프로세스 실행 파일 이름 (예: microsoft.notes.exe)."""
    if pid not in _exe_cache:
        name, handle = "", kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if handle:
            buf, size = ctypes.create_unicode_buffer(1024), wt.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
            kernel32.CloseHandle(handle)
        _exe_cache[pid] = name
    return _exe_cache[pid]


def is_hidden(hwnd):
    cloaked = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4)
    return bool(cloaked.value) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd)


def _has_notes_child(hwnd):
    """창 안에 스티커 메모 프로세스의 화면이 들어 있는지 (창 제목이 다른 언어일 때 대비)."""
    found = []

    def visit(child, _):
        if process_exe(window_pid(child)) == NOTES_EXE:
            found.append(child)
            return False                          # 하나 찾으면 그만
        return True

    user32.EnumChildWindows(hwnd, EnumProc(visit), 0)
    return bool(found)


def _is_note_window(hwnd):
    cls = window_class(hwnd)
    if cls == "ApplicationFrameWindow":           # 스토어 앱 버전 (대부분 여기에 해당)
        return window_title(hwnd) in NOTE_TITLES or _has_notes_child(hwnd)
    return process_exe(window_pid(hwnd)) == NOTES_EXE and cls not in ("IME", "MSCTFIME UI")


def find_notes():
    """화면에 보이는 스티커 메모 창 → {창 핸들: (좌, 상, 우, 하)}"""
    notes = {}

    def visit(hwnd, _):
        if not is_hidden(hwnd) and _is_note_window(hwnd):
            l, t, r, b = window_rect(hwnd)
            if r - l > 40 and b - t > 40:
                notes[hwnd] = (l, t, r, b)
        return True

    user32.EnumWindows(EnumProc(visit), 0)
    return notes


def stack_above(note, windows):
    """windows(위→아래 순서)를 메모 창 바로 위에 끼워 넣는다."""
    chain = [note] + list(reversed(windows))
    if all(user32.GetWindow(chain[i], GW_HWNDPREV) == chain[i + 1] for i in range(len(chain) - 1)):
        return                                   # 이미 올바른 순서
    after = user32.GetWindow(note, GW_HWNDPREV)
    while after in windows:
        after = user32.GetWindow(after, GW_HWNDPREV)
    for win in windows:
        user32.SetWindowPos(win, after, 0, 0, 0, 0, SWP_KEEP)
        after = win


# ═══════════════════════════ 비밀번호 저장 (로컬 파일) ═══════════════════════════
def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(**values):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    config = load_config()
    config.update(values)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f)


def save_password(password):
    salt = os.urandom(16)
    save_config(salt=salt.hex(), hash=_hash(password, salt), len=len(password))


def check_password(password):
    config = load_config()
    if "hash" not in config:
        return False
    return hmac.compare_digest(_hash(password, bytes.fromhex(config["salt"])), config["hash"])


def has_password():
    return "hash" in load_config()


# ───────── 시작프로그램 등록 (바로가기 하나를 만들거나 지운다) ─────────
def _startup_link():
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup\StickyLock.lnk")


def autostart_enabled():
    return os.path.exists(_startup_link())


def set_autostart(enable):
    """윈도우를 켤 때 자동 실행할지 설정. exe로 실행 중이면 exe를, 아니면 파이썬을 등록한다."""
    link = _startup_link()
    if not enable:
        ps = f"Remove-Item -LiteralPath '{link}' -ErrorAction SilentlyContinue"
    else:
        if getattr(sys, "frozen", False):          # exe로 빌드된 경우
            target, args = sys.executable, ""
        else:
            target = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            args = f'"{os.path.abspath(__file__)}"'
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
              "$s.TargetPath='{target}';$s.Arguments='{args}';"
              "$s.WorkingDirectory='{home}';$s.Save()").format(
            link=link, target=target, args=args, home=os.path.dirname(target))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   creationflags=0x08000000, check=False)
    return autostart_enabled()


def password_length():
    """잠금 화면에 보여 줄 입력 칸 수."""
    return max(1, min(int(load_config().get("len", MIN_PASSWORD_LEN)), 12))


# ═══════════════════════════════ 잠금 화면 ═══════════════════════════════
class Overlay:
    """메모 하나를 덮는 블러 창 + 가운데 비밀번호 입력 칸."""

    def __init__(self, app, note):
        self.app, self.note = app, note
        self.typed, self.drag, self.showing_error = "", None, False

        # 블러 창: 검은색으로 칠한 부분이 "뒤가 비치는 유리"가 된다
        self.blur = tk.Toplevel(app.root)
        self.blur.overrideredirect(True)
        self.blur.configure(bg="black", cursor="fleur")
        self.blur.bind("<ButtonPress-1>", self.drag_start)
        self.blur.bind("<B1-Motion>", self.drag_move)
        self.blur.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))

        # 입력 칸은 별도의 작은 창 (유리 창 위에 그린 글자는 투명해지기 때문)
        self.card = tk.Toplevel(app.root)
        self.card.overrideredirect(True)
        self.card.configure(bg=CARD_BG)
        self.card.bind("<Key>", self.on_key)
        self.slots = self._build_slots(app.scale)

        self.hwnd, self.card_hwnd = hwnd_of(self.blur), hwnd_of(self.card)
        apply_glass(self.hwnd, BLUR_TINT)
        round_corners(self.card_hwnd)
        self.card.update_idletasks()
        self.card_w, self.card_h = self.card.winfo_reqwidth(), self.card.winfo_reqheight()
        SetWindowLongPtr(self.card_hwnd, GWLP_HWNDPARENT, self.hwnd)   # 카드는 항상 블러 위에
        self.rect, self.visible = None, True

    def _build_slots(self, scale):
        """비밀번호 자리수만큼 '_ _' 칸을 만든다."""
        box = tk.Frame(self.card, bg=CARD_BG, padx=14, pady=10)
        box.pack()
        slots = []
        for _ in range(password_length()):
            cell = tk.Frame(box, bg=CARD_BG)
            cell.pack(side="left", padx=int(6 * scale))
            dot = tk.Label(cell, text=" ", font=(FONT, 16, "bold"), bg=CARD_BG, fg=TEXT, width=2)
            dot.pack()
            bar = tk.Frame(cell, bg=TEXT, height=max(2, int(2 * scale)), width=int(24 * scale))
            bar.pack()
            slots.append((dot, bar))
        for widget in (self.card, box, *(w for pair in slots for w in pair)):
            widget.bind("<Button-1>", lambda e: self.focus())
        return slots

    # ── 입력 ──
    def focus(self):
        self.card.focus_force()

    def on_key(self, event):
        if event.keysym == "BackSpace":
            self.typed = self.typed[:-1]
        elif event.keysym == "Escape":
            self.typed = ""
        elif event.keysym in ("Return", "KP_Enter"):
            if self.typed:
                self.submit()
            return
        elif event.char and event.char.isprintable():
            self.typed += event.char
        self.redraw()
        if len(self.typed) >= len(self.slots):
            self.card.after(80, self.submit)     # 마지막 ●가 보이도록 잠깐 뒤에 확인

    def submit(self):
        password, self.typed = self.typed, ""
        if password:
            self.app.try_unlock(password, self)
        if not self.showing_error:
            self.redraw()

    def redraw(self, color=TEXT):
        for i, (dot, bar) in enumerate(self.slots):
            dot.configure(text="●" if i < len(self.typed) else " ", fg=color)
            bar.configure(bg=color)

    def flash_error(self):
        """틀렸을 때 밑줄을 잠깐 빨갛게."""
        self.showing_error = True
        self.redraw(ERROR)

        def reset():
            self.showing_error = False
            self.redraw()

        self.card.after(700, reset)

    # ── 메모 창 따라다니기 ──
    def drag_start(self, event):
        self.focus()
        r = wt.RECT()
        user32.GetWindowRect(self.note, ctypes.byref(r))
        self.drag = (event.x_root, event.y_root, r.left, r.top)

    def drag_move(self, event):
        if not self.drag:
            return
        start_x, start_y, note_x, note_y = self.drag
        user32.SetWindowPos(self.note, None, note_x + event.x_root - start_x,
                            note_y + event.y_root - start_y, 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        self.place(window_rect(self.note))

    def place(self, rect):
        if rect == self.rect:
            return
        l, t, r, b = rect
        user32.SetWindowPos(self.hwnd, None, l, t, r - l, b - t, SWP_NOACTIVATE | SWP_NOZORDER)
        if r - l >= self.card_w and b - t >= self.card_h:      # 메모 한가운데에 입력 칸
            if self.card.state() == "withdrawn":
                self.card.deiconify()
                SetWindowLongPtr(self.card_hwnd, GWLP_HWNDPARENT, self.hwnd)
            user32.SetWindowPos(self.card_hwnd, None, l + (r - l - self.card_w) // 2,
                                t + (b - t - self.card_h) // 2, self.card_w, self.card_h,
                                SWP_NOACTIVATE | SWP_NOZORDER)
        else:
            self.card.withdraw()                                # 메모가 너무 작으면 칸은 숨김
        self.rect = rect

    def show(self, rect):
        if not self.visible:
            self.blur.deiconify()
            SetWindowLongPtr(self.card_hwnd, GWLP_HWNDPARENT, self.hwnd)
            self.visible, self.rect = True, None
        self.place(rect)
        stack_above(self.note, [self.card_hwnd, self.hwnd])     # 메모 바로 위에만

    def hide(self):
        if self.visible:
            self.card.withdraw()
            self.blur.withdraw()
            self.visible = False

    def raise_note(self):
        """뒤에 있던 메모를 클릭했을 때, 메모 본체도 같이 앞으로."""
        if user32.GetWindow(self.note, GW_HWNDPREV) != self.hwnd:
            user32.SetWindowPos(self.hwnd, self.card_hwnd, 0, 0, 0, 0, SWP_KEEP)
            user32.SetWindowPos(self.note, self.hwnd, 0, 0, 0, 0, SWP_KEEP)

    def destroy(self):
        self.card.destroy()
        self.blur.destroy()


class LockButton:
    """열린 메모 오른쪽 아래에 뜨는 유리 자물쇠 버튼."""

    SIZE = 34

    def __init__(self, app, note):
        self.app, self.note = app, note
        scale = app.scale
        self.w = self.h = int(self.SIZE * scale)
        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        canvas = tk.Canvas(self.win, width=self.w, height=self.h, bg="black",
                           highlightthickness=0, bd=0, cursor="hand2")
        canvas.pack()
        self._draw_lock(canvas, scale)
        canvas.bind("<Button-1>", lambda e: app.lock_note(self.note))
        canvas.bind("<Button-3>", self.popup_menu)
        canvas.bind("<Enter>", lambda e: apply_glass(self.hwnd, GLASS_HOVER))
        canvas.bind("<Leave>", lambda e: apply_glass(self.hwnd, GLASS_TINT))
        self.hwnd = hwnd_of(self.win)
        apply_glass(self.hwnd, GLASS_TINT)
        self.rect = None

    def _draw_lock(self, canvas, scale):
        ink = "#FFFFFF"
        cx, top = self.w / 2, self.h / 2 - scale          # 자물쇠 몸통 위쪽
        body_w, body_h = 7 * scale, 6 * scale
        ring, line = 4.6 * scale, max(2, round(1.8 * scale))
        canvas.create_arc(cx - ring, top - 2 * ring + scale, cx + ring, top + scale,
                          start=0, extent=180, style="arc", outline=ink, width=line)
        for side in (-ring, ring):
            canvas.create_line(cx + side, top - ring + scale, cx + side, top + 1, fill=ink, width=line)
        canvas.create_rectangle(cx - body_w, top, cx + body_w, top + 2 * body_h, fill=ink, outline=ink)
        key = 1.5 * scale                                  # 열쇠 구멍 (뚫려서 유리가 비침)
        canvas.create_oval(cx - key, top + body_h - 1.5 * key, cx + key, top + body_h + 0.5 * key,
                           fill="black", outline="black")

    def popup_menu(self, event):
        menu = tk.Menu(self.win, tearoff=0, font=(FONT, 10))
        menu.add_command(label="이 메모 잠그기", command=lambda: self.app.lock_note(self.note))
        menu.add_command(label="모든 메모 잠그기", command=self.app.lock_all)
        self.app.add_common_menu(menu)
        menu.tk_popup(event.x_root, event.y_root)

    def place(self, rect):
        if rect != self.rect:
            _, _, right, bottom = rect
            pad = int(10 * self.app.scale)
            user32.SetWindowPos(self.hwnd, None, right - self.w - pad, bottom - self.h - pad,
                                self.w, self.h, SWP_NOACTIVATE | SWP_NOZORDER)
            self.rect = rect
        stack_above(self.note, [self.hwnd])

    def destroy(self):
        self.win.destroy()


class Toolbar:
    """항상 떠 있는 작은 툴바. 끌어서 옮길 수 있고 위치는 기억된다."""

    BG, FG, DIM, BTN = "#1F2430", "#E8EAF0", "#9AA3B5", "#2E3545"

    def __init__(self, app):
        self.app = app
        self.collapsed, self.drag = False, None
        self.pos = load_config().get("toolbar_pos")

        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=self.BG)

        self.bar = tk.Frame(self.win, bg=self.BG, padx=10, pady=5)
        grip = tk.Label(self.bar, text="⠿", font=(FONT, 11), bg=self.BG, fg=self.DIM, padx=2)
        grip.pack(side="left", padx=(0, 6))
        title = tk.Label(self.bar, text="🔒 StickyLock", font=(FONT, 10, "bold"), bg=self.BG, fg=self.FG)
        title.pack(side="left")
        self.status = tk.Label(self.bar, text="", font=(FONT, 9), bg=self.BG, fg=self.DIM, padx=10)
        self.status.pack(side="left")
        self._button("메모 열기", app.open_notes, "#3B6E4B")
        self._button("모두 잠그기", app.lock_all, ACCENT)
        self._button("⚙", self.popup_menu)       # 비밀번호 변경 · 자동 실행 · 종료
        self._button("▾", self.toggle)
        self.mini = tk.Label(self.win, text="🔒", font=(FONT, 10), bg=self.BG, fg=self.FG,
                             padx=10, pady=3, cursor="hand2")

        # 버튼이 아닌 곳을 끌면 툴바가 움직인다. 제목을 더블클릭하면 원래 자리로.
        for widget in (self.bar, grip, title, self.status, self.mini):
            widget.bind("<ButtonPress-1>", self.drag_start, add="+")
            widget.bind("<B1-Motion>", self.drag_move, add="+")
            widget.bind("<ButtonRelease-1>", self.drag_end, add="+")
            if widget is not self.mini:
                widget.configure(cursor="fleur")
        title.bind("<Double-Button-1>", lambda e: self.reset_position())

        self.bar.pack()
        self.hwnd = hwnd_of(self.win)
        round_corners(self.hwnd)
        self.reposition()

    def _button(self, text, command, bg=None):
        btn = tk.Label(self.bar, text=text, font=(FONT, 9), bg=bg or self.BTN, fg="white",
                       padx=9, pady=2, cursor="hand2")
        btn.pack(side="left", padx=(6, 0))
        btn.bind("<Button-1>", lambda e: command())

    def popup_menu(self):
        menu = tk.Menu(self.win, tearoff=0, font=(FONT, 10))
        self.app.add_common_menu(menu)
        menu.tk_popup(*self.win.winfo_pointerxy())

    def drag_start(self, event):
        self.drag = [event.x_root, event.y_root, self.win.winfo_x(), self.win.winfo_y(), False]

    def drag_move(self, event):
        if not self.drag:
            return
        start_x, start_y, win_x, win_y, moved = self.drag
        dx, dy = event.x_root - start_x, event.y_root - start_y
        if not moved and abs(dx) + abs(dy) < 4:          # 살짝 떨린 건 클릭으로 본다
            return
        self.drag[4] = True
        self.win.geometry(f"+{win_x + dx}+{win_y + dy}")

    def drag_end(self, event):
        drag, self.drag = self.drag, None
        if drag and drag[4]:
            self.pos = [self.win.winfo_x(), self.win.winfo_y()]
            save_config(toolbar_pos=self.pos)
        elif event.widget is self.mini:
            self.toggle()                                 # 접힌 상태에서 클릭 = 펼치기

    def reset_position(self):
        self.pos = None
        save_config(toolbar_pos=None)
        self.reposition()

    def toggle(self):
        self.collapsed = not self.collapsed
        (self.bar if self.collapsed else self.mini).pack_forget()
        (self.mini if self.collapsed else self.bar).pack()
        self.reposition()

    def reposition(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        if self.pos:        # 옮겨 둔 자리 (화면 밖이면 안쪽으로 당김)
            vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
            vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
            x, y = min(max(self.pos[0], vx), vx + vw - w), min(max(self.pos[1], vy), vy + vh - h)
        else:               # 기본 자리 = 작업 표시줄 바로 위, 가운데
            area = wt.RECT()
            user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)   # SPI_GETWORKAREA
            x = area.left + (area.right - area.left - w) // 2
            y = area.bottom - h - int(6 * self.app.scale)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def update(self, locked, unlocked):
        text = f"잠김 {locked} · 열림 {unlocked}" if locked or unlocked else "메모 없음"
        if self.status.cget("text") != text:
            self.status.configure(text=text)
            if not self.collapsed:
                self.reposition()
        self.win.attributes("-topmost", True)


# ═══════════════════════════════ 본체 ═══════════════════════════════
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("StickyLock")
        self.scale = self.root.winfo_fpixels("1i") / 96.0   # 화면 배율 (125%, 150% …)
        self.overlays, self.buttons = {}, {}                # 메모 창 핸들 → 잠금 화면 / 버튼
        self.unlocked, self.last_active = set(), {}         # 메모마다 따로 열고 잠근다
        self.fails, self.block_until, self.fast_until = 0, 0, 0

        if not has_password() and not self.ask_password("StickyLock 비밀번호 설정", change=False):
            sys.exit(0)
        self.toolbar = Toolbar(self)
        self.tick()

    def run(self):
        self.root.mainloop()

    # ── 비밀번호 ──
    def ask_password(self, title, change):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(bg=CARD_BG, padx=24, pady=18)
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)
        tk.Label(dialog, text=title, font=(FONT, 13, "bold"), bg=CARD_BG, fg=TEXT).pack(anchor="w", pady=(0, 10))

        fields = []
        for label in (["현재 비밀번호"] if change else []) + ["새 비밀번호", "새 비밀번호 확인"]:
            tk.Label(dialog, text=label, font=(FONT, 9), bg=CARD_BG, fg=SUB).pack(anchor="w")
            entry = tk.Entry(dialog, show="●", width=28, font=(FONT, 11), relief="solid", bd=1)
            entry.pack(pady=(2, 8), ipady=3)
            fields.append(entry)
        error = tk.Label(dialog, text="", font=(FONT, 9), bg=CARD_BG, fg=ERROR)
        error.pack(anchor="w")
        done = {"ok": False}

        def save(*_):
            values = [f.get() for f in fields]
            if change and not check_password(values.pop(0)):
                return error.configure(text="현재 비밀번호가 틀렸습니다.")
            new, again = values
            if len(new) < MIN_PASSWORD_LEN:
                return error.configure(text=f"{MIN_PASSWORD_LEN}자 이상 입력하세요.")
            if new != again:
                return error.configure(text="새 비밀번호가 서로 다릅니다.")
            save_password(new)
            done["ok"] = True
            dialog.destroy()

        tk.Button(dialog, text="저장", font=(FONT, 10, "bold"), bg=ACCENT, fg="white",
                  relief="flat", padx=16, command=save).pack(anchor="e", pady=(6, 0))
        dialog.bind("<Return>", save)
        dialog.update_idletasks()
        dialog.geometry(f"+{(dialog.winfo_screenwidth() - dialog.winfo_width()) // 2}"
                        f"+{(dialog.winfo_screenheight() - dialog.winfo_height()) // 3}")
        dialog.focus_force()
        fields[0].focus_set()
        dialog.grab_set()
        self.root.wait_window(dialog)
        return done["ok"]

    def add_common_menu(self, menu):
        """자물쇠 버튼과 툴바가 함께 쓰는 메뉴 항목."""
        menu.add_command(label="비밀번호 변경", command=self.change_password)
        self._autostart_var = tk.BooleanVar(value=autostart_enabled())   # 메뉴가 닫힐 때까지 유지
        menu.add_checkbutton(label="윈도우 시작할 때 자동 실행", command=self.toggle_autostart,
                             variable=self._autostart_var)
        menu.add_separator()
        menu.add_command(label="StickyLock 종료", command=self.quit)

    def toggle_autostart(self):
        enabled = set_autostart(not autostart_enabled())
        messagebox.showinfo("StickyLock", "윈도우를 켤 때 자동으로 실행됩니다." if enabled
                            else "자동 실행을 해제했습니다.")

    def change_password(self):
        if self.ask_password("비밀번호 변경", change=True):
            for overlay in self.overlays.values():   # 입력 칸 수가 바뀔 수 있으니 다시 만든다
                overlay.destroy()
            self.overlays.clear()
            messagebox.showinfo("StickyLock", "비밀번호가 변경되었습니다.")

    # ── 잠금 / 해제 ──
    def try_unlock(self, password, overlay):
        now = time.time()
        if now < self.block_until:
            return overlay.flash_error()
        if check_password(password):
            self.fails = 0
            self.unlocked.add(overlay.note)          # 이 메모만 연다
            self.last_active[overlay.note] = now
            overlay.hide()
            user32.SetForegroundWindow(overlay.note)
        else:
            self.fails += 1
            if self.fails >= MAX_FAILS:
                self.fails, self.block_until = 0, now + FAIL_COOLDOWN
            overlay.flash_error()

    def lock_note(self, note):
        self.unlocked.discard(note)
        button = self.buttons.pop(note, None)
        if button:
            button.destroy()

    def lock_all(self):
        for note in list(self.unlocked):
            self.lock_note(note)

    def open_notes(self):
        """툴바에서 스티커 메모 켜기. 창이 뜨자마자 가려지도록 잠시 빠르게 확인한다."""
        try:
            subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + NOTES_APP_ID],
                             creationflags=0x08000000)       # CREATE_NO_WINDOW
            self.fast_until = time.time() + 10
        except Exception as e:
            messagebox.showerror("StickyLock", f"스티커 메모를 열지 못했습니다.\n{e}")

    def quit(self):
        if messagebox.askyesno("StickyLock", "StickyLock을 종료할까요?\n종료하면 메모가 보호되지 않습니다."):
            self.root.destroy()

    # ── 메모 창 따라다니기 (0.12초마다) ──
    def active_note(self, foreground, notes):
        """지금 쓰고 있는 창이 어느 메모의 것인지."""
        root = user32.GetAncestor(foreground, GA_ROOT) or foreground if foreground else None
        if root in notes:
            return root
        for note, overlay in self.overlays.items():
            if root in (overlay.hwnd, overlay.card_hwnd):
                return note
        for note, button in self.buttons.items():
            if root == button.hwnd:
                return note
        return None

    def tick(self):
        try:
            notes = find_notes()
            now = time.time()
            foreground = user32.GetForegroundWindow()

            for note in list(self.unlocked):          # 닫히거나 최소화된 메모는 다시 잠금
                if note not in notes:
                    self.lock_note(note)
            self.last_active = {k: v for k, v in self.last_active.items() if k in notes}

            active = self.active_note(foreground, notes)
            if active:
                self.last_active[active] = now
            if AUTO_LOCK_SECONDS:                     # 한동안 안 쓴 메모는 자동으로 다시 잠금
                for note in list(self.unlocked):
                    if now - self.last_active.get(note, now) > AUTO_LOCK_SECONDS:
                        self.lock_note(note)

            for note in list(self.overlays):          # 사라진 메모의 창 정리
                if note not in notes:
                    self.overlays.pop(note).destroy()
            for note in list(self.buttons):
                if note not in notes or note not in self.unlocked:
                    self.buttons.pop(note).destroy()

            for note, rect in notes.items():
                if note in self.unlocked:
                    if note in self.overlays:
                        self.overlays[note].hide()
                    if note not in self.buttons:
                        self.buttons[note] = LockButton(self, note)
                    self.buttons[note].place(rect)
                else:
                    if note not in self.overlays:
                        self.overlays[note] = Overlay(self, note)
                    overlay = self.overlays[note]
                    overlay.show(rect)
                    if active == note:                # 뒤에 있던 메모를 클릭한 경우
                        overlay.raise_note()

            open_count = len(self.unlocked & notes.keys())
            self.toolbar.update(len(notes) - open_count, open_count)
        except Exception as e:                        # 오류가 나도 앱이 멈추지 않도록
            print("tick error:", e, file=sys.stderr)

        try:
            if time.time() < self.fast_until:
                delay = FAST_POLL_MS
            elif self.overlays or self.buttons:
                delay = POLL_MS
            else:
                delay = IDLE_POLL_MS
            self.root.after(delay, self.tick)
        except tk.TclError:
            pass                                      # 종료됨


def already_running():
    kernel32.CreateMutexW(None, False, "StickyLock_SingleInstance_Mutex")
    return kernel32.GetLastError() == 183             # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if already_running():
        sys.exit(0)
    App().run()
