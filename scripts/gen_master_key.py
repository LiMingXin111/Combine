"""生成 SECRETS_MASTER_KEY（32 字节随机数的 base64），写入服务器环境变量或 .env。"""
import base64
import os

print(base64.b64encode(os.urandom(32)).decode())
