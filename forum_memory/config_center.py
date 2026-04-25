"""配置中心集成 — 从加密的集中配置服务获取敏感参数。

分层配置策略
============
优先级: ConfigCenter > 环境变量 > .env 文件 > 默认值

- 敏感凭证（数据库密码、API Key 等）→ ConfigCenter 加密存储
- 环境相关地址（DB URL、ES URL 等）→ ConfigCenter 按环境自动区分
- 业务参数（超时天数、阈值等）→ 环境变量 / .env（不敏感）
- ConfigCenter 引导参数（自身连接信息）→ 环境变量（最小集）

本地开发 (FM_DEPLOY_ENV=local 或未设置 FM_CC_BASE_URL) 不启用，
继续使用 .env 文件，无需安装内部加解密 SDK。
"""
import base64
import logging
import os

import requests
from his_decrypt import ADSKeyLoader, EncryptType, HisDecrypt


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 引导参数 — ConfigCenter 自身的连接信息，必须来自环境变量
# （这些是让 ConfigCenter 能工作的最小集，无法自举）
# ---------------------------------------------------------------------------
CC_BASE_URL = os.getenv("FM_CONFIG_CENTER_BASE_URL", "")
CC_APP_ID = os.getenv("FM_APP_ID", "")
CC_SUB_APP = os.getenv("FM_SUB_APP", "")
CC_REGION = os.getenv("FM_REGION", "")
CC_VERSION = os.getenv("FM_VERSION", "")
CC_APP_NAME = os.getenv("FM_APP_NAME", "")
CC_DEPLOY_UNIT_NAME = os.getenv("FM_DEPLOY_UNIT_NAME", "")
CC_DOCKER_ENV = os.getenv("FM_DOCKER_ENV", "")
HIS_ENV = os.getenv("FM_HIS_ENV", "local")
CC_TOKEN_CIPHER = os.getenv("FM_TOKEN_CIPHER", "")
CC_WORK_KEY_CIPHER = os.getenv("FM_WORK_KEY_CIPHER", "")
CC_TOKEN_CONFIG_PART = [os.getenv("FM_TOKEN_CONFIG_PART1", ""), os.getenv("FM_TOKEN_CONFIG_PART2", "")]
CC_APP_TOKEN_URL = os.getenv("FM_APP_TOKEN_URL", "")
CC_SERVICE_ENV = os.getenv("FM_SERVICE_ENV", "dev")
CC_DEPLOY_ENV = os.getenv("FM_DEPLOY_ENV", "")

# ---------------------------------------------------------------------------
# ConfigCenter 配置键 → Settings 字段 映射表
# 左侧: 配置中心注册的 key 名（即 j2c 响应中的 "user" 字段）
# 右侧: Settings 类中对应的字段名
# 根据实际在配置中心注册的 key 名称进行调整
# ---------------------------------------------------------------------------
CONFIG_CENTER_KEY_MAP: dict[str, str] = {
    "fm_sso_ak": "sso_ak",
    "fm_sso_sk": "sso_sk",
    "fm_sso_tenant_id": "sso_tenant_id",
    "fm_sso_user_scope": "sso_user_scope",
    "fm_idata_app_token": "idata_app_token",
    "fm_obs_ak": "obs_ak",
    "fm_obs_sk": "obs_sk",
    "fm_obs_endpoint": "obs_endpoint",
    "fm_obs_bucket": "obs_bucket",
    "fm_app_key": "app_key"
}


class ConfigCenter:
    _config = None
    _decrypter = None

    @classmethod
    def get_config_by_name(cls, name: str):
        configs = cls.get_configs()
        return configs.get(name)

    @classmethod
    def get_configs(cls):
        if cls._config is None:
            cls._config = cls.__get_config_from_config_center()
        return cls._config

    @classmethod
    def get_static_token(cls):
        return cls.__decrypt_j2c(CC_TOKEN_CIPHER, CC_WORK_KEY_CIPHER, CC_TOKEN_CONFIG_PART)

    @classmethod
    def __get_config_from_config_center(cls):
        result = {}
        url = (
            f"{CC_BASE_URL}?application_id={CC_APP_ID}"
            f"&sub_application_id={CC_SUB_APP}&region={CC_REGION}&environment={CC_DEPLOY_ENV}&version={CC_VERSION}"
        )
        header = {
            "Authorization": get_app_dynamic_token(),
        }
        resp = requests.get(url, headers=header, verify=False).json()
        if resp.get("errorCode") or resp.get("errorMsg"):
            return result
        for j2c in resp.get("j2c"):
            cipher = j2c["password"]
            work_key_cipher = j2c["work_key_cipher"]
            config_parts = j2c["config_parts"]
            plaintext = cls.__decrypt_j2c(cipher, work_key_cipher, config_parts)
            result[j2c["user"]] = plaintext
        return result

    @classmethod
    def __get_decrypter(cls):
        """初始化KeyLoader，并注册到解码器"""
        if cls._decrypter is not None:
            return cls._decrypter
        key_loader = ADSKeyLoader(CC_APP_NAME, CC_DEPLOY_UNIT_NAME, CC_DOCKER_ENV)
        cls._decrypter = HisDecrypt()
        cls._decrypter.register(key_loader)
        return cls._decrypter

    @classmethod
    def __decrypt_j2c(cls, cipher, work_key_cipher, config_parts):
        return cls.__get_decrypter().decrypt(config_parts, work_key_cipher, cipher, EncryptType.ADV_2_6)


APP_TOKEN = ConfigCenter.get_static_token()


def load_from_config_center() -> dict[str, str]:
    """从配置中心加载配置，返回 {Settings字段名: 值} 字典。

    返回值直接作为 Settings(**overrides) 的 init kwargs，
    优先级高于环境变量和 .env 文件。

    以下场景返回空字典（自动回退到环境变量）：
    - FM_DEPLOY_ENV == "local"（本地开发）
    - FM_CC_BASE_URL 未设置
    - 内部加解密 SDK 未安装
    - 配置中心不可达
    """
    if not CC_BASE_URL:
        return {}

    try:
        all_configs = ConfigCenter.get_configs()
    except Exception:
        logger.exception("ConfigCenter unavailable, falling back to env vars")
        return {}

    overrides: dict[str, str] = {}
    for cc_key, field_name in CONFIG_CENTER_KEY_MAP.items():
        value = all_configs.get(cc_key)
        if value is not None:
            overrides[field_name] = value
    return overrides


def get_app_dynamic_token() -> str:
    """Get dynamic authorization token for external API calls."""

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    body = {"appId": CC_APP_ID, "credential": base64.b64encode(APP_TOKEN.encode("utf-8")).decode("utf-8")}
    try:
        resp = requests.post(CC_APP_TOKEN_URL, headers=headers, json=body, verify=False)

        if not resp.ok:
            logger.warning("_get_app_token failed: %s", resp.reason)
            return ""

        return resp.json()["result"]
    except Exception:
        logger.exception("Get app dynamic token failed")
        return ""


if CC_SERVICE_ENV == "PROD":
    DB_CONNECTION = ConfigCenter.get_config_by_name("db_connection")
    ES_CONNECT_URL = ConfigCenter.get_config_by_name("es_connect_url")
else:
    DB_CONNECTION = os.getenv("FM_DB_CONNECTION", "")
    ES_CONNECT_URL = os.getenv("FM_ES_CONNECT_URL", "")