"""交互式密码输入的登录及业务命令，不持久化凭据或会话令牌。"""

import argparse
from contextlib import closing
from dataclasses import asdict, is_dataclass
from getpass import getpass
import json
import sqlite3
import sys

from intellect.auth.application import AuthorizedApplication
from intellect.core.errors import IntellectError
from intellect.db import connect_database


def _json_value(value):
    """将业务结果中的数据类转成 JSON 对象，不读取登录服务内部状态。"""
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"不支持的输出类型：{type(value).__name__}")


def run(argv: list[str]) -> int:
    """解析命令后先登录，所有业务命令统一经过受保护入口。"""
    parser = argparse.ArgumentParser(prog="intellect")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("login", "search", "import", "user-add", "password-set", "category-create"):
        child = commands.add_parser(command)
        child.add_argument("--name", required=True, help="登录用户名")
        if command == "search":
            child.add_argument("question")
        elif command == "import":
            child.add_argument("archive")
            child.add_argument("--title", required=True)
            child.add_argument("--kind", required=True, choices=("txt", "pdf", "docx", "web"))
            child.add_argument("--confirm-duplicate", action="store_true")
        elif command == "user-add":
            child.add_argument("new_name")
            child.add_argument("--permissions", type=int, choices=(0, 1), default=1)
        elif command == "password-set":
            child.add_argument("user_id", type=int)
        elif command == "category-create":
            child.add_argument("category_name")
    args = parser.parse_args(argv)
    try:
        with closing(connect_database()) as connection:
            application = AuthorizedApplication(connection)
            token = application.auth.login(args.name, getpass("登录密码："))
            try:
                if args.command == "login":
                    result = application.auth.current_user(token)
                elif args.command == "search":
                    result = application.execute(token, "search", question=args.question)
                elif args.command == "import":
                    result = application.execute(
                        token, "document.import", archive=args.archive, title=args.title,
                        kind=args.kind, confirmed_duplicate=args.confirm_duplicate,
                    )
                elif args.command == "category-create":
                    result = application.execute(token, "category.create", name=args.category_name)
                else:
                    action = "user.create" if args.command == "user-add" else "user.password"
                    application.auth.require(token, action)
                    password = getpass("新密码：")
                    if password != getpass("确认新密码："):
                        raise IntellectError("两次密码不一致")
                    payload = ({"name": args.new_name, "permissions": args.permissions}
                               if args.command == "user-add" else {"user_id": args.user_id})
                    result = application.execute(token, action, password=password, **payload)
                print(json.dumps(result, default=_json_value, ensure_ascii=False))
            finally:
                application.auth.logout(token)
    except (IntellectError, sqlite3.Error, EOFError) as exc:
        code = exc.code if isinstance(exc, IntellectError) else "COMMAND_FAILED"
        print(f"{code}: {exc}", file=sys.stderr)
        return 1
    return 0
