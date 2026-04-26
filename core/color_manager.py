"""
Color manager: applies vibrance via NvAPI and brightness/contrast/gamma via SetDeviceGammaRamp.

IMPORTANT: an atexit hook is registered to release persistent display DCs.
Without it, abnormal termination (taskkill, crash, Explorer restart) can leave
the user's monitor in a tinted state until they restart the app and press Reset.

NvAPI vibrance:
    NvAPI exposes Digital Vibrance Control (DVC) via `nvapi_QueryInterface(id)` which
    returns a __cdecl function pointer. Loaded lazily; failures are non-fatal -
    `nvapi_available` is set to False and an explanation is stored in `nvapi_error`.

Gamma ramp:
    256 x 3 uint16 array passed to gdi32.SetDeviceGammaRamp on the primary display DC.
"""

import atexit
import ctypes
import os
from ctypes import wintypes
from typing import Optional


# ---------- NvAPI struct ----------

class _NV_DVC_INFO_V1(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("currentLevel", ctypes.c_int32),
        ("minLevel", ctypes.c_int32),
        ("maxLevel", ctypes.c_int32),
    ]


_NV_DVC_INFO_V1_VER = ctypes.sizeof(_NV_DVC_INFO_V1) | (1 << 16)


# Ex variant — same fields plus defaultLevel. Used by SetDVCLevelEx /
# GetDVCInfoEx, which exist on newer NvAPI builds and on some 5xx+ drivers
# work even when the legacy SetDVCLevel returns NVAPI_INVALID_ARGUMENT.
class _NV_DVC_INFO_EX(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("currentLevel", ctypes.c_int32),
        ("minLevel", ctypes.c_int32),
        ("maxLevel", ctypes.c_int32),
        ("defaultLevel", ctypes.c_int32),
    ]


_NV_DVC_INFO_EX_VER = ctypes.sizeof(_NV_DVC_INFO_EX) | (1 << 16)


# ---------- NvAPI status / function ids ----------

NVAPI_OK = 0

_FN_INITIALIZE = 0x0150E828
_FN_UNLOAD = 0xD22BDD7E
_FN_ENUM_DISPLAY = 0x9ABDD40D
_FN_SET_DVC_LEVEL = 0x172409B4    # legacy
_FN_GET_DVC_INFO = 0x4085DE45     # legacy
_FN_SET_DVC_LEVEL_EX = 0x4A82C2B1
_FN_GET_DVC_INFO_EX = 0x0E45002D


# NvAPI uses __cdecl. CFUNCTYPE matches that on x86 and is identical to WINFUNCTYPE on x64.
_PROTO_INITIALIZE = ctypes.CFUNCTYPE(ctypes.c_int)
_PROTO_ENUM_DISPLAY = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)
)
_PROTO_SET_DVC_LEVEL = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
_PROTO_GET_DVC_INFO = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_NV_DVC_INFO_V1)
)
_PROTO_SET_DVC_LEVEL_EX = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_NV_DVC_INFO_EX)
)
_PROTO_GET_DVC_INFO_EX = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_NV_DVC_INFO_EX)
)
_PROTO_UNLOAD = ctypes.CFUNCTYPE(ctypes.c_int)


def _try_load_nvapi_dll() -> Optional[ctypes.CDLL]:
    """Try System32 first (canonical NVIDIA install), fall back to default search."""
    is_x64 = ctypes.sizeof(ctypes.c_void_p) == 8
    dll_name = "nvapi64.dll" if is_x64 else "nvapi.dll"

    candidates = []
    sysroot = os.environ.get("SystemRoot") or "C:\\Windows"
    if is_x64:
        candidates.append(os.path.join(sysroot, "System32", dll_name))
    else:
        candidates.append(os.path.join(sysroot, "SysWOW64", dll_name))
    candidates.append(dll_name)  # default search path

    for path in candidates:
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    return None


class _NvAPI:
    """Lightweight NvAPI wrapper. Only DVC (Digital Vibrance) is exposed."""

    def __init__(self):
        self.available = False
        self.error: Optional[str] = None
        self.set_works_for_nonzero: Optional[bool] = None  # None = not probed yet
        self.using_ex_api: bool = False  # True when SetDVCLevelEx is used
        self._dll: Optional[ctypes.CDLL] = None
        self._display_handle: Optional[ctypes.c_void_p] = None
        self._SetDVCLevel = None
        self._GetDVCInfo = None
        self._SetDVCLevelEx = None
        self._GetDVCInfoEx = None
        self._Unload = None
        self.min_level = 0
        self.max_level = 63
        self.default_level = 0

        self._load()
        if self.available:
            self._probe_set()

    def _probe_set(self) -> None:
        """
        Find an NvAPI Set* call that the current driver actually accepts.

        Recent NVIDIA drivers (5xx+) regressed legacy SetDVCLevel and reject
        non-zero levels with NVAPI_INVALID_ARGUMENT. The newer SetDVCLevelEx
        sometimes still works on the same drivers.

        Order of preference:
          1. SetDVCLevelEx — modern, more likely to be honored on 5xx+.
          2. SetDVCLevel   — legacy fallback for older drivers.

        On success we restore the neutral level immediately, so the probe
        doesn't leave the screen visibly tinted.
        """
        if self._display_handle is None:
            return

        # Pick a probe level that's clearly different from both the default
        # AND from the min/max edges. Some drivers validate "diff from current"
        # and reject min+1 as too small; some reject the extreme max. Mid-range
        # away from default is the safest indicator that the driver actually
        # accepts arbitrary writes.
        mid = (self.min_level + self.max_level) // 2
        if mid == self.default_level:
            # Avoid landing exactly on the default — pick something off-center.
            mid = self.default_level + max(1, (self.max_level - self.min_level) // 4)
            mid = max(self.min_level, min(self.max_level, mid))
        test_level = mid

        # Try Ex first.
        if self._SetDVCLevelEx is not None:
            info = _NV_DVC_INFO_EX()
            info.version = _NV_DVC_INFO_EX_VER
            info.currentLevel = test_level
            try:
                rv = self._SetDVCLevelEx(self._display_handle, ctypes.byref(info))
            except OSError:
                rv = -1
            if rv == NVAPI_OK:
                # restore neutral via the same path
                info.currentLevel = self.default_level
                try:
                    self._SetDVCLevelEx(self._display_handle, ctypes.byref(info))
                except OSError:
                    pass
                self.set_works_for_nonzero = True
                self.using_ex_api = True
                return

        # Fall back to legacy SetDVCLevel.
        if self._SetDVCLevel is not None:
            try:
                rv = self._SetDVCLevel(self._display_handle, test_level)
                if rv == NVAPI_OK:
                    self._SetDVCLevel(self._display_handle, self.default_level)
                    self.set_works_for_nonzero = True
                    self.using_ex_api = False
                    return
            except OSError:
                pass

        self.set_works_for_nonzero = False

    def _safe_call(self, fn, *args, label: str = ""):
        """Wrap an NvAPI call so an SEH access violation becomes an explicit error."""
        try:
            return fn(*args), None
        except OSError as e:
            return None, f"{label or 'call'} failed: {e}"

    def _load(self):
        self._dll = _try_load_nvapi_dll()
        if self._dll is None:
            self.error = "nvapi64.dll not found (NVIDIA driver required)"
            return

        try:
            query_addr = ctypes.cast(self._dll.nvapi_QueryInterface, ctypes.c_void_p).value
        except AttributeError:
            self.error = "nvapi_QueryInterface export not found"
            return

        query = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_uint32)(query_addr)

        try:
            addr_init = query(_FN_INITIALIZE)
            addr_enum = query(_FN_ENUM_DISPLAY)
            addr_set = query(_FN_SET_DVC_LEVEL)
            addr_get = query(_FN_GET_DVC_INFO)
            addr_set_ex = query(_FN_SET_DVC_LEVEL_EX)
            addr_get_ex = query(_FN_GET_DVC_INFO_EX)
            addr_unload = query(_FN_UNLOAD)
        except OSError as e:
            self.error = f"nvapi_QueryInterface raised: {e}"
            return

        # We need at least one Set variant; either legacy or Ex is fine.
        if not addr_init or not addr_enum or not (addr_set or addr_set_ex):
            self.error = "NvAPI core functions not exported by this driver"
            return

        Initialize = _PROTO_INITIALIZE(addr_init)
        EnumDisplay = _PROTO_ENUM_DISPLAY(addr_enum)

        rv, err = self._safe_call(Initialize, label="NvAPI_Initialize")
        if err:
            self.error = err
            return
        if rv != NVAPI_OK:
            self.error = f"NvAPI_Initialize returned {rv}"
            return

        # Init succeeded. From this point on, any failure must call Unload to
        # avoid leaking driver state.
        if addr_unload:
            self._Unload = _PROTO_UNLOAD(addr_unload)

        handle = ctypes.c_void_p()
        rv, err = self._safe_call(EnumDisplay, 0, ctypes.byref(handle),
                                  label="NvAPI_EnumNvidiaDisplayHandle")
        if err:
            self.error = err
            self._unload_safely()
            return
        if rv != NVAPI_OK or not handle.value:
            self.error = f"no NVIDIA display found (status {rv})"
            self._unload_safely()
            return

        self._display_handle = handle
        if addr_set:
            self._SetDVCLevel = _PROTO_SET_DVC_LEVEL(addr_set)
        if addr_set_ex:
            self._SetDVCLevelEx = _PROTO_SET_DVC_LEVEL_EX(addr_set_ex)

        # Prefer the Ex info call — it returns defaultLevel which the legacy
        # call doesn't, and Ex tends to track the driver's current intended
        # range more accurately.
        if addr_get_ex:
            self._GetDVCInfoEx = _PROTO_GET_DVC_INFO_EX(addr_get_ex)
            info_ex = _NV_DVC_INFO_EX()
            info_ex.version = _NV_DVC_INFO_EX_VER
            rv, err = self._safe_call(self._GetDVCInfoEx, handle, ctypes.byref(info_ex),
                                      label="NvAPI_DISP_GetDVCInfoEx")
            if err is None and rv == NVAPI_OK:
                self.min_level = info_ex.minLevel
                self.max_level = info_ex.maxLevel
                self.default_level = info_ex.defaultLevel
        elif addr_get:
            self._GetDVCInfo = _PROTO_GET_DVC_INFO(addr_get)
            info = _NV_DVC_INFO_V1()
            info.version = _NV_DVC_INFO_V1_VER
            rv, err = self._safe_call(self._GetDVCInfo, handle, ctypes.byref(info),
                                      label="NvAPI_GetDVCInfo")
            if err is None and rv == NVAPI_OK:
                self.min_level = info.minLevel
                self.max_level = info.maxLevel

        self.available = True

    def _unload_safely(self) -> None:
        """Best-effort NvAPI_Unload; used to clean up after partial init."""
        if self._Unload is None:
            return
        try:
            self._Unload()
        except OSError:
            pass

    def set_vibrance_percent(self, percent: int) -> bool:
        """
        percent in 0..100 -> mapped to NvAPI internal range.
        Uses SetDVCLevelEx if probe found it works, otherwise legacy SetDVCLevel.
        """
        if not self.available or self._display_handle is None:
            return False
        percent = max(0, min(100, int(percent)))
        rng = self.max_level - self.min_level
        level = self.min_level + round(percent * rng / 100)

        if self.using_ex_api and self._SetDVCLevelEx is not None:
            info = _NV_DVC_INFO_EX()
            info.version = _NV_DVC_INFO_EX_VER
            info.currentLevel = level
            try:
                return self._SetDVCLevelEx(self._display_handle, ctypes.byref(info)) == NVAPI_OK
            except OSError:
                return False

        if self._SetDVCLevel is not None:
            try:
                return self._SetDVCLevel(self._display_handle, level) == NVAPI_OK
            except OSError:
                return False

        return False

    def shutdown(self):
        self._unload_safely()


# ---------- Gamma ramp via gdi32.SetDeviceGammaRamp ----------
#
# IMPORTANT: on Windows 10/11 GetDC(NULL) returns a desktop DC that is composited
# by DWM and silently rejects gamma operations. We must create a DC for the
# physical display device via CreateDCW("DISPLAY", deviceName, ...).

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


_DISPLAY_DEVICE_ACTIVE = 0x00000001
_DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
_DISPLAY_DEVICE_MIRRORING_DRIVER = 0x00000008

_user32.EnumDisplayDevicesW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DISPLAY_DEVICEW), wintypes.DWORD,
]
_user32.EnumDisplayDevicesW.restype = wintypes.BOOL

_gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
_gdi32.CreateDCW.restype = wintypes.HDC
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL
_gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
_gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL
_gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
_gdi32.GetDeviceGammaRamp.restype = wintypes.BOOL


_GammaRamp = ctypes.c_uint16 * 256 * 3  # [3][256]


def _enum_active_display_names() -> list:
    """Return all attached, non-mirror display device names (e.g. \\\\.\\DISPLAY1)."""
    names = []
    dd = _DISPLAY_DEVICEW()
    dd.cb = ctypes.sizeof(dd)
    i = 0
    while _user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
        flags = dd.StateFlags
        if (flags & _DISPLAY_DEVICE_ACTIVE) and not (flags & _DISPLAY_DEVICE_MIRRORING_DRIVER):
            names.append(dd.DeviceName)
        i += 1
        # safety: real systems have <16 displays
        if i > 64:
            break
    return names


def _primary_display_name() -> Optional[str]:
    dd = _DISPLAY_DEVICEW()
    dd.cb = ctypes.sizeof(dd)
    i = 0
    while _user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
        if dd.StateFlags & _DISPLAY_DEVICE_PRIMARY_DEVICE:
            return dd.DeviceName
        i += 1
    return None


def _open_primary_dc() -> Optional[int]:
    name = _primary_display_name()
    if not name:
        return None
    hdc = _gdi32.CreateDCW("DISPLAY", name, None, None)
    return hdc if hdc else None


# Persistent DC list, one per active monitor. We need this because:
#  1) Multi-monitor setups want the ramp on EVERY display, not just primary
#     (game might be on secondary, or the user just wants consistent color).
#  2) On some NVIDIA setups DeleteDC reverts the ramp, so we keep DCs alive
#     for the whole process lifetime.
_persistent_dcs: list = []  # list of (display_name, hdc)


def _get_persistent_dcs() -> list:
    """Lazily open one DC per active display; cached for the process lifetime."""
    global _persistent_dcs
    if _persistent_dcs:
        return _persistent_dcs
    for name in _enum_active_display_names():
        hdc = _gdi32.CreateDCW("DISPLAY", name, None, None)
        if hdc:
            _persistent_dcs.append((name, hdc))
    return _persistent_dcs


def _get_persistent_hdc() -> Optional[int]:
    """Primary monitor DC (kept for callers that only deal with one display)."""
    primary = _primary_display_name()
    for name, hdc in _get_persistent_dcs():
        if name == primary:
            return hdc
    # Primary not in list (rare) — fall back to first available DC.
    dcs = _get_persistent_dcs()
    return dcs[0][1] if dcs else None


def release_persistent_hdc() -> None:
    """Release every persistent display DC. Call once at process shutdown."""
    global _persistent_dcs
    for _name, hdc in _persistent_dcs:
        _gdi32.DeleteDC(hdc)
    _persistent_dcs = []


# Register at module import so DCs get released even on hard shutdown paths
# (sys.exit, unhandled exception). atexit fires before the interpreter dies
# but does NOT fire on os._exit() or external taskkill — those still leak.
atexit.register(release_persistent_hdc)


def reset_persistent_dcs() -> None:
    """
    Drop and re-open the persistent DC list. Use after WM_DISPLAYCHANGE so
    that newly attached monitors get a DC and detached ones don't leave a
    stale handle. Caller is expected to re-capture ramps and re-apply the
    active profile after this.
    """
    release_persistent_hdc()
    _get_persistent_dcs()  # lazily rebuilds the list


def _build_gamma_ramp(
    brightness: float,
    contrast: float,
    gamma: float,
    black_lift: float = 0.0,
) -> _GammaRamp:
    """
    Build 256x3 ramp.

    Pipeline (in order):
      1. gamma       — pow(v, 1/gamma); curves midtones, leaves 0 and 1 fixed.
      2. contrast    — stretches/compresses around 0.5; >1 expands, <1 compresses.
      3. black_lift  — selectively raises dark pixels using a (1-v)^2 falloff.
                       At v=0 the full lift value is added; at v=1 nothing is
                       added. Use to brighten "dark corners" without burning
                       highlights. 0 = disabled; 0.5 = aggressive.
      4. brightness  — ADDITIVE offset, not multiplicative. brightness=1.0 = no
                       change; 2.0 lifts everything by +0.5; 0.5 by -0.25.
    """
    brightness = max(0.1, min(3.0, float(brightness)))
    contrast = max(0.1, min(3.0, float(contrast)))
    gamma = max(0.1, min(5.0, float(gamma)))
    black_lift = max(0.0, min(0.5, float(black_lift)))

    inv_gamma = 1.0 / gamma
    brightness_offset = (brightness - 1.0) * 0.5

    ramp = _GammaRamp()
    for i in range(256):
        v = i / 255.0
        # 1. gamma
        v = v ** inv_gamma if v > 0 else 0.0
        # 2. contrast around midpoint
        v = (v - 0.5) * contrast + 0.5
        # 3. shadow lift — quadratic falloff so the effect concentrates in
        # the lower third of the curve.
        if black_lift > 0:
            inv = 1.0 - v
            if inv < 0:
                inv = 0.0
            v = v + black_lift * (inv * inv)
        # 4. brightness as additive offset
        v = v + brightness_offset
        # clamp
        if v < 0.0:
            v = 0.0
        elif v > 1.0:
            v = 1.0
        word = int(round(v * 65535))
        ramp[0][i] = word
        ramp[1][i] = word
        ramp[2][i] = word
    return ramp


def apply_gamma_ramp(
    brightness: float,
    contrast: float,
    gamma: float,
    black_lift: float = 0.0,
) -> bool:
    """
    Apply the ramp to EVERY active monitor. Returns True if at least one DC
    accepted the call — partial success is normal in multi-monitor setups
    where one head might be on a non-NVIDIA GPU or otherwise read-only.
    """
    dcs = _get_persistent_dcs()
    if not dcs:
        return False
    ramp = _build_gamma_ramp(brightness, contrast, gamma, black_lift)
    any_ok = False
    for _name, hdc in dcs:
        if _gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
            any_ok = True
    return any_ok


def capture_gamma_ramps() -> dict:
    """
    Snapshot the current ramp from every active monitor.
    Returns {display_name: _GammaRamp}. Empty dict on failure.
    """
    out = {}
    for name, hdc in _get_persistent_dcs():
        ramp = _GammaRamp()
        if _gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
            out[name] = ramp
    return out


def restore_gamma_ramps(ramps: dict) -> bool:
    """
    Push previously captured per-display ramps back. Any monitor missing from
    the dict is left alone (it wasn't snapshotted at startup).
    """
    if not ramps:
        return False
    any_ok = False
    for name, hdc in _get_persistent_dcs():
        ramp = ramps.get(name)
        if ramp is None:
            continue
        if _gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
            any_ok = True
    return any_ok


# Back-compat shims for any single-monitor callers that may still be around.
def capture_gamma_ramp() -> Optional["_GammaRamp"]:
    snaps = capture_gamma_ramps()
    if not snaps:
        return None
    primary = _primary_display_name()
    return snaps.get(primary, next(iter(snaps.values()), None))


def restore_gamma_ramp(ramp: "_GammaRamp") -> bool:
    """Push a single ramp to every active monitor (legacy helper)."""
    dcs = _get_persistent_dcs()
    if not dcs:
        return False
    any_ok = False
    for _name, hdc in dcs:
        if _gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
            any_ok = True
    return any_ok


def gamma_ramp_supported() -> bool:
    """
    Probe whether SetDeviceGammaRamp actually reaches the screen.

    Two levels of verification:
      1. Write a recognizable ramp through persistent DC, read back through the
         SAME DC. NVIDIA drivers cache the value in the DC so this almost
         always returns the probe pattern even when DWM is dropping the change.
      2. Open a SECOND independent DC (or GetDC(NULL)) and read again. If THIS
         readback matches our probe, the change is global; if it shows the
         original (or a default linear), DWM is swallowing the call and the
         user sees no on-screen effect — including in games.

    Win10/11 DWM blocks ramp updates when HDR / Auto Color Management /
    Night Light / ICC profile / Game Bar HDR is active. This probe catches
    that case so the UI can warn the user.
    """
    hdc = _get_persistent_hdc()
    if not hdc:
        return False

    original = _GammaRamp()
    if not _gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(original)):
        return False

    probe = _GammaRamp()
    for i in range(256):
        v = ((i * 257) ^ 0x5555) & 0xFFFF
        probe[0][i] = v
        probe[1][i] = v
        probe[2][i] = v

    if not _gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(probe)):
        return False

    # Level 2: read through an independent DC. If the OS is honestly applying
    # the ramp system-wide, this fresh DC will see the probe pattern too.
    second_hdc = _open_primary_dc()
    second_match = False
    if second_hdc:
        readback2 = _GammaRamp()
        if _gdi32.GetDeviceGammaRamp(second_hdc, ctypes.byref(readback2)):
            second_match = bytes(readback2) == bytes(probe)
        _gdi32.DeleteDC(second_hdc)

    # Always restore — the probe must not leave the screen tinted.
    _gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(original))

    return second_match


# ---------- Public ColorManager ----------

class ColorManager:
    """Top-level facade. Owns NvAPI handle and provides apply/reset for profiles."""

    DEFAULT_VIBRANCE = 0     # neutral
    DEFAULT_BRIGHTNESS = 1.0
    DEFAULT_CONTRAST = 1.0
    DEFAULT_GAMMA = 1.0

    def __init__(self):
        self._nvapi = _NvAPI()
        # Snapshot every monitor's ramp BEFORE we touch anything. reset()
        # restores each one to exactly what the user had on launch (preserving
        # ICC profile / Night Light / per-monitor calibration).
        self._original_ramps = capture_gamma_ramps()
        self._gamma_supported = bool(self._original_ramps) and gamma_ramp_supported()

    @property
    def nvapi_available(self) -> bool:
        return self._nvapi.available

    @property
    def nvapi_error(self) -> Optional[str]:
        return self._nvapi.error

    @property
    def gamma_supported(self) -> bool:
        return self._gamma_supported

    @property
    def vibrance_writable(self) -> bool:
        """True only if NvAPI accepts non-zero DVC levels (legacy API works)."""
        return self._nvapi.available and self._nvapi.set_works_for_nonzero is True

    def rebuild_for_display_change(self) -> None:
        """
        Call after WM_DISPLAYCHANGE / system resume. Releases stale DCs
        (their backing display may have been unplugged), re-opens DCs for the
        currently active set of monitors, and re-snapshots their original
        ramps. The caller should re-apply the currently active profile after
        this — this method intentionally doesn't, to keep responsibilities
        separated.
        """
        reset_persistent_dcs()
        # Snapshot whatever ramp the OS now considers the baseline for the new
        # monitor set. New monitors get their startup ramp captured for the
        # first time; for monitors already known we *replace* the snapshot
        # with what they have right now (ours might already have been applied,
        # but right after a resume Windows usually reverts to the system one).
        new_snaps = capture_gamma_ramps()
        if new_snaps:
            for name, ramp in new_snaps.items():
                if name not in self._original_ramps:
                    self._original_ramps[name] = ramp
        # Refresh the gamma_supported flag too — a new monitor on a different
        # GPU might respond differently.
        self._gamma_supported = bool(self._original_ramps) and gamma_ramp_supported()

    def apply(
        self,
        vibrance: int,
        brightness: float,
        contrast: float,
        gamma: float,
        black_lift: float = 0.0,
    ) -> bool:
        """
        Apply a profile.

        Returns True only if at least one of the requested adjustments
        actually succeeded. Earlier the method returned True whenever NvAPI
        was loaded — which lied to the UI ("Применён профиль") even though
        the driver had rejected the DVC write and gamma never landed.
        """
        ok_gamma = (
            apply_gamma_ramp(brightness, contrast, gamma, black_lift)
            if self._gamma_supported else False
        )
        # Only count vibrance as "applied" if the driver actually accepted
        # writes during probe. Otherwise set_vibrance_percent is a no-op
        # disguised as success.
        ok_vibrance = False
        if self._nvapi.available and self._nvapi.set_works_for_nonzero:
            ok_vibrance = self._nvapi.set_vibrance_percent(vibrance)
        elif self._nvapi.available:
            # Try the neutral level (DVC=default) anyway — most drivers honor
            # zero-level even when non-zero is rejected. Don't count it as
            # success; it's just hygiene.
            self._nvapi.set_vibrance_percent(50)
        return ok_gamma or ok_vibrance

    def reset(self) -> bool:
        # Restore each monitor's exact ramp captured at startup, not a synthetic
        # linear one. Otherwise users with ICC profiles / Night Light /
        # calibrated displays see "Reset" leave the screen in a different state
        # than before launch.
        ok_gamma = False
        if self._original_ramps:
            ok_gamma = restore_gamma_ramps(self._original_ramps)
        elif self._gamma_supported:
            # No snapshot (very rare) — fall back to neutral linear ramp.
            ok_gamma = apply_gamma_ramp(
                self.DEFAULT_BRIGHTNESS, self.DEFAULT_CONTRAST, self.DEFAULT_GAMMA
            )

        if self._nvapi.available:
            self._nvapi.set_vibrance_percent(self.DEFAULT_VIBRANCE)

        return ok_gamma or self._nvapi.available

    def shutdown(self):
        try:
            self.reset()
        finally:
            self._nvapi.shutdown()
            release_persistent_hdc()
