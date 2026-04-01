import asyncio
import httpx
import logging
import json
import os
from auth import JQAuth
from config import JQ_BASE_URL, COOKIE_FILE

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("test_auth")

async def test_auth_flow():
    auth = JQAuth()
    
    logger.info("Step 1: Checking initial login status...")
    is_logged_in = await auth.check_login_status()
    logger.info(f"Initial login status: {is_logged_in}")
    
    if not is_logged_in:
        logger.info("Step 2: Attempting login...")
        success = await auth.login()
        logger.info(f"Login success: {success}")
        if not success:
            logger.error("Login failed, stopping test.")
            return

    logger.info("Step 3: Checking logic persistence...")
    is_logged_in_again = await auth.check_login_status()
    logger.info(f"Status after login/persistence: {is_logged_in_again}")
    
    if not is_logged_in_again:
        logger.error("CRITICAL: Session still invalid immediately after login!")
        # 调试：打印当前的 cookies
        logger.info(f"Current cookies in memory: {auth.cookies}")
    else:
        logger.info("SUCCESS: Auth flow working as expected.")

async def test_proxy_connection():
    # 测试连接聚宽服务器是否正常
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            logger.info("Testing connection to JoinQuant...")
            resp = await client.get(JQ_BASE_URL, timeout=10)
            logger.info(f"Connection OK, Status: {resp.status_code}")
        except Exception as e:
            logger.error(f"Connection failed: {type(e).__name__}: {e}")

if __name__ == "__main__":
    asyncio.run(test_proxy_connection())
    asyncio.run(test_auth_flow())
