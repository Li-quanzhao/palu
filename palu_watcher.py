"""
帕鲁热更新守护进程
监控文件变化 → 自动重启 app.py
不需要装第三方库，用文件修改时间轮询

用法：
  python palu_watcher.py              # 前台运行（看日志）
  pythonw palu_watcher.py             # 后台运行（无窗口，Windows）
"""

import os
import sys
import time
import signal
import subprocess
import logging
from datetime import datetime

# 【参数可调】监控间隔（秒）
WATCH_INTERVAL = 3

# 脚本所在目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 【参数可调】监控的文件后缀
WATCH_EXTENSIONS = (".py", ".json")

# 【参数可调】忽略的文件/文件夹
WATCH_IGNORE = {"__pycache__", ".git", ".env", "palu_icon.png", "palu_icon.svg"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHER] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("watcher")

# 当前子进程
child_process = None


def get_monitored_files():
    """获取需要监控的文件列表及其最后修改时间"""
    files = {}
    for root, dirs, filenames in os.walk(BASE_DIR):
        # 跳过忽略的目录
        dirs[:] = [d for d in dirs if d not in WATCH_IGNORE]
        for f in filenames:
            if f.endswith(WATCH_EXTENSIONS):
                path = os.path.join(root, f)
                try:
                    files[path] = os.path.getmtime(path)
                except OSError:
                    pass
    return files


def start_app():
    """启动 app.py 子进程"""
    global child_process
    app_path = os.path.join(BASE_DIR, "app.py")
    log.info("🚀 启动帕鲁...")
    # 【逻辑说明】用 subprocess.Popen 启动子进程，不阻塞 watcher
    # stdout/stderr 直接透传，方便看日志
    # 【逻辑说明】设置 PALU_WATCHER=1 环境变量，让 app.py 知道自己被守护进程管理
    # 这样 /api/reload 端点不会自己 spawn 新进程，避免和 watcher 抢端口
    env = os.environ.copy()
    env["PALU_WATCHER"] = "1"
    child_process = subprocess.Popen(
        [sys.executable, app_path],
        cwd=BASE_DIR,
        env=env,
    )
    log.info(f"帕鲁进程已启动 | PID={child_process.pid}")


def restart_app():
    """重启 app.py 子进程"""
    global child_process
    if child_process and child_process.poll() is None:
        log.info(f"🔄 检测到文件变化，重启帕鲁 (PID={child_process.pid})...")
        # 先温柔地发 Ctrl+C 信号
        if sys.platform == "win32":
            child_process.terminate()
        else:
            child_process.send_signal(signal.SIGTERM)
        # 等最多 5 秒让进程退出
        try:
            child_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("帕鲁未在 5 秒内退出，强制 kill")
            child_process.kill()
            child_process.wait()
        log.info(f"旧进程已结束 (PID={child_process.pid})")
    start_app()


def main():
    log.info("=" * 50)
    log.info("帕鲁热更新守护进程启动")
    log.info(f"监控目录: {BASE_DIR}")
    log.info(f"监控间隔: {WATCH_INTERVAL}s")
    log.info(f"监控后缀: {', '.join(WATCH_EXTENSIONS)}")
    log.info("=" * 50)

    # 第一次启动
    start_app()
    
    # 记录初始文件状态
    last_files = get_monitored_files()
    log.info(f"已监控 {len(last_files)} 个文件")

    try:
        while True:
            time.sleep(WATCH_INTERVAL)
            current_files = get_monitored_files()

            # 检查是否有文件变化
            changed = False
            for path, mtime in current_files.items():
                old_mtime = last_files.get(path)
                if old_mtime is None or mtime > old_mtime:
                    rel_path = os.path.relpath(path, BASE_DIR)
                    log.info(f"文件变化: {rel_path}")
                    changed = True

            # 检查是否有文件被删除
            for path in last_files:
                if path not in current_files:
                    rel_path = os.path.relpath(path, BASE_DIR)
                    log.info(f"文件删除: {rel_path}")
                    changed = True

            if changed:
                restart_app()
                last_files = current_files

            # 检查子进程是否意外退出
            if child_process and child_process.poll() is not None:
                exit_code = child_process.poll()
                log.warning(f"帕鲁进程意外退出 | exit_code={exit_code}，3 秒后重启...")
                time.sleep(3)
                start_app()
                last_files = get_monitored_files()

    except KeyboardInterrupt:
        log.info("收到退出信号，停止帕鲁...")
        if child_process and child_process.poll() is None:
            child_process.terminate()
            child_process.wait()
        log.info("帕鲁已停止，守护进程退出")
        sys.exit(0)


if __name__ == "__main__":
    main()
