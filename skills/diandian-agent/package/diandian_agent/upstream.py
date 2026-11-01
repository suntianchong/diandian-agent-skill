import httpx
from fastapi import HTTPException


class UpstreamFailure(Exception):
    def __init__(self, status: int, unknown: bool = False):
        self.status = status
        self.unknown = unknown


class Upstream:
    def __init__(self, base_url: str, transport=None):
        parsed = httpx.URL(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.host or parsed.query or parsed.fragment:
            raise ValueError("upstream URL must be an http(s) origin/base path")
        self.client = httpx.Client(base_url=base_url.rstrip("/") + "/", timeout=15,
                                   transport=transport, follow_redirects=False, trust_env=False)

    def call(self, token, method, path, body=None, params=None):
        if not path.startswith("/") or ".." in path:
            raise ValueError("invalid upstream path")
        # 未认证调用（注册、登录）不能发 `Authorization: Bearer `：空值在
        # httpx/httpcore 下是非法 header，真实底座请求会直接抛 LocalProtocolError。
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            r = self.client.request(method, "api/v2" + path,
                                    headers=headers,
                                    json=body, params=params)
        except httpx.TransportError:
            raise UpstreamFailure(502, unknown=method != "GET") from None
        if not 200 <= r.status_code < 300:
            # A 5xx may occur after the upstream committed the write.
            raise UpstreamFailure(r.status_code, unknown=method != "GET" and r.status_code >= 500)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            raise UpstreamFailure(502, unknown=method != "GET") from None

    def all(self, token, path):
        result = []
        for page in range(1, 10001):
            data = self.call(token, "GET", path, params={"page": page, "per_page": 100})
            if not isinstance(data, dict) or "items" not in data:
                raise UpstreamFailure(502)
            result.extend(data["items"] or [])
            if not data["items"] or page >= data.get("total_pages", 1):
                return result
        raise UpstreamFailure(502)

    def close(self):
        self.client.close()


def public_failure(exc):
    code = exc.status if exc.status in (401, 403, 404, 409, 422, 429) else 502
    return HTTPException(code, detail={"code": "upstream_error", "upstreamStatus": exc.status})
