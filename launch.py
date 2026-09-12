#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TFBSM 期权交易系统 一键启动器
==============================
双击打包后的 exe 即可：
  1. 检查 8000 端口服务是否已在运行（已运行则直接打开浏览器）
  2. 未运行则后台启动 app.py
  3. 等待服务就绪后自动打开浏览器

exe 查找 app.py 的顺序：
  1. exe 同目录下的 app.py（启动器放在 tfbsm_trading 目录内时）
  2. 默认安装路径（打包时内置的 tfbsm_trading 目录）
"""

import os
import socket
import subprocess
import sys
import time
import webbrowser

PORT = 8000
URL = f"http://127.0.0.1:{PORT}"
# 打包时内置的默认系统路径（启动器 exe 放在桌面等外部位置时使用）
DEFAULT_BASE = r"C:\Users\xuyin\Desktop\期权\TFBSM期权交易工具包\tfbsm_trading"


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_app_py():
    # 1. exe 同目录
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    p = os.path.join(here, "app.py")
    if os.path.exists(p):
        return here
    # 2. 默认路径
    if os.path.exists(os.path.join(DEFAULT_BASE, "app.py")):
        return DEFAULT_BASE
    return None


def find_python():
    for cmd in (["python"], ["py", "-3"], ["python3"]):
        try:
            subprocess.run(cmd + ["--version"], capture_output=True, timeout=15)
            return cmd
        except Exception:
            continue
    return None


def main():
    base = find_app_py()
    if base is None:
        msg = ("未找到 TFBSM 系统目录。请确认以下路径存在 app.py：\n\n"
               f"  {DEFAULT_BASE}\n\n"
               "或把启动器放到 tfbsm_trading 目录内。")
        print(msg)
        try:
            webbrowser.open("https://example.com")  # 兜底：无法弹窗时至少保持简单行为
        except Exception:
            pass
        input("\n按回车键退出...")
        return

    if not port_in_use(PORT):
        py = find_python()
        if py is None:
            print("未找到 Python 环境，请先安装 Python 3.10+")
            input("\n按回车键退出...")
            return
        os.makedirs(os.path.join(base, "logs"), exist_ok=True)
        logf = open(os.path.join(base, "logs", "launcher.log"), "a", encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            py + [os.path.join(base, "app.py"), "--port", str(PORT)],
            cwd=base, stdout=logf, stderr=subprocess.STDOUT, creationflags=flags,
        )
        print("正在启动 TFBSM 服务...")

    # 等待服务就绪
    for _ in range(60):
        if port_in_use(PORT):
            break
        time.sleep(0.5)

    print(f"打开浏览器: {URL}")
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
