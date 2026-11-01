"""人工首登工具：headed 打开平台登录页，人工完成登录后保存会话种子。

用法：
    PLAYWRIGHT_BROWSERS_PATH=ti_data/browsers \
        .venv/bin/python -m api.ti_expand.recon.manual_login <platform>

适用场景：平台登录带验证码/滑块（如微步极验），自动登录抛 need_manual 后，
由人工在有头浏览器里完成一次登录。

窗口形态（env TI_LOGIN_VIEWER，默认 auto）：
  - local：服务器本机弹窗（macOS / 有显示器的 Linux）
  - vnc  ：无显示器 Linux——起 Xvfb 虚拟屏 + x11vnc + websockify，浏览器跑在
          虚拟屏上，画面经 noVNC 网页远程展示（调用台「去登录」弹窗内操作）
  auto  ：macOS 或已设 DISPLAY → local，否则 → vnc

状态上报：启动后把 {"mode", "ws_port", "pid"} 写入
ti_data/artifacts/manual_login_{platform}.json（父进程 /ti/sessions/{p}/manual-login
据此生成远程桌面地址），正常/异常退出时按 pid 匹配删除。

判定口径（2026-07-25 修正，防首页假阳性）：人工操作期间每 2s 观察当前页，
一旦离开 SSO 登录页，就用 login.probe_url（数据面地址）开新页实测——
未被弹回 SSO 才算真登录（此时 SPA 已写入 token），再等待 3s 让本地态落盘，
最后把 storage_state 写入 ti_data/sessions/{platform}/storage_state.json
（BrowserSession 空 profile 启动时自动注入该种子）。Ctrl-C 中止。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]  # ThreatLens-API/
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_ROOT / "ti_data" / "browsers"))

# 包导入路径（直接 python -m 运行时已就绪；脚本方式运行兜底）
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api.ti_expand.adapters.browser import (  # noqa: E402
    _STEALTH_INIT_SCRIPT,
    parse_proxy,
)
from api.ti_expand.adapters.session import (  # noqa: E402
    glob_match,
    load_login_cfg,
    verify_data_plane,
)

TIMEOUT_S = 15 * 60
VERIFY_COOLDOWN_S = 10.0  # 数据面实测未通过后，多久再试（防打频率）


# ─── 窗口形态：本机弹窗 / 网页远程桌面 ────────────────────────────────────────


def _resolve_mode() -> str:
    """TI_LOGIN_VIEWER=auto|local|vnc；auto 下 macOS/有 DISPLAY → local，否则 vnc。"""
    mode = os.environ.get("TI_LOGIN_VIEWER", "auto").strip().lower()
    if mode not in ("auto", "local", "vnc"):
        mode = "auto"
    if mode == "auto":
        return "local" if (sys.platform == "darwin" or os.environ.get("DISPLAY")) else "vnc"
    return mode


def _free_tcp_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _free_display() -> str:
    """找一个未被占用的 X display 编号（99~129）。"""
    for n in range(99, 130):
        if not Path(f"/tmp/.X{n}-lock").exists() \
                and not Path(f"/tmp/.X11-unix/X{n}").exists():
            return f":{n}"
    raise RuntimeError("无可用 X display 编号（99~129 均被占用）")


class _VncStack:
    """Xvfb 虚拟屏 + x11vnc + websockify：noVNC 网页远程桌面的服务端半边。

    只绑 127.0.0.1：对外暴露与鉴权由 API 进程的 /ti/login-view 反代负责，
    本栈不自开任何外部端口。
    """

    def __init__(self) -> None:
        self.display = ""
        self.ws_port = 0
        self._procs = []

    def start(self) -> None:
        missing = [t for t in ("Xvfb", "x11vnc", "websockify") if not shutil.which(t)]
        if missing:
            raise RuntimeError(
                f"远程桌面组件缺失: {', '.join(missing)}"
                "（Debian/Ubuntu: apt-get install -y xvfb x11vnc websockify novnc）")

        self.display = _free_display()
        xvfb = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", "1440x900x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(xvfb)
        sock = Path(f"/tmp/.X11-unix/X{self.display[1:]}")
        for _ in range(50):  # 等虚拟屏就绪（5s）
            if sock.exists():
                break
            if xvfb.poll() is not None:
                raise RuntimeError("Xvfb 启动即退出")
            time.sleep(0.1)
        else:
            raise RuntimeError("Xvfb 虚拟屏就绪超时")

        rfb_port = _free_tcp_port()
        self._procs.append(subprocess.Popen(
            ["x11vnc", "-display", self.display, "-forever", "-shared",
             "-nopw", "-localhost", "-rfbport", str(rfb_port), "-quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for _ in range(50):  # 等 RFB 端口就绪（5s）
            try:
                with socket.create_connection(("127.0.0.1", rfb_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)

        self.ws_port = _free_tcp_port()
        self._procs.append(subprocess.Popen(
            ["websockify", f"127.0.0.1:{self.ws_port}", f"127.0.0.1:{rfb_port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        os.environ["DISPLAY"] = self.display

    def stop(self) -> None:
        for proc in reversed(self._procs):
            try:
                proc.terminate()
            except Exception:
                pass
        for proc in reversed(self._procs):
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._procs.clear()


# ─── 状态上报（父进程读以生成远程桌面地址）────────────────────────────────────


def _status_path(platform: str) -> Path:
    return _ROOT / "ti_data" / "artifacts" / f"manual_login_{platform}.json"


def _write_status(platform: str, payload: dict) -> None:
    path = _status_path(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _clear_status(platform: str, pid: int) -> None:
    """按 pid 匹配删除（误杀新一轮登录上报的状态文件会破坏重连）。"""
    path = _status_path(platform)
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == pid:
            path.unlink(missing_ok=True)
    except Exception:
        pass


# ─── 主流程 ──────────────────────────────────────────────────────────────────


async def _main(platform: str) -> int:
    from playwright.async_api import async_playwright

    try:
        login_cfg = load_login_cfg(platform)
    except ValueError as exc:
        print(f"[manual_login] {exc}")
        return 2
    seed = _ROOT / "ti_data" / "sessions" / platform / "storage_state.json"
    mode = _resolve_mode()
    my_pid = os.getpid()
    stack = None
    try:
        if mode == "vnc":
            stack = _VncStack()
            stack.start()
            print(f"[manual_login] 远程桌面已就绪: display={stack.display} "
                  f"ws_port={stack.ws_port}（经 /ti/login-view 访问）")
        else:
            print("[manual_login] 本机窗口模式")
        _write_status(platform, {"mode": mode,
                                 "ws_port": stack.ws_port if stack else None,
                                 "pid": my_pid, "launched_at": time.time()})

        async with async_playwright() as pw:
            # 优先本机 Chrome（JA3/指纹与日常浏览器一致，WAF 最不易拦）；
            # TI_BROWSER_CHANNEL 可覆盖，置空串强制用内置 Chromium
            channel = os.environ.get("TI_BROWSER_CHANNEL", "chrome").strip()
            # 内网部署出网需走代理：生效代理由 /ti/sessions/{p}/manual-login 父进程
            # 算好后经 TI_EFFECTIVE_PROXY 透传（本进程无 DB 连接，不自行读设置）
            launch_kwargs = {"headless": False}
            if channel:
                launch_kwargs["channel"] = channel
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                # 容器 root 运行必须关 Chromium 沙箱，否则浏览器拒绝启动
                launch_kwargs["chromium_sandbox"] = False
            proxy = parse_proxy(os.environ.get("TI_EFFECTIVE_PROXY", ""))
            if proxy:
                launch_kwargs["proxy"] = proxy
                print(f"[manual_login] 浏览器经由代理: {proxy['server']}")
            else:
                print("[manual_login] 未配置代理，直连")
            try:
                browser = await pw.chromium.launch(**launch_kwargs)
            except Exception as exc:
                print(f"[manual_login] channel={channel} 启动失败（{exc}），回落内置 Chromium")
                launch_kwargs.pop("channel", None)
                browser = await pw.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900}, locale="zh-CN")
            await context.add_init_script(_STEALTH_INIT_SCRIPT)  # 与采集端同套指纹补丁
            page = await context.new_page()
            entry = login_cfg["url"]
            print(f"[manual_login] 打开 {entry}")
            print("[manual_login] 请在浏览器中完成登录（含验证码/滑块/扫码）…")
            print("[manual_login] 判定口径：登录后自动开页实测数据面，通过才会保存会话并关闭窗口")
            await page.goto(entry, wait_until="domcontentloaded", timeout=60000)

            sso = login_cfg.get("sso_url_pattern") or ""
            deadline = time.monotonic() + TIMEOUT_S
            last_verify = 0.0
            while time.monotonic() < deadline:
                await asyncio.sleep(2.0)
                try:
                    pages = context.pages
                    if not pages:
                        print("[manual_login] 浏览器页面已关闭，退出")
                        return 1
                    current = pages[-1]
                    # 仍在 SSO 登录页 → 继续等人工操作，不发任何探测
                    if sso and glob_match(current.url, sso):
                        continue
                    if time.monotonic() - last_verify < VERIFY_COOLDOWN_S:
                        continue
                    last_verify = time.monotonic()
                    print(f"[manual_login] 页面已离开登录页（{current.url}），实测数据面…")
                    if not await verify_data_plane(context, login_cfg):
                        print("[manual_login] 数据面仍弹回登录页：登录未真正生效，请继续完成登录")
                        continue
                    # 真登录：等 SPA 把 token 写入本地态再快照
                    await asyncio.sleep(3.0)
                    seed.parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=str(seed))
                    cookies = len(await context.cookies())
                    print(f"[manual_login] 数据面验证通过（{current.url}）")
                    print(f"[manual_login] 会话种子已保存: {seed}（{cookies} 个 cookie）")
                    print("[manual_login] 后续采集将自动复用该会话，无需再次登录")
                    await browser.close()
                    return 0
                except Exception as exc:
                    print(f"[manual_login] 判定异常（继续等待）: {exc}")
            print(f"[manual_login] 超时（{TIMEOUT_S // 60} 分钟）未检测到登录成功")
            await browser.close()
            return 1
    finally:
        if stack is not None:
            stack.stop()
        _clear_status(platform, my_pid)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    platform = sys.argv[1].strip()
    # SIGTERM → KeyboardInterrupt：让 _main 的 finally 跑完（收 VNC 栈、删状态文件）
    import signal

    def _sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)
    try:
        return asyncio.run(_main(platform))
    except KeyboardInterrupt:
        print("\n[manual_login] 已中止，未保存会话")
        return 130


if __name__ == "__main__":
    sys.exit(main())
