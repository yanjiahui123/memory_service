"""
种子脚本 — 初始化超级管理员。

用法:
    cd forum_memory_backend
    python -m forum_memory.seed

说明:
    首次启动系统前必须运行此脚本，创建工号 00000000 的超级管理员。
    重复运行是安全的，已存在的用户会被跳过。
"""

from sqlmodel import Session, select
from forum_memory.database import engine, init_db
from forum_memory.models.user import User
from forum_memory.models.enums import SystemRole


SEED_USERS = [
    {
        "employee_id": "00000000",
        "username": "superadmin",
        "display_name": "超级管理员",
        "email": "admin@company.com",
        "role": SystemRole.SUPER_ADMIN,
    },
    # 以下为示例普通用户，可按需增删
    {
        "employee_id": "00000001",
        "username": "zhangsan",
        "display_name": "张三",
        "email": "zhangsan@company.com",
        "role": SystemRole.USER,
    },
    {
        "employee_id": "00000002",
        "username": "lisi",
        "display_name": "李四",
        "email": "lisi@company.com",
        "role": SystemRole.USER,
    },
]


def seed():
    """创建种子用户。"""
    init_db()
    with Session(engine) as session:
        for data in SEED_USERS:
            existing = session.exec(
                select(User).where(User.employee_id == data["employee_id"])
            ).first()
            if existing:
                print(f"  ⏭  工号 {data['employee_id']} ({data['display_name']}) 已存在，跳过")
                continue
            user = User(**data)
            session.add(user)
            session.commit()
            session.refresh(user)
            print(f"  ✅ 创建用户: {data['employee_id']} — {data['display_name']} ({data['role']})")

    print("\n🎉 种子数据初始化完成")
    print("   超级管理员工号: 00000000")
    print("   前端请求头需设置: X-Employee-Id: 00000000")


if __name__ == "__main__":
    seed()