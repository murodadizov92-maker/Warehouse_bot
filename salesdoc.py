"""Sales Doctor API v2 klienti (https://{domain}/api/v2, hamma so'rov POST)."""
import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("salesdoc")


class SalesDocError(Exception):
    pass


class SalesDocClient:
    def __init__(self, domain, login, password, filial_id=None):
        self.url = f"https://{domain}/api/v2"
        self.login_name = login
        self.password = password
        self.filial_id = filial_id or None
        self._auth = None
        self._session = None

    async def close(self):
        if self._session:
            await self._session.close()

    async def _post(self, body):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        async with self._session.post(self.url, json=body) as resp:
            text = await resp.text()
            try:
                return json.loads(text)
            except ValueError:
                snippet = text[:200].replace("\n", " ")
                raise SalesDocError(
                    f"Sales Doctor JSON qaytarmadi (HTTP {resp.status}, {self.url}): {snippet!r}"
                )

    async def login(self):
        data = await self._post(
            {
                "method": "login",
                "auth": {"login": self.login_name, "password": self.password},
            }
        )
        if not data.get("status"):
            raise SalesDocError(f"Login xato: {data.get('error')}")
        res = data["result"]
        self._auth = {"userId": res["userId"], "token": res["token"]}
        log.info("Sales Doctor: login OK")

    async def call(self, method, params=None):
        for attempt in range(3):
            if not self._auth:
                await self.login()
            body = {"method": method, "auth": self._auth, "params": params or {}}
            if self.filial_id:
                body["filial"] = {"filial_id": self.filial_id}
            data = await self._post(body)
            if data.get("status"):
                return data
            err = data.get("error") or {}
            code = err.get("code")
            if code == 401:  # token eskirgan yoki boshqa joyda login qilingan
                self._auth = None
                continue
            if code == 429:  # limit
                await asyncio.sleep(10)
                continue
            raise SalesDocError(f"{method}: {err}")
        raise SalesDocError(f"{method}: qayta urinishlar tugadi")

    async def paginate(self, method, params, key, limit=1000, max_pages=200):
        """GET metodlarini sahifalab o'qib, result[key] elementlarini qaytaradi."""
        items = []
        for page in range(1, max_pages + 1):
            p = dict(params or {})
            p["limit"] = limit
            p["page"] = page
            data = await self.call(method, p)
            chunk = (data.get("result") or {}).get(key) or []
            items.extend(chunk)
            total = (data.get("pagination") or {}).get("total", 0)
            if not chunk or page * limit >= total:
                break
        return items
