class ServiceError(Exception):
    """可安全返回给 API 调用方的业务异常。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def bad_request(detail: str) -> ServiceError:
    return ServiceError(400, detail)


def not_found(resource: str) -> ServiceError:
    return ServiceError(404, f"{resource}不存在")


def conflict(detail: str) -> ServiceError:
    return ServiceError(409, detail)
