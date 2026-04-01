import os

BASE_DIR = os.path.dirname(__file__)

# 聚宽配置
JQ_BASE_URL = "https://www.joinquant.com"
JQ_USER_ID = "58026470838"
JQ_PHONE = "13142080234"
JQ_PWD = "123456Abc!"

# 代理服务器配置
PROXY_HOST = "0.0.0.0"
PROXY_PORT = 8888
# 代理入口鉴权 token
PROXY_TOKEN = "wobeidaohao"

# 本地持久化文件
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.json")
