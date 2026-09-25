"""本地运行入口：python3 -m service_09251_010 serve [--db PATH] [--keys-file PATH]"""
from __future__ import annotations

import argparse
import json
import os
import sys
from wsgiref.simple_server import make_server

from .interfaces.wsgi_app import create_app
from .persistence.sqlite_store import SQLiteStore
from .services.auth import DEFAULT_DEV_KEYS, Authenticator
from .services.review_service import ReviewService


def default_db_path() -> str:
    """运行数据默认放在用户数据目录，绝不写入源码目录。"""
    env_path = os.environ.get("SERVICE_09251_010_DB")
    if env_path:
        return env_path
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, "service_09251_010", "review.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="service_09251_010", description="假期补能保供复盘服务")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="启动 HTTP 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", default=None, help="SQLite 数据库路径（默认取环境变量或用户数据目录）")
    serve.add_argument("--keys-file", default=None, help="API 密钥配置 JSON；缺省使用内置开发密钥")
    args = parser.parse_args(argv)

    if args.command == "serve":
        db_path = args.db or default_db_path()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        if args.keys_file:
            with open(args.keys_file, "r", encoding="utf-8") as handle:
                keys = json.load(handle)["keys"]
        else:
            keys = DEFAULT_DEV_KEYS
            print("警告：未提供 --keys-file，使用内置开发密钥，仅限本地调试。", file=sys.stderr)
        store = SQLiteStore(db_path)
        service = ReviewService(store)
        recovered = service.recover_interrupted()
        if recovered:
            print(f"已把 {recovered} 个中断的计算运行标记为可续算。", file=sys.stderr)
        app = create_app(service, Authenticator(keys))
        with make_server(args.host, args.port, app) as httpd:
            print(f"服务已启动: http://{args.host}:{args.port}  数据库: {db_path}")
            httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
