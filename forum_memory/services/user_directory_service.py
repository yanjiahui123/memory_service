"""External user directory API wrapper.

Encapsulates three external APIs:
1. get_user_info — lookup single user by account (w3account)
2. get_dept_employee_list_page — list department members (paginated)
3. search_member_information — fuzzy search users by name/account
"""

import logging

import requests

from forum_memory.config import get_settings

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100


def _get_app_token() -> str:
    """Get dynamic authorization token for external API calls."""
    settings = get_settings()
    return settings.idata_app_token


def _build_dept_path(user_info: dict) -> str:
    """Build '/' separated department path from l0_Name ~ l12_Name."""
    parts = []
    for i in range(13):
        name = (user_info.get(f"l{i}_Name") or "").strip()
        if name:
            parts.append(name)
    return "/".join(parts)


def _build_dept_levels(user_info: dict) -> dict:
    """Build dept_levels dict from l0~l12 code/name pairs."""
    levels = {}
    for i in range(13):
        code = (user_info.get(f"l{i}_Dept_Code") or "").strip()
        name = (user_info.get(f"l{i}_Name") or "").strip()
        if code or name:
            levels[f"l{i}"] = {"code": code, "name": name}
    return levels


def lookup_user(account: str) -> dict | None:
    """Lookup a single user by account (w3account).

    Returns standardized dict or None if not found.
    """
    settings = get_settings()
    headers = {"Authorization": _get_app_token()}
    params = {"Account": account, "lang": "zh"}
    try:
        resp = requests.get(
            settings.idata_user_info_url,
            params=params, headers=headers, verify=False, timeout=10,
        )
        if not resp.ok:
            logger.warning("lookup_user failed for %s: %s", account, resp.reason)
            return None
        data = resp.json()
    except Exception:
        logger.exception("lookup_user error for %s", account)
        return None

    w3 = (data.get("w3Account") or "").strip()
    if not w3:
        return None
    return {
        "w3account": w3,
        "name": (data.get("name") or "").strip(),
        "email": (data.get("person_Mail") or "").strip(),
        "dept_code": (data.get("dept_Code") or "").strip(),
        "dept_path": _build_dept_path(data),
        "dept_levels": _build_dept_levels(data),
    }


def list_dept_members(dept_code: str) -> list[dict]:
    """List all active members in a department (auto-pagination).

    Returns list of {w3account, name, dept_code}.
    """
    settings = get_settings()
    headers = {"Content-Type": "application/json", "Authorization": _get_app_token()}
    all_members: list[dict] = []
    page = 1

    while True:
        params = {
            "account_status": "1",
            "language": "CHN",
            "dept_code": dept_code,
            "search_type": "3",
            "pageSize": _PAGE_SIZE,
            "curPage": page,
        }
        try:
            resp = requests.get(
                settings.idata_dept_employee_url,
                headers=headers, params=params, verify=False, timeout=15,
            )
            body = resp.json() if resp.ok else {}
            if not resp.ok or body.get("code") != 200:
                logger.warning("list_dept_members failed page %d: %s", page, resp.reason)
                break
        except Exception:
            logger.exception("list_dept_members error page %d", page)
            break

        rows = body.get("data") or []
        for r in rows:
            all_members.append({
                "w3account": (r.get("w3account") or "").strip(),
                "name": (r.get("name") or r.get("full_name") or "").strip(),
                "dept_code": (r.get("dept_code") or "").strip(),
            })
        if len(rows) < _PAGE_SIZE:
            break
        page += 1

    return all_members


def search_users(keyword: str, page_size: int = 20) -> list[dict]:
    """Fuzzy search users by name or account.

    Returns list of dicts from the external search API.
    """
    settings = get_settings()
    headers = {"Content-Type": "application/json", "Authorization": _get_app_token()}
    params = {
        "lang": "zh",
        "searchValue": keyword,
        "searchType": "1",
        "pageSize": str(page_size),
        "page": "1",
    }
    try:
        resp = requests.get(
            settings.idata_member_search_url,
            headers=headers, params=params, verify=False, timeout=10,
        )
        if not resp.ok:
            logger.warning("search_users failed: %s", resp.reason)
            return []
        return resp.json().get("data", []) if isinstance(resp.json(), dict) else []
    except Exception:
        logger.exception("search_users error")
        return []
