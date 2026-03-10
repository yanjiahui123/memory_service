"""Admin API schemas."""

from uuid import UUID
from pydantic import BaseModel, Field


class ImportTopicsRequest(BaseModel):
    """批量导入历史帖子请求。"""
    namespace_id: UUID = Field(..., description="目标板块 UUID")
    dir_path: str = Field(..., description="服务器上 JSON 文件所在目录的绝对路径")
    workers: int = Field(default=4, ge=1, le=16, description="记忆提取并发线程数")
    skip_extraction: bool = Field(default=False, description="是否跳过记忆提取步骤")
    dry_run: bool = Field(default=False, description="演练模式，不写入数据库")


class ImportTopicsResult(BaseModel):
    """批量导入结果。"""
    total: int
    imported: int
    skipped: int
    failed: int
    resolved: int
    extracted: int
    extract_failed: int
