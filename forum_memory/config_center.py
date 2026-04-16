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

import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 引导参数 — ConfigCenter 自身的连接信息，必须来自环境变量
# （这些是让 ConfigCenter 能工作的最小集，无法自举）
# ---------------------------------------------------------------------------
_CC_BASE_URL = os.getenv("FM_CC_BASE_URL", "")
_CC_APP_ID = os.getenv("FM_CC_APP_ID", "")
_CC_SUB_APP_ID = os.getenv("FM_CC_SUB_APP_ID", "")
_CC_REGION = os.getenv("FM_CC_REGION", "")
_CC_VERSION = os.getenv("FM_CC_VERSION", "")
_CC_APP_NAME = os.getenv("FM_CC_APP_NAME", "")
_CC_DEPLOY_UNIT_NAME = os.getenv("FM_CC_DEPLOY_UNIT_NAME", "")
_CC_DOCKER_ENV = os.getenv("FM_CC_DOCKER_ENV", "")
_DEPLOY_ENV = os.getenv("FM_DEPLOY_ENV", "local")

# ---------------------------------------------------------------------------
# ConfigCenter 配置键 → Settings 字段 映射表
# 左侧: 配置中心注册的 key 名（即 j2c 响应中的 "user" 字段）
# 右侧: Settings 类中对应的字段名
# 根据实际在配置中心注册的 key 名称进行调整
# ---------------------------------------------------------------------------
CONFIG_CENTER_KEY_MAP: dict[str, str] = {
    # ---- 敏感凭证 ----
    "db_connection": "database_url",
    "es_password": "es_password",
    "jwt_secret_key": "jwt_secret_key",
    "sso_ak": "sso_ak",
    "sso_sk": "sso_sk",
    "obs_ak": "obs_ak",
    "obs_sk": "obs_sk",
    "llm_api_key": "llm_api_key",
    "custom_api_key": "custom_api_key",
    "idata_app_token": "idata_app_token",
    # ---- 环境相关的服务地址 ----
    "es_url": "es_url",
    "es_username": "es_username",
    "custom_llm_url": "custom_llm_url",
    "custom_embed_url": "custom_embed_url",
    "custom_rerank_url": "custom_rerank_url",
    "sso_verify_url": "sso_verify_url",
    "obs_endpoint": "obs_endpoint",
    "rag_base_url": "rag_base_url",
    "idata_app_token_url": "idata_app_token_url",
    "idata_user_info_url": "idata_user_info_url",
    "idata_dept_employee_url": "idata_dept_employee_url",
    "idata_member_search_url": "idata_member_search_url",
}


class ConfigCenter:
    """集中配置中心客户端 — 获取并解密 J2C 加密配置。

    使用方式::

        value = ConfigCenter.get_config_by_name("db_connection")
    """

    _config: dict[str, str] | None = None
    _decrypter = None

    @classmethod
    def get_config_by_name(cls, name: str) -> str | None:
        return cls.get_configs().get(name)

    @classmethod
    def get_configs(cls) -> dict[str, str]:
        if cls._config is None:
            cls._config = cls._fetch_and_decrypt()
        return cls._config

    @classmethod
    def _fetch_and_decrypt(cls) -> dict[str, str]:
        import requests
        # TODO: 替换为实际的内部 SDK 包名
        from your_auth_sdk import get_app_dynamic_token

        result: dict[str, str] = {}
        url = (
            f"{_CC_BASE_URL}"
            f"?application_id={_CC_APP_ID}"
            f"&sub_application_id={_CC_SUB_APP_ID}"
            f"&region={_CC_REGION}"
            f"&environment={_DEPLOY_ENV}"
            f"&version={_CC_VERSION}"
        )
        resp = requests.get(
            url,
            headers={"Authorization": get_app_dynamic_token()},
            verify=False,
            timeout=10,
        ).json()
        if resp.get("errorCode") or resp.get("errorMsg"):
            logger.error("ConfigCenter error: %s", resp.get("errorMsg"))
            return result
        for j2c in resp.get("j2c", []):
            plaintext = cls._decrypt_j2c(
                j2c["password"], j2c["work_key_cipher"], j2c["config_parts"],
            )
            result[j2c["user"]] = plaintext
        return result

    @classmethod
    def _get_decrypter(cls):
        if cls._decrypter is not None:
            return cls._decrypter
        # TODO: 替换为实际的内部 SDK 包名
        from your_crypto_sdk import ADSKeyLoader, HisDecrypt

        key_loader = ADSKeyLoader(_CC_APP_NAME, _CC_DEPLOY_UNIT_NAME, _CC_DOCKER_ENV)
        cls._decrypter = HisDecrypt()
        cls._decrypter.register(key_loader)
        return cls._decrypter

    @classmethod
    def _decrypt_j2c(cls, cipher: str, work_key_cipher: str, config_parts: str) -> str:
        # TODO: 替换为实际的内部 SDK 包名
        from your_crypto_sdk import EncryptType

        return cls._get_decrypter().decrypt(
            config_parts, work_key_cipher, cipher, EncryptType.ADV_2_6,
        )

    @classmethod
    def reset(cls):
        """清除缓存（用于测试或配置热更新）。"""
        cls._config = None
        cls._decrypter = None


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
    if _DEPLOY_ENV == "local" or not _CC_BASE_URL:
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
