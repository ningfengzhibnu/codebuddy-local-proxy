#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代理守护进程 - 外部监护，每30秒检查端口。
死了就强杀进程 + 重启，不依赖代理自身存活。
"""
import subprocess, time, socket, os, sys

HOST, PORT = "127.0.0.1", 19090
PYTHON = r"python"
SCRIPT = r"local_codebuddy_proxy.py"

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_guard.log")

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass

def check_port():
    try:
        s = socket.create_connection((HOST, PORT), timeout=3)
        s.close()
        return True
    except:
        return False

def kill_port_owner():
    """只杀 19090 端口上的进程，不误伤其他 Python"""
    try:
        out = subprocess.check_output(
            f'netstat -ano | findstr :{PORT} | findstr LISTENING',
            shell=True, text=True, timeout=5
        )
        for line in out.strip().split('\n'):
            parts = line.split()
            if len(parts) >= 5 and parts[-1].isdigit():
                pid = int(parts[-1])
                log(f"杀死占用端口的进程 PID={pid}")
                subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                               capture_output=True, timeout=5)
                return True
    except:
        pass
    return False

# 确保代理运行
if not check_port():
    log("守护启动，代理未运行，启动中...")
    subprocess.Popen([PYTHON, SCRIPT],
                     cwd=os.path.dirname(SCRIPT),
                     creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(3)

log("守护就绪，每30秒检查一次...")

while True:
    time.sleep(30)
    if check_port():
        continue  # 正常
    log("代理无响应，强制重启...")
    kill_port_owner()
    time.sleep(2)
    subprocess.Popen([PYTHON, SCRIPT],
                     cwd=os.path.dirname(SCRIPT),
                     creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(3)
