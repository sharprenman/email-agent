"""阿里邮箱开放平台认证与 HTTP 客户端。"""

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx

from ...contracts import (
    ProviderAuthenticationError,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


class AliMailClient:
    """管理应用访问令牌并统一映射开放平台错误。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
        read_retries: int = 2,
    ) -> None:
        self._client = client
        self._client_id = client_id
        self._client_secret = client_secret
        self._read_retries = read_retries
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        retry_read: bool = True,
    ) -> Mapping[str, Any]:
        """发送 JSON API 请求；只对幂等读取进行有限重试。"""
        token = await self._get_access_token()
        retries_remaining = self._read_retries if retry_read else 0
        refreshed = False
        while True:
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.TimeoutException as exc:
                if retries_remaining:
                    retries_remaining -= 1
                    continue
                raise ProviderTimeoutError("阿里邮箱开放平台请求超时") from exc
            except httpx.RequestError as exc:
                if retries_remaining:
                    retries_remaining -= 1
                    continue
                raise ProviderUnavailableError("阿里邮箱开放平台暂时不可用") from exc

            if response.status_code == 401 and not refreshed:
                self._access_token = None
                token = await self._get_access_token()
                refreshed = True
                continue
            if response.status_code in {429, 500, 502, 503} and retries_remaining:
                retries_remaining -= 1
                continue
            if response.is_error:
                raise _map_http_error(response.status_code)
            if response.status_code == 204 or not response.content:
                return {}
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderUnavailableError("阿里邮箱开放平台返回了无效 JSON") from exc
            if not isinstance(payload, dict):
                raise ProviderUnavailableError("阿里邮箱开放平台返回了无效 JSON")
            return payload

    async def get_download_location(self, path: str) -> str:
        """读取附件下载会话返回的 HTTPS 地址。"""
        token = await self._get_access_token()
        refreshed = False
        while True:
            try:
                response = await self._client.get(
                    path,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError("阿里邮箱附件下载会话请求超时") from exc
            except httpx.RequestError as exc:
                raise ProviderUnavailableError("阿里邮箱附件下载会话暂时不可用") from exc
            if response.status_code == 401 and not refreshed:
                self._access_token = None
                token = await self._get_access_token()
                refreshed = True
                continue
            if response.is_error:
                raise _map_http_error(response.status_code)
            location = response.headers.get("location", "").strip()
            if not location and response.content:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ProviderUnavailableError("阿里邮箱附件下载会话响应无效") from exc
                if isinstance(payload, dict):
                    location = str(payload.get("location") or "").strip()
            if not location:
                raise ProviderUnavailableError("阿里邮箱附件下载会话缺少地址")
            return location

    async def download(self, location: str) -> bytes:
        """下载开放平台签发的 HTTPS 地址或当前 Host 绝对路径。"""
        if not (
            location.startswith("https://")
            or (location.startswith("/") and not location.startswith("//"))
        ):
            raise ProviderUnavailableError("阿里邮箱返回了不安全的附件下载地址")
        try:
            response = await self._client.get(location)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("阿里邮箱附件下载超时") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError("阿里邮箱附件暂时无法下载") from exc
        if response.is_error:
            raise _map_http_error(response.status_code)
        return response.content

    async def aclose(self) -> None:
        """关闭 HTTP 连接池。"""
        await self._client.aclose()

    async def _get_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._token_expires_at:
                return self._access_token
            try:
                response = await self._client.post(
                    "/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError("阿里邮箱访问令牌请求超时") from exc
            except httpx.RequestError as exc:
                raise ProviderUnavailableError("阿里邮箱认证服务暂时不可用") from exc
            if response.is_error:
                raise ProviderAuthenticationError("阿里邮箱应用凭证认证失败")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderAuthenticationError("阿里邮箱令牌响应不是有效 JSON") from exc
            access_token = str(payload.get("access_token") or "").strip()
            if not access_token:
                raise ProviderAuthenticationError("阿里邮箱令牌响应缺少 access_token")
            try:
                expires_in = int(payload.get("expires_in") or 3600)
            except (TypeError, ValueError) as exc:
                raise ProviderAuthenticationError("阿里邮箱令牌响应包含无效 expires_in") from exc
            self._access_token = access_token
            self._token_expires_at = time.monotonic() + max(1, expires_in - 60)
            return access_token


def _map_http_error(status: int) -> Exception:
    if status == 401:
        return ProviderAuthenticationError("阿里邮箱开放平台认证失败")
    if status == 403:
        return ProviderPermissionError("阿里邮箱开放平台权限不足")
    if status == 404:
        return ProviderNotFoundError("阿里邮箱资源不存在")
    if status == 429:
        return ProviderRateLimitError("阿里邮箱开放平台请求受限")
    if status in {408, 504}:
        return ProviderTimeoutError("阿里邮箱开放平台请求超时")
    return ProviderUnavailableError("阿里邮箱开放平台请求失败")
