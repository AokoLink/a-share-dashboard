# -*- coding: utf-8 -*-
"""首次安装:安装依赖 + 下载 ECharts 5.5.1 到 static/echarts.min.js。
用法: python install.py
"""
import os, subprocess, sys, urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ECHARTS_VERSION = "5.5.1"
ECHARTS_URLS = [
    f"https://cdn.jsdelivr.net/npm/echarts@{ECHARTS_VERSION}/dist/echarts.min.js",
    f"https://unpkg.com/echarts@{ECHARTS_VERSION}/dist/echarts.min.js",
    f"https://registry.npmmirror.com/echarts/{ECHARTS_VERSION}/files/dist/echarts.min.js",
]
STATIC_DIR = os.path.join(BASE_DIR, "static")


def install_deps():
    req = os.path.join(BASE_DIR, "requirements.txt")
    print("[1/2] 安装 Python 依赖...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", req], check=True)


def download_echarts():
    print("[2/2] 下载 ECharts %s ..." % ECHARTS_VERSION)
    os.makedirs(STATIC_DIR, exist_ok=True)
    dest = os.path.join(STATIC_DIR, "echarts.min.js")
    if os.path.exists(dest) and os.path.getsize(dest) > 500 * 1024:
        print("  已存在 echarts.min.js,跳过下载")
        return
    last_err = None
    for url in ECHARTS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=30).read()
            if len(data) < 500 * 1024:
                raise RuntimeError("文件过小(%d bytes),疑似错误页面" % len(data))
            with open(dest, "wb") as f:
                f.write(data)
            print("  下载成功: %s" % url)
            return
        except Exception as e:
            last_err = e
            print("  失败: %s (%s)" % (url, e))
    print("!! ECharts 下载失败:%s" % last_err)
    print("!! 请手动下载 https://cdn.jsdelivr.net/npm/echarts@%s/dist/echarts.min.js 放到 static/ 目录" % ECHARTS_VERSION)


def main():
    install_deps()
    download_echarts()
    print("\n完成!运行: python app.py 然后访问 http://127.0.0.1:8000")


if __name__ == "__main__":
    main()
