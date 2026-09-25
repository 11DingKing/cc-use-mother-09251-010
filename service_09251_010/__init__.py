"""假期补能保供复盘的服务端包入口。"""
PROJECT_CODE = "service_09251_010"


def project_info() -> dict[str, str]:
    """返回稳定的项目标识。"""
    return {"code": PROJECT_CODE, "title": "假期补能保供复盘"}


def create_service(db_path: str = ":memory:", *, clock=None, ids=None):
    """装配领域服务（延迟导入，保证干净环境可载入包元信息）。"""
    from .persistence.sqlite_store import SQLiteStore
    from .services.review_service import ReviewService

    return ReviewService(SQLiteStore(db_path), clock=clock, ids=ids)


def create_app(db_path: str = ":memory:", *, api_keys=None, clock=None, ids=None):
    """装配 WSGI 应用；api_keys 缺省时使用内置开发密钥（仅限本地调试）。"""
    from .interfaces.wsgi_app import create_app as _create_app
    from .services.auth import DEFAULT_DEV_KEYS, Authenticator

    service = create_service(db_path, clock=clock, ids=ids)
    return _create_app(service, Authenticator(api_keys or DEFAULT_DEV_KEYS))
