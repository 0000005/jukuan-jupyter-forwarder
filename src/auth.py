import json
import os
import httpx
import logging

try:
    from .config import JQ_BASE_URL, JQ_PHONE, JQ_PWD, JQ_USER_ID, COOKIE_FILE
except ImportError:
    from config import JQ_BASE_URL, JQ_PHONE, JQ_PWD, JQ_USER_ID, COOKIE_FILE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jq_auth")

class JQAuth:
    def __init__(self):
        self.cookies = {}
        self.load_cookies()

    def load_cookies(self):
        """从本地文件加载 Cookie"""
        if os.path.exists(COOKIE_FILE):
            try:
                with open(COOKIE_FILE, "r") as f:
                    self.cookies = json.load(f)
                logger.info("已从文件加载 Cookie")
            except Exception as e:
                logger.error(f"加载 Cookie 失败: {e}")

    def save_cookies(self, cookies):
        """将 Cookie 保存到本地文件"""
        self.cookies = cookies
        try:
            with open(COOKIE_FILE, "w") as f:
                json.dump(self.cookies, f)
            logger.info("Cookie 已持久化到文件")
        except Exception as e:
            logger.error(f"保存 Cookie 失败: {e}")

    async def check_login_status(self):
        """检查当前会话是否有效.

        返回值:
        - True: 会话有效
        - False: 会话无效，需要重新登录
        - None: 检查过程出现瞬时网络异常，无法判断
        """
        if not self.cookies:
            return False
        
        # 访问聚宽的检查接口
        url = f"{JQ_BASE_URL}/user/index/isLogin"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{JQ_BASE_URL}/user/login/index",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with httpx.AsyncClient(cookies=self.cookies, trust_env=False, timeout=30.0) as client:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.error(f"检查登录接口返回非 200: {resp.status_code}")
                    return False
                try:
                    data = resp.json()
                    logger.info(f"检查登录状态返回: {data}")
                    # 聚宽 check 接口返回 {"data": {"isLogin": 1, ...}, "status": "0", ...}
                    if data.get("data", {}).get("isLogin") == 1:
                        return True
                except Exception:
                    logger.error(f"检查登录接口返回非 JSON: {resp.text[:200]}")
            except httpx.HTTPError as e:
                import traceback
                logger.error(f"检查登录状态请求失败: {e}\n{traceback.format_exc()}")
                return None
        return False

    async def login(self):
        """模拟登录聚宽"""
        logger.info("正在模拟登录聚宽...")
        # 1. 先访问一下主页获获取基础 cookie
        login_page_url = f"{JQ_BASE_URL}/user/login/index"
        do_login_url = f"{JQ_BASE_URL}/user/login/doLoginByText"
        
        async with httpx.AsyncClient(trust_env=False) as client:
            # 访问首页
            await client.get(login_page_url)
            
            # 执行登录
            payload = {
                "username": JQ_PHONE,
                "pwd": JQ_PWD
            }
            headers = {
                "X-Requested-With": "XMLHttpRequest",
                "Referer": login_page_url,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            
            resp = await client.post(do_login_url, data=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                # 聚宽返回 code "00000" 或 status "0" 表示成功
                if data.get("code") == "00000" or data.get("status") == "0":
                    logger.info("登录成功!")
                    
                    # 关键！必须访问一下 Jupyter 首页以获取 _xsrf cookie 和 jupyter 专用 cookie
                    jupyter_url = f"{JQ_BASE_URL}/user/{JQ_USER_ID}/tree"
                    await client.get(jupyter_url, follow_redirects=True)
                    
                    # 保存所有 cookie
                    all_cookies = {c.name: c.value for c in client.cookies.jar}
                    self.save_cookies(all_cookies)
                    return True
                else:
                    logger.error(f"登录失败: {data.get('msg', '未知错误')}")
            else:
                logger.error(f"登录请求失败, HTTP 状态码: {resp.status_code}")
        return False

    async def get_request_cookies(self):
        """获取用于转发请求的 Cookie，不主动做登录态探测"""
        if self.cookies:
            return self.cookies

        logger.info("本地无可用 Cookie, 尝试登录")
        if not await self.login():
            logger.error("自动登录失败")
            return None
        return self.cookies

    async def refresh_cookies_after_auth_failure(self):
        """后端明确要求登录时，直接强制重新登录并刷新 Jupyter 相关 Cookie"""
        logger.info("后端要求重新鉴权，强制重新登录聚宽并刷新 Cookie")
        if not await self.login():
            logger.error("自动登录失败")
            return None
        return self.cookies

    async def get_valid_cookies(self):
        """获取有效的 Cookie, 如失效则自动重新登录"""
        login_status = await self.check_login_status()
        if login_status is True:
            return self.cookies

        if login_status is None:
            logger.warning("登录状态检查失败，暂时复用当前 Cookie")
            return self.cookies

        if not login_status:
            logger.info("会话已失效, 尝试重新登录")
            if not await self.login():
                logger.error("自动登录失败")
                return None
        return self.cookies

if __name__ == "__main__":
    # 测试代码
    import asyncio
    auth = JQAuth()
    asyncio.run(auth.get_valid_cookies())
