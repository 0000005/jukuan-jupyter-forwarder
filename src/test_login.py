import httpx
import asyncio

async def test():
    url = "https://www.joinquant.com/user/login/doLoginByText"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.joinquant.com/user/login/index",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    data = {
        "username": "13142080234",
        "pwd": "123456Abc!"
    }
    async with httpx.AsyncClient() as client:
        await client.get("https://www.joinquant.com/user/login/index")
        resp = await client.post(url, data=data, headers=headers)
        print(f"Status: {resp.status_code}")
        print(f"Headers: {resp.headers}")
        print(f"Body: {resp.text}")
        print(f"Cookies: {client.cookies}")

asyncio.run(test())
