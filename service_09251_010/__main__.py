"""python -m service_09251_010 启动 HTTP 服务。"""
import argparse

from .app import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="假期补能保供复盘服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=None, help="SQLite 路径，默认取 HOLIDAY_REVIEW_DB 或系统数据目录")
    args = parser.parse_args()

    httpd, _repo = serve(args.host, args.port, args.db)
    print(f"复盘服务已启动: http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
