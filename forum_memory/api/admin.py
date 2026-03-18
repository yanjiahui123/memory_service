"""Admin API routes — batch operations."""

import logging
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from forum_memory.api.deps import (
    check_board_permission, get_current_user, get_db, get_managed_namespace_ids,
    require_admin, require_any_admin,
)
from forum_memory.models.enums import SystemRole, Authority
from forum_memory.models.memory import Memory
from forum_memory.models.namespace import Namespace
from forum_memory.models.operation_log import OperationLog
from forum_memory.models.user import User
from forum_memory.schemas.admin import ImportTopicsRequest, ImportTopicsResult
from forum_memory.schemas.relation import (
    ContradictionResolveRequest,
    ContradictionResolveResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_super_admin(user: User = Depends(get_current_user)) -> User:
    """Dependency: only super_admin may call admin endpoints."""
    if user.role != SystemRole.SUPER_ADMIN:
        raise HTTPException(403, "仅超级管理员可执行此操作")
    return user


# ─── Background import job state ────────────────────────────────────────────

JobStatus = Literal["pending", "running", "done", "error"]


@dataclass
class ImportJob:
    job_id: str
    status: JobStatus = "pending"
    total_files: int = 0
    result: dict = field(default_factory=dict)
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: str | None = None


# In-process job store: job_id → ImportJob
# 进程内存储，重启后清空（对于长时批量任务已足够）
_import_jobs: dict[str, ImportJob] = {}
_import_jobs_lock = threading.Lock()


def _run_import_thread(job_id: str, tmp_path: Path, ns_uuid: UUID,
                       workers: int, skip_extraction: bool, dry_run: bool) -> None:
    """后台线程执行导入，完成后更新 job 状态。"""
    from forum_memory.scripts.import_topics import run_import

    with _import_jobs_lock:
        _import_jobs[job_id].status = "running"

    try:
        stats = run_import(
            dir_path=tmp_path,
            namespace_id=ns_uuid,
            workers=workers,
            skip_extraction=skip_extraction,
            dry_run=dry_run,
        )
        with _import_jobs_lock:
            job = _import_jobs[job_id]
            job.status = "done"
            job.result = stats
            job.finished_at = datetime.utcnow().isoformat()
        logger.info("import job %s done: %s", job_id, stats)
    except Exception as e:
        with _import_jobs_lock:
            job = _import_jobs[job_id]
            job.status = "error"
            job.error = str(e)
            job.finished_at = datetime.utcnow().isoformat()
        logger.exception("import job %s failed", job_id)
    finally:
        # 删除临时目录（线程负责清理）
        import shutil
        shutil.rmtree(tmp_path, ignore_errors=True)


# ─── Server-path import (super admin only) ──────────────────────────────────

@router.post("/import-topics", response_model=ImportTopicsResult)
def import_topics(
    req: ImportTopicsRequest,
    session: Session = Depends(get_db),
    _user: User = Depends(_require_super_admin),
) -> ImportTopicsResult:
    """通过服务器目录路径批量导入历史帖子（仅超级管理员）。"""
    ns = session.get(Namespace, req.namespace_id)
    if not ns:
        raise HTTPException(404, f"板块 {req.namespace_id} 不存在")

    dir_path = Path(req.dir_path)
    if not req.dry_run and not dir_path.is_dir():
        raise HTTPException(400, f"目录不存在或无法访问: {req.dir_path}")

    logger.info(
        "import-topics (path): namespace=%s  dir=%s  workers=%d  dry_run=%s",
        req.namespace_id, req.dir_path, req.workers, req.dry_run,
    )

    from forum_memory.scripts.import_topics import run_import
    try:
        stats = run_import(
            dir_path=dir_path,
            namespace_id=req.namespace_id,
            workers=req.workers,
            skip_extraction=req.skip_extraction,
            dry_run=req.dry_run,
        )
    except Exception as e:
        logger.exception("import_topics (path) failed")
        raise HTTPException(500, f"导入失败: {e}") from e

    return ImportTopicsResult(**stats)


# ─── File-upload import (super admin or board admin) ────────────────────────

class ImportJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    total_files: int
    created_at: str


def _read_json_from_zip(zf: zipfile.ZipFile, tmp_path: Path) -> int:
    """Extract JSON files from an open ZipFile into tmp_path. Returns count."""
    count = 0
    for member in zf.namelist():
        if not member.lower().endswith(".json") or member.startswith("__"):
            continue
        (tmp_path / Path(member).name).write_bytes(zf.read(member))
        count += 1
    return count


def _extract_zip_json(zip_bytes: bytes, tmp_path: Path, filename: str) -> int:
    """Extract JSON files from a ZIP archive into tmp_path. Returns count extracted."""
    zip_tmp = tmp_path / "upload.zip"
    zip_tmp.write_bytes(zip_bytes)
    try:
        with zipfile.ZipFile(zip_tmp) as zf:
            count = _read_json_from_zip(zf, tmp_path)
    except zipfile.BadZipFile as e:
        import shutil
        shutil.rmtree(tmp_path, ignore_errors=True)
        raise HTTPException(400, f"文件 {filename} 不是有效的 ZIP 压缩包") from e
    finally:
        zip_tmp.unlink(missing_ok=True)
    return count


def _save_uploaded_files(files: list[UploadFile], tmp_path: Path) -> int:
    """Save uploaded JSON/ZIP files to tmp_path. Returns JSON file count."""
    json_count = 0
    for uf in files:
        filename = uf.filename or "unknown"
        content = uf.file.read()
        if filename.lower().endswith(".zip"):
            json_count += _extract_zip_json(content, tmp_path, filename)
        elif filename.lower().endswith(".json"):
            (tmp_path / Path(filename).name).write_bytes(content)
            json_count += 1
        else:
            logger.warning("Skipping unsupported file type: %s", filename)
    return json_count


@router.post("/import-topics/upload", response_model=ImportJobResponse)
def import_topics_upload(
    namespace_id: str = Form(..., description="目标板块 UUID"),
    workers: int = Form(default=4, ge=1, le=16),
    skip_extraction: bool = Form(default=False),
    dry_run: bool = Form(default=False),
    files: list[UploadFile] = File(..., description="JSON 文件或 ZIP 压缩包"),
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportJobResponse:
    """通过文件上传批量导入历史帖子（超级管理员或板块管理员）。

    立即返回 job_id，后台异步执行。通过 GET /admin/import-jobs/{job_id} 查询进度。
    """
    try:
        ns_uuid = UUID(namespace_id)
    except ValueError as e:
        raise HTTPException(400, "namespace_id 格式不正确") from e

    ns = session.get(Namespace, ns_uuid)
    if not ns:
        raise HTTPException(404, f"板块 {ns_uuid} 不存在")
    check_board_permission(ns_uuid, session, user)
    if not files:
        raise HTTPException(400, "未上传任何文件")

    # 注意：不使用 with 语句，由后台线程负责清理
    tmp_path = Path(tempfile.mkdtemp(prefix="fm_import_"))
    json_count = _save_uploaded_files(files, tmp_path)

    if json_count == 0:
        import shutil
        shutil.rmtree(tmp_path, ignore_errors=True)
        raise HTTPException(400, "未找到任何 JSON 文件（支持直接上传 .json 或包含 .json 的 .zip）")

    job_id = str(uuid.uuid4())
    job = ImportJob(job_id=job_id, total_files=json_count)
    with _import_jobs_lock:
        _import_jobs[job_id] = job

    logger.info(
        "import-topics (upload): namespace=%s  files=%d  workers=%d  user=%s  dry_run=%s  job=%s",
        ns_uuid, json_count, workers, user.employee_id, dry_run, job_id,
    )

    t = threading.Thread(
        target=_run_import_thread,
        args=(job_id, tmp_path, ns_uuid, workers, skip_extraction, dry_run),
        daemon=True,
        name=f"import-{job_id[:8]}",
    )
    t.start()

    return ImportJobResponse(
        job_id=job_id,
        status="pending",
        total_files=json_count,
        created_at=job.created_at,
    )


class ImportJobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    total_files: int
    result: dict | None = None
    error: str | None = None
    created_at: str
    finished_at: str | None = None


@router.get("/import-jobs/{job_id}", response_model=ImportJobStatusResponse)
def get_import_job(
    job_id: str,
    _admin: User = Depends(require_any_admin),
) -> ImportJobStatusResponse:
    """查询批量导入任务的进度与结果。"""
    with _import_jobs_lock:
        job = _import_jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"任务 {job_id} 不存在（可能服务已重启）")
    return ImportJobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        total_files=job.total_files,
        result=job.result if job.result else None,
        error=job.error,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


def _apply_namespace_filter(stmt, namespace_id, session, user):
    """为查询语句追加板块过滤：指定 namespace_id 或 board_admin 自动限制到管理的板块。"""
    if namespace_id:
        stmt = stmt.where(Memory.namespace_id == namespace_id)
    elif user.role == SystemRole.BOARD_ADMIN:
        ns_ids = get_managed_namespace_ids(session, user)
        if ns_ids:
            stmt = stmt.where(Memory.namespace_id.in_(ns_ids))
        else:
            stmt = stmt.where(Memory.namespace_id.is_(None))
    return stmt


# ─── Quality Alerts ──────────────────────────────────────────────────────────

class QualityAlertItem(BaseModel):
    id: UUID
    namespace_id: UUID
    content: str
    authority: str
    quality_score: float
    wrong_count: int
    outdated_count: int
    useful_count: int
    not_useful_count: int
    cite_count: int
    resolved_citation_count: int
    model_config = {"from_attributes": True}


class QualityAlertList(BaseModel):
    items: list[QualityAlertItem]
    total: int


@router.get("/quality-alerts", response_model=QualityAlertList)
def list_quality_alerts(
    namespace_id: UUID | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_db),
    user: User = Depends(require_any_admin),
) -> QualityAlertList:
    """返回 pending_human_confirm=True 的记忆（质量告警列表），按 wrong_count 降序。

    可按板块过滤。超级管理员或板块管理员均可访问。
    """
    if namespace_id:
        check_board_permission(namespace_id, session, user)

    stmt = (
        select(Memory)
        .where(Memory.pending_human_confirm.is_(True))
        .order_by(Memory.wrong_count.desc(), Memory.updated_at.desc())
    )
    stmt = _apply_namespace_filter(stmt, namespace_id, session, user)

    total = len(session.exec(stmt).all())
    items = list(session.exec(stmt.offset((page - 1) * size).limit(size)).all())
    return QualityAlertList(items=items, total=total)


@router.post("/quality-alerts/{memory_id}/dismiss", response_model=QualityAlertItem)
def dismiss_quality_alert(
    memory_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(require_any_admin),
) -> QualityAlertItem:
    """管理员复核后关闭质量告警（清除 pending_human_confirm 标记）。"""
    memory = session.get(Memory, memory_id)
    if not memory:
        raise HTTPException(404, "记忆不存在")
    check_board_permission(memory.namespace_id, session, user)
    memory.pending_human_confirm = False
    session.commit()
    session.refresh(memory)
    return memory


# ─── Contradictions ───────────────────────────────────────────────────────────

@router.get("/contradictions")
def list_contradictions(
    namespace_id: UUID | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_db),
    user: User = Depends(require_any_admin),
):
    """List all CONTRADICTS memory pairs for admin review."""
    if namespace_id:
        check_board_permission(namespace_id, session, user)

    from forum_memory.services import relation_service
    from forum_memory.schemas.relation import RelationRead

    ns_ids = None
    if not namespace_id and user.role == SystemRole.BOARD_ADMIN:
        ns_ids = get_managed_namespace_ids(session, user)
        if not ns_ids:
            return {"items": [], "total": 0}

    items, total = relation_service.list_contradictions(
        session, namespace_id, page, size, namespace_ids=ns_ids,
    )
    return {"items": [RelationRead.model_validate(r) for r in items], "total": total}


@router.post("/contradictions/{relation_id}/resolve",
             response_model=ContradictionResolveResponse)
def resolve_contradiction_endpoint(
    relation_id: UUID,
    body: ContradictionResolveRequest,
    session: Session = Depends(get_db),
    admin_user: User = Depends(require_any_admin),
):
    """Resolve a CONTRADICTS relation: keep_source, keep_target, or keep_both."""
    from forum_memory.models.memory_relation import MemoryRelation
    from forum_memory.services import relation_service

    rel = session.get(MemoryRelation, relation_id)
    if not rel:
        raise HTTPException(404, "关系不存在")
    source_mem = session.get(Memory, rel.source_memory_id)
    if source_mem:
        check_board_permission(source_mem.namespace_id, session, admin_user)

    success, detail = relation_service.resolve_contradiction(
        session=session,
        relation_id=relation_id,
        action=body.action,
        reason=body.reason,
        operator_id=admin_user.id,
    )
    if not success:
        raise HTTPException(400, detail)
    return ContradictionResolveResponse(
        resolved=True, action=body.action, detail=detail,
    )


# ─── Audit Logs ───────────────────────────────────────────────────────────────

class AuditLogItem(BaseModel):
    id: UUID
    memory_id: UUID
    operation: str
    operator_id: UUID | None
    operator_type: str
    reason: str | None
    before_snapshot: dict | None
    after_snapshot: dict | None
    created_at: datetime
    model_config = {"from_attributes": True}


@router.get("/audit-logs")
def list_audit_logs(
    memory_id: UUID | None = Query(None),
    namespace_id: UUID | None = Query(None),
    operation: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_db),
    user: User = Depends(require_any_admin),
):
    """查询操作审计日志，支持按记忆ID、板块和操作类型过滤。

    板块管理员仅可查看其管理板块内的日志。
    """
    stmt, count_stmt = _build_audit_log_query(
        session, user, memory_id, namespace_id, operation,
    )

    total = session.exec(count_stmt).one()
    items = list(session.exec(stmt.offset((page - 1) * size).limit(size)).all())

    from fastapi.responses import JSONResponse
    data = [AuditLogItem.model_validate(item).model_dump(mode="json") for item in items]
    return JSONResponse(content=data, headers={"X-Total-Count": str(total)})


def _build_audit_log_query(
    session: Session,
    user: User,
    memory_id: UUID | None,
    namespace_id: UUID | None,
    operation: str | None,
):
    """Construct audit-log SELECT + COUNT respecting board_admin scope."""
    from sqlmodel import func

    stmt = select(OperationLog).order_by(OperationLog.created_at.desc())
    count_stmt = select(func.count()).select_from(OperationLog)

    # board_admin: restrict to memories belonging to managed namespaces
    if user.role == SystemRole.BOARD_ADMIN:
        ns_ids = get_managed_namespace_ids(session, user)
        mem_sub = select(Memory.id).where(Memory.namespace_id.in_(ns_ids))
        stmt = stmt.where(OperationLog.memory_id.in_(mem_sub))
        count_stmt = count_stmt.where(OperationLog.memory_id.in_(mem_sub))

    if namespace_id:
        ns_sub = select(Memory.id).where(Memory.namespace_id == namespace_id)
        stmt = stmt.where(OperationLog.memory_id.in_(ns_sub))
        count_stmt = count_stmt.where(OperationLog.memory_id.in_(ns_sub))
    if memory_id:
        stmt = stmt.where(OperationLog.memory_id == memory_id)
        count_stmt = count_stmt.where(OperationLog.memory_id == memory_id)
    if operation:
        stmt = stmt.where(OperationLog.operation == operation)
        count_stmt = count_stmt.where(OperationLog.operation == operation)

    return stmt, count_stmt
