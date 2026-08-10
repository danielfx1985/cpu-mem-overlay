"""Windows CPU / GPU 温度采集（仅系统与显卡驱动接口，无第三方监控软件）。"""

from __future__ import annotations

import ctypes
import time
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char,
    c_int,
    c_void_p,
    cast,
    create_string_buffer,
    sizeof,
)
from dataclasses import dataclass


@dataclass
class TempReading:
    cpu_c: float | None = None
    gpu_c: float | None = None
    cpu_source: str = ""
    gpu_source: str = ""


def _valid_temp(value: float | None) -> float | None:
    if value is None:
        return None
    if 1.0 < value <= 120.0:
        return float(value)
    return None


class TemperatureMonitor:
    """在采样线程中使用。"""

    def __init__(self) -> None:
        self._pdh_query = None
        self._pdh_counters: list = []
        self._pdh_ready = False
        self._pdh_primed = False
        self._nvml_ok = False
        self._nvml = None
        self._adl = None
        self._adl_ready = False
        self._adl_adapter_index: int | None = None

    def close(self) -> None:
        if self._pdh_query is not None:
            try:
                import win32pdh

                win32pdh.CloseQuery(self._pdh_query)
            except Exception:
                pass
            self._pdh_query = None
            self._pdh_counters = []
            self._pdh_ready = False

        if self._nvml_ok and self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:
                pass
            self._nvml_ok = False
            self._nvml = None

        if self._adl_ready and self._adl is not None:
            try:
                self._adl.ADL_Main_Control_Destroy()
            except Exception:
                pass
            self._adl_ready = False
            self._adl = None

    def read(self) -> TempReading:
        reading = TempReading()
        self._read_thermal_zone(reading)
        self._read_amd_adl(reading)
        if reading.gpu_c is None:
            self._read_nvml(reading)
        return reading

    def _ensure_pdh(self) -> bool:
        if self._pdh_ready:
            return True
        try:
            import win32pdh

            paths = win32pdh.ExpandCounterPath(r"\Thermal Zone Information(*)\Temperature")
            if not paths:
                return False
            query = win32pdh.OpenQuery()
            counters = []
            for path in paths:
                counters.append(win32pdh.AddCounter(query, path))
            self._pdh_query = query
            self._pdh_counters = counters
            self._pdh_ready = True
            return True
        except Exception:
            self._pdh_query = None
            self._pdh_counters = []
            self._pdh_ready = False
            return False

    def _read_thermal_zone(self, reading: TempReading) -> None:
        """Windows 性能计数器热区温度（开氏度）。"""
        if not self._ensure_pdh():
            return
        try:
            import win32pdh

            win32pdh.CollectQueryData(self._pdh_query)
            if not self._pdh_primed:
                # PDH 首次采集常无效，补一次短间隔
                time.sleep(0.05)
                win32pdh.CollectQueryData(self._pdh_query)
                self._pdh_primed = True

            values: list[float] = []
            for counter in self._pdh_counters:
                try:
                    _t, raw = win32pdh.GetFormattedCounterValue(counter, win32pdh.PDH_FMT_DOUBLE)
                except Exception:
                    continue
                celsius = float(raw) - 273.15
                checked = _valid_temp(celsius)
                if checked is not None:
                    values.append(checked)
            if values:
                reading.cpu_c = max(values)
                reading.cpu_source = "WinThermalZone"
        except Exception:
            # 仅重置 PDH，不影响 GPU 后端
            if self._pdh_query is not None:
                try:
                    import win32pdh

                    win32pdh.CloseQuery(self._pdh_query)
                except Exception:
                    pass
            self._pdh_query = None
            self._pdh_counters = []
            self._pdh_ready = False
            self._pdh_primed = False

    def _read_nvml(self, reading: TempReading) -> None:
        if reading.gpu_c is not None:
            return
        try:
            import pynvml

            if not self._nvml_ok:
                pynvml.nvmlInit()
                self._nvml = pynvml
                self._nvml_ok = True
            count = pynvml.nvmlDeviceGetCount()
            if count <= 0:
                return
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            temp = float(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
            checked = _valid_temp(temp)
            if checked is not None:
                reading.gpu_c = checked
                reading.gpu_source = "NVML"
        except Exception:
            self._nvml_ok = False
            self._nvml = None

    def _ensure_adl(self) -> bool:
        if self._adl_ready:
            return True
        try:
            adl = ctypes.WinDLL(r"C:\Windows\System32\atiadlxx.dll")
        except OSError:
            try:
                adl = ctypes.WinDLL(r"C:\Windows\System32\atiadlxy.dll")
            except OSError:
                return False

        @ctypes.WINFUNCTYPE(c_void_p, c_int)
        def adl_alloc(size: int):
            return cast(create_string_buffer(size), c_void_p).value

        adl.ADL_Main_Control_Create.argtypes = [ctypes.c_void_p, c_int]
        adl.ADL_Main_Control_Create.restype = c_int
        adl.ADL_Main_Control_Destroy.restype = c_int
        if adl.ADL_Main_Control_Create(adl_alloc, 1) != 0:
            return False

        class AdapterInfo(Structure):
            _fields_ = [
                ("iSize", c_int),
                ("iAdapterIndex", c_int),
                ("strUDID", c_char * 256),
                ("iBusNumber", c_int),
                ("iDeviceNumber", c_int),
                ("iFunctionNumber", c_int),
                ("iVendorID", c_int),
                ("strAdapterName", c_char * 256),
                ("strDisplayName", c_char * 256),
                ("iPresent", c_int),
                ("iExist", c_int),
                ("strDriverPath", c_char * 256),
                ("strDriverPathExt", c_char * 256),
                ("strPNPString", c_char * 256),
                ("iOSDisplayIndex", c_int),
            ]

        num = c_int(0)
        adl.ADL_Adapter_NumberOfAdapters_Get.argtypes = [POINTER(c_int)]
        adl.ADL_Adapter_NumberOfAdapters_Get.restype = c_int
        if adl.ADL_Adapter_NumberOfAdapters_Get(byref(num)) != 0 or num.value <= 0:
            adl.ADL_Main_Control_Destroy()
            return False

        infos = (AdapterInfo * num.value)()
        for info in infos:
            info.iSize = sizeof(AdapterInfo)
        adl.ADL_Adapter_AdapterInfo_Get.argtypes = [POINTER(AdapterInfo), c_int]
        adl.ADL_Adapter_AdapterInfo_Get.restype = c_int
        if adl.ADL_Adapter_AdapterInfo_Get(infos, sizeof(infos)) != 0:
            adl.ADL_Main_Control_Destroy()
            return False

        # 选第一个在位的适配器
        for info in infos:
            if info.iPresent:
                self._adl_adapter_index = int(info.iAdapterIndex)
                break
        if self._adl_adapter_index is None:
            adl.ADL_Main_Control_Destroy()
            return False

        self._adl = adl
        self._AdapterInfo = AdapterInfo  # type: ignore[attr-defined]
        self._adl_ready = True
        return True

    def _read_amd_adl(self, reading: TempReading) -> None:
        """AMD 驱动 ADL Overdrive 温度（毫摄氏度）。"""
        if reading.gpu_c is not None:
            return
        if not self._ensure_adl():
            return
        assert self._adl is not None and self._adl_adapter_index is not None

        class ADLTemperature(Structure):
            _fields_ = [("iSize", c_int), ("iTemperature", c_int)]

        try:
            fn = self._adl.ADL_Overdrive5_Temperature_Get
            fn.argtypes = [c_int, c_int, POINTER(ADLTemperature)]
            fn.restype = c_int
            temp = ADLTemperature(iSize=sizeof(ADLTemperature), iTemperature=0)
            rc = fn(self._adl_adapter_index, 0, byref(temp))
            if rc == 0:
                checked = _valid_temp(temp.iTemperature / 1000.0)
                if checked is not None:
                    reading.gpu_c = checked
                    reading.gpu_source = "AMD-ADL"
                    return
        except Exception:
            pass

        # 失败时下次重建
        try:
            self._adl.ADL_Main_Control_Destroy()
        except Exception:
            pass
        self._adl_ready = False
        self._adl = None
        self._adl_adapter_index = None
