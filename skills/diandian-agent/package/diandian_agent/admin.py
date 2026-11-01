"""管理后台（ADMIN-CONSOLE-01）：点点账号的三级角色与账号管理。

边界（与现有约定一致）：
- 用户事实是点点自有账号（`account` 记录），不新建第二套用户表；底座 Vikunja
  仍拥有任务与身份事实，本模块不改写底座用户，只管理点点账号的登录与角色。
- 角色与状态写在账号记录的 `role` / `status` 字段上，缺省为 `user` / `active`；
  禁用即拒绝登录（并在禁用时撤销已有会话），不删除底座用户，避免误伤任务归属。
- 权限矩阵：`superadmin` 管理所有人；`admin` 只能管理 `role=user` 的普通用户且
  不能改角色；普通用户无权访问 `/admin/*`。
- 引导：库里没有超管时，最早创建的账号自动成为超管（一次性，见 bootstrap）。
"""

import json
import time

from .service import fail

ROLES = ("superadmin", "admin", "user")
STATUSES = ("active", "disabled")


def role_of(account):
    role = account.get("role")
    return role if role in ROLES else "user"


def status_of(account):
    status = account.get("status")
    return status if status in STATUSES else "active"


class AdminConsole:
    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock

    # -- 引导 ---------------------------------------------------------------

    def bootstrap(self):
        """库中没有任何超管时，把最早创建的账号提升为超管。

        返回被提升的账号 id；无需提升时返回 None。仅供服务启动时调用。
        """
        accounts = self.store.all("account")
        if not accounts or any(role_of(a) == "superadmin" for a in accounts):
            return None
        first = min(accounts, key=lambda a: (a.get("created") or 0, a.get("id") or ""))
        with self.store.transaction():
            first["role"] = "superadmin"
            first["status"] = status_of(first)
            self.store.put("account", first["id"], first)
        return first["id"]

    # -- 查询 ---------------------------------------------------------------

    @staticmethod
    def public(account):
        return {
            "id": account["id"],
            "email": account.get("email", ""),
            "name": account.get("name", ""),
            "provider": account.get("provider"),
            "avatar": account.get("avatar", ""),
            "vikunjaUserId": account.get("vikunjaUserId"),
            "created": account.get("created"),
            "lastLogin": account.get("lastLogin"),
            "role": role_of(account),
            "status": status_of(account),
        }

    def me(self, account):
        account = self._load_actor(account)
        role = self._actor_role(account)
        return {
            "account": self.public(account),
            "permissions": {
                "manageAll": role == "superadmin",
                "manageUsers": role in ("superadmin", "admin"),
            },
        }

    def list_users(self, account):
        account = self._load_actor(account)
        role = self._actor_role(account)
        if role not in ("superadmin", "admin"):
            fail(403, "admin_role_required")
        items = []
        for other in self.store.all("account"):
            if role == "admin" and role_of(other) != "user":
                continue
            items.append(self.public(other))
        items.sort(key=lambda row: (row.get("created") or 0, row["id"]), reverse=True)
        return {"items": items, "total": len(items),
                "viewer": {"role": role, "accountId": account["id"]}}

    # -- 修改 ---------------------------------------------------------------

    def update(self, account, account_id, payload):
        account = self._load_actor(account)
        role = self._actor_role(account)
        if role not in ("superadmin", "admin"):
            fail(403, "admin_role_required")
        target = self.store.get("account", account_id)
        if not target:
            fail(404, "account_not_found")
        fields = payload.model_dump(exclude_unset=True)

        if role == "admin" and role_of(target) != "user":
            fail(403, "target_not_manageable")
        if "role" in fields and fields["role"] is not None and role != "superadmin":
            fail(403, "role_change_forbidden")
        # 不给自己降级/禁用，避免把最后一个超管锁在门外
        if target["id"] == account["id"] and ("role" in fields or "status" in fields):
            fail(400, "cannot_modify_self_privilege")

        if "name" in fields and fields["name"] is not None:
            target["name"] = fields["name"].strip()
        if fields.get("role"):
            target["role"] = fields["role"]
        if fields.get("status"):
            target["status"] = fields["status"]

        with self.store.transaction():
            self.store.put("account", target["id"], target)
            if status_of(target) == "disabled":
                self._revoke_sessions(target["id"])
        return {"account": self.public(target)}

    def delete(self, account, account_id):
        account = self._load_actor(account)
        role = self._actor_role(account)
        if role not in ("superadmin", "admin"):
            fail(403, "admin_role_required")
        target = self.store.get("account", account_id)
        if not target:
            fail(404, "account_not_found")
        if target["id"] == account["id"]:
            fail(400, "cannot_delete_self")
        if role == "admin" and role_of(target) != "user":
            fail(403, "target_not_manageable")

        with self.store.transaction():
            self.store.db.execute(
                "DELETE FROM records WHERE category='account' AND id=?", (target["id"],))
            self._revoke_sessions(target["id"])
        return {"deleted": True, "id": target["id"]}

    # -- 内部 ---------------------------------------------------------------

    def _load_actor(self, account):
        """会话只带公开字段，权限判断要回库读完整账号（含 role/status）。"""
        full = self.store.get("account", account["id"])
        if not full:
            fail(401, "identity_session_invalid")
        return full

    def _actor_role(self, account):
        if status_of(account) == "disabled":
            fail(403, "account_disabled")
        return role_of(account)

    def _revoke_sessions(self, account_id):
        """撤销指定账号的全部会话（禁用/删除后立即失效）。"""
        rows = self.store.db.execute(
            "SELECT id, value FROM records WHERE category='auth_session'").fetchall()
        for row in rows:
            try:
                record = json.loads(row[1])
            except (TypeError, ValueError):
                continue
            if record.get("accountId") == account_id:
                self.store.db.execute(
                    "DELETE FROM records WHERE category='auth_session' AND id=?", (row[0],))
