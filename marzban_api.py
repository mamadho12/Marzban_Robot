"""
کلاینت ساده برای API پنل مرزبان (Marzban).
مستندات API معمولاً روی MARZBAN_URL/docs در دسترسه.
"""
import time
from typing import Optional

import httpx

from config import MARZBAN_URL, MARZBAN_USERNAME, MARZBAN_PASSWORD, LOCATIONS


class MarzbanError(Exception):
    pass


class MarzbanAPI:
    def __init__(self):
        self._client = httpx.AsyncClient(base_url=MARZBAN_URL, timeout=20)
        self._token: Optional[str] = None
        self._token_expiry: float = 0

    async def close(self):
        await self._client.aclose()

    async def _ensure_token(self):
        # توکن رو کش می‌کنیم و کمی زودتر از انقضا رفرش می‌کنیم
        if self._token and time.time() < self._token_expiry:
            return
        resp = await self._client.post(
            "/api/admin/token",
            data={"username": MARZBAN_USERNAME, "password": MARZBAN_PASSWORD},
        )
        if resp.status_code != 200:
            raise MarzbanError(f"ورود به پنل ناموفق بود ({resp.status_code}): {resp.text}")
        data = resp.json()
        self._token = data["access_token"]
        # مرزبان معمولا مدت اعتبار مشخص نمی‌کنه، محافظه‌کارانه ۵۰ دقیقه در نظر می‌گیریم
        self._token_expiry = time.time() + 50 * 60

    async def _request(self, method: str, path: str, retry: bool = True, **kwargs):
        await self._ensure_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token}"
        resp = await self._client.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 401 and retry:
            # توکن رو باطل و یک بار دیگه امتحان کن
            self._token = None
            return await self._request(method, path, retry=False, **kwargs)
        if resp.status_code >= 400:
            raise MarzbanError(f"خطا در {path} ({resp.status_code}): {resp.text}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ---------- کاربران ----------

    async def list_users(self, offset: int = 0, limit: int = 50):
        data = await self._request("GET", "/api/users", params={"offset": offset, "limit": limit})
        return data.get("users", [])

    async def list_all_users(self):
        """همه‌ی کاربرها رو صفحه‌به‌صفحه می‌گیره (برای هشدار و بک‌آپ)."""
        users = []
        offset = 0
        limit = 100
        while True:
            batch = await self.list_users(offset=offset, limit=limit)
            if not batch:
                break
            users.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        return users

    async def get_user(self, username: str):
        return await self._request("GET", f"/api/user/{username}")

    async def get_inbounds(self):
        return await self._request("GET", "/api/inbounds")

    async def _default_proxies_and_inbounds(self):
        """با توجه به این‌باندهای موجود روی سرور نود، proxies/inbounds پیش‌فرض رو می‌سازه."""
        inbounds_data = await self.get_inbounds()
        proxies = {}
        inbounds = {}
        for protocol, inbound_list in inbounds_data.items():
            if not inbound_list:
                continue
            proxies[protocol] = {}
            inbounds[protocol] = [ib["tag"] for ib in inbound_list]
        if not proxies:
            raise MarzbanError("هیچ این‌باندی روی نود پیدا نشد؛ اول یک این‌باند در پنل مرزبان تعریف کن.")
        return proxies, inbounds

    async def _proxies_and_inbounds_for_locations(self, location_names: list[str]):
        """ساخت proxies و inbounds فقط برای لوکیشن‌های انتخاب‌شده"""
        if not location_names:
            raise MarzbanError("حداقل یک لوکیشن باید انتخاب شود")

        selected_tags = set()
        for name in location_names:
            if name not in LOCATIONS:
                raise MarzbanError(f"لوکیشن نامعتبر: {name}")
            selected_tags.update(LOCATIONS[name])

        inbounds_data = await self.get_inbounds()
        proxies = {}
        inbounds = {}

        for protocol, inbound_list in inbounds_data.items():
            matching_tags = [ib["tag"] for ib in inbound_list if ib["tag"] in selected_tags]
            if matching_tags:
                proxies[protocol] = {}
                inbounds[protocol] = matching_tags

        if not proxies:
            raise MarzbanError("هیچ اینباند معتبری برای لوکیشن‌های انتخاب‌شده پیدا نشد")

        return proxies, inbounds

    async def create_user(self, username: str, data_limit_gb: float, expire_days: int,
                          location_names: list[str] | None = None):
        if location_names is None:
            proxies, inbounds = await self._default_proxies_and_inbounds()
        else:
            proxies, inbounds = await self._proxies_and_inbounds_for_locations(location_names)

        body = {
            "username": username,
            "proxies": proxies,
            "inbounds": inbounds,
            "status": "active",
        }
        if data_limit_gb and data_limit_gb > 0:
            body["data_limit"] = int(data_limit_gb * 1024 ** 3)
        else:
            body["data_limit"] = 0  # نامحدود
        if expire_days and expire_days > 0:
            body["expire"] = int(time.time()) + expire_days * 86400
        else:
            body["expire"] = 0  # نامحدود
        return await self._request("POST", "/api/user", json=body)

    async def set_user_locations(self, username: str, location_names: list[str]):
        """تغییر لوکیشن‌های یک کاربر موجود"""
        proxies, inbounds = await self._proxies_and_inbounds_for_locations(location_names)
        return await self.modify_user(username, proxies=proxies, inbounds=inbounds)

    async def modify_user(self, username: str, **fields):
        return await self._request("PUT", f"/api/user/{username}", json=fields)

    async def delete_user(self, username: str):
        return await self._request("DELETE", f"/api/user/{username}")

    async def reset_user_data(self, username: str):
        return await self._request("POST", f"/api/user/{username}/reset")

    async def add_days(self, username: str, days: int):
        user = await self.get_user(username)
        current_expire = user.get("expire") or 0
        base = current_expire if current_expire and current_expire > time.time() else int(time.time())
        new_expire = base + days * 86400
        return await self.modify_user(username, expire=new_expire)

    async def add_data_gb(self, username: str, gb: float):
        user = await self.get_user(username)
        current_limit = user.get("data_limit") or 0
        new_limit = current_limit + int(gb * 1024 ** 3)
        return await self.modify_user(username, data_limit=new_limit)

    async def get_system_stats(self):
        return await self._request("GET", "/api/system")


marzban = MarzbanAPI()
