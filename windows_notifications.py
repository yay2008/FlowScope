"""当前 Windows 用户会话里的原生通知；只在通知线程初始化 WinRT。"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

APP_ID = "FlowScope.WT2.Alerts"


class WindowsNotifier:
    def __init__(self):
        self.toaster = None

    def prepare(self):
        if self.toaster is not None:
            return
        if sys.platform != "win32":
            raise RuntimeError("Windows 桌面通知仅支持 Windows；警报记录仍会保存")
        import winreg
        from windows_toasts import InteractableWindowsToaster
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Software\\Classes\\AppUserModelId\\" + APP_ID) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "FlowScope")
            icon = Path(__file__).resolve().parent / "static" / "favicon.ico"
            if icon.is_file():
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
        self.toaster = InteractableWindowsToaster("FlowScope", notifierAUMID=APP_ID)

    def send(self, events, *, test=False):
        from windows_toasts import Toast
        from winrt.windows.ui.notifications import NotificationSetting
        self.prepare()
        try:
            setting = self.toaster.toastNotifier.setting
        except OSError as exc:
            if exc.winerror != -2147023728:  # 首次发送前还没有系统通知设置项
                raise
            setting = NotificationSetting.ENABLED
        if setting != NotificationSetting.ENABLED:
            raise RuntimeError("Windows 已禁用 FlowScope 通知，请在系统通知设置中开启")
        first = events[0] if events else {}
        title = "FlowScope · 通知测试" if test else "FlowScope · 5分钟 WT2 警报"
        if test:
            lines = [title, "Windows 通知已接通", "关闭网页后，只要 FlowScope 后端运行，警报仍会发送。"]
        else:
            lines = [title]
            for event in events[:2]:
                zone = "超买" if event["zone"] == "overbought" else "超卖"
                stamp = datetime.fromtimestamp(event["bar"] + 300, timezone.utc).strftime("%H:%M")
                lines.append(f'{event["name"]} · {zone} {event["wt2"]:.2f} · {stamp} 收盘')
            if len(events) > 2:
                lines[-1] += f"（共 {len(events)} 条，点击查看）"
        toast = Toast(lines)
        query = urlencode({"symbol": first.get("symbol", "KQ.m@SHFE.fu"), "alert_tf": 300})
        toast.launch_action = "http://127.0.0.1:8000/?" + query
        # 固定标识使同一批通知在 Windows 通知中心也只有一项。
        toast.tag = first.get("id", "test")[:16]
        toast.group = "flowscope-wt2"
        failed, errors = threading.Event(), []
        def on_failed(args):
            errors.append(str(args.error_code))
            failed.set()
        toast.on_failed = on_failed
        self.toaster.show_toast(toast)
        if failed.wait(0.3):
            raise RuntimeError("Windows 未接收通知：" + ", ".join(errors))
