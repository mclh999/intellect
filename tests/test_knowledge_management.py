"""T1.5：分类/别名的业务约束、直接关联 FTS 与事务边界。"""

from __future__ import annotations

import json
from contextlib import closing

import pytest

from intellect.db import connect_database, initialize_database
from intellect.knowledge import (
    AliasService, CategorySeed, CategoryService, KnowledgeManagementError,
)
from intellect.knowledge import aliases as alias_module
from intellect.knowledge import categories as category_module
from intellect.knowledge import fts as fts_module
from intellect.knowledge.fts import refresh_knowledge
from intellect.search.tokenization import tokenize


@pytest.fixture
def database_path(tmp_path):
    """执行 `database_path` 对应的功能并返回处理结果。 参数包括：`tmp_path`。"""
    path = tmp_path / "management.db"
    initialize_database(path)
    return path


@pytest.fixture
def connection(database_path):
    """执行 `connection` 对应的功能并返回处理结果。 参数包括：`database_path`。"""
    with closing(connect_database(database_path)) as database:
        yield database


@pytest.fixture
def categories(connection):
    """执行 `categories` 对应的功能并返回处理结果。 参数包括：`connection`。"""
    return CategoryService(connection)


@pytest.fixture
def aliases(connection):
    """执行 `aliases` 对应的功能并返回处理结果。 参数包括：`connection`。"""
    return AliasService(connection)


def knowledge(database, *, status=0, title="事务隔离", content="原子性与可见性需要完整上下文"):
    """执行 `knowledge` 对应的功能并返回处理结果。 参数包括：`database`、`status`、`title`、`content`。"""
    with database:
        model_id = database.execute(
            "INSERT INTO embedding_model (model_name, dimension) VALUES (?, 1024)",
            (f"model-{database.execute('SELECT count(*) FROM knowledge').fetchone()[0]}",),
        ).lastrowid
        knowledge_id = database.execute("INSERT INTO knowledge (status) VALUES (?)", (status,)).lastrowid
        version_id = database.execute(
            "INSERT INTO knowledge_version (knowledge_id, version_no, title, content, content_hash, change_type) "
            "VALUES (?, 1, ?, ?, 'test-hash', 0)", (knowledge_id, title, content),
        ).lastrowid
        database.execute(
            "UPDATE knowledge SET current_version_id = ?, indexed_version_id = ?, "
            "indexed_embedding_model_id = ? WHERE id = ?", (version_id, version_id, model_id, knowledge_id),
        )
        refresh_knowledge(database, knowledge_id)
    return knowledge_id


def fts_row(database, knowledge_id):
    """执行 `fts_row` 对应的功能并返回处理结果。 参数包括：`database`、`knowledge_id`。"""
    return database.execute(
        "SELECT rowid, title_tokens, content_tokens, alias_tokens, category_tokens, knowledge_id, version_id "
        "FROM knowledge_fts WHERE rowid = ?", (knowledge_id,),
    ).fetchone()


def matches(database, text):
    """执行 `matches` 对应的功能并返回处理结果。 参数包括：`database`、`text`。"""
    query = " OR ".join('"' + word.replace('"', '""') + '"' for word in tokenize(text))
    return [row[0] for row in database.execute(
        "SELECT knowledge_id FROM knowledge_fts WHERE knowledge_fts MATCH ? ORDER BY rowid", (query,)
    )]


class TestCategoryTree:
    def test_initial_tree_created_from_explicit_input(self, categories):
        """验证 `initial_tree_created_from_explicit_input` 所代表的行为。 参数包括：`categories`。"""
        created = categories.create_tree((
            CategorySeed("计算机科学", (CategorySeed("数据库"), CategorySeed("网络"))),
            CategorySeed("其他领域"),
        ))
        assert [value.name for value in created] == ["计算机科学", "数据库", "网络", "其他领域"]
        assert [value.parent_id for value in created] == [None, created[0].id, created[0].id, None]
        assert categories.list_all() == created

    @pytest.mark.parametrize("bad_seed", [None, "错误节点", CategorySeed(7)])
    def test_initial_tree_partial_failure_rolls_back(self, categories, bad_seed):
        """验证 `initial_tree_partial_failure_rolls_back` 所代表的行为。 参数包括：`categories`、`bad_seed`。"""
        with pytest.raises(KnowledgeManagementError):
            categories.create_tree((CategorySeed("已写根", (CategorySeed("已写子"),)), bad_seed))
        assert categories.list_all() == []

    def test_self_and_descendant_parent_rejected(self, categories, connection):
        """验证 `self_and_descendant_parent_rejected` 所代表的行为。 参数包括：`categories`、`connection`。"""
        root, child, leaf = categories.create_tree((CategorySeed("根", (CategorySeed("子", (CategorySeed("叶"),)),)),))
        for parent_id in (root.id, child.id, leaf.id):
            with pytest.raises(KnowledgeManagementError, match="循环"):
                categories.move(root.id, parent_id, reason="错误移动")
        assert categories.get(root.id).parent_id is None
        assert connection.execute("SELECT count(*) FROM change_log").fetchone()[0] == 0

    def test_missing_parent_and_corrupted_parent_chain_rejected(self, categories, connection):
        """验证 `missing_parent_and_corrupted_parent_chain_rejected` 所代表的行为。 参数包括：`categories`、`connection`。"""
        with pytest.raises(KnowledgeManagementError, match="不存在"):
            categories.create("孤立分类", parent_id=999)
        root, child = categories.create_tree((CategorySeed("根", (CategorySeed("子"),)),))
        with connection:
            connection.execute("UPDATE category SET parent_id = ? WHERE id = ?", (child.id, root.id))
        with pytest.raises(KnowledgeManagementError, match="循环"):
            categories.create("新节点", parent_id=child.id)
        assert len(categories.list_all()) == 2

    def test_rename_and_move_log_full_snapshots(self, categories, connection):
        """验证 `rename_and_move_log_full_snapshots` 所代表的行为。 参数包括：`categories`、`connection`。"""
        root, other, child = categories.create_tree((CategorySeed("根"), CategorySeed("另一根", (CategorySeed("旧名称"),))))
        renamed = categories.rename(child.id, "新名称", reason="规范名称", operator="system")
        moved = categories.move(child.id, root.id, reason="调整层级", operator="system")
        assert renamed.name == "新名称"
        assert moved.parent_id == root.id
        rows = connection.execute(
            "SELECT target_type, target_id, old_value, new_value, change_reason, operator_id "
            "FROM change_log ORDER BY id"
        ).fetchall()
        assert [(row[0], row[1], row[4], row[5]) for row in rows] == [
            (0, child.id, "规范名称", "system"), (0, child.id, "调整层级", "system"),
        ]
        assert json.loads(rows[0][2])["name"] == "旧名称"
        assert json.loads(rows[0][3])["name"] == "新名称"
        assert json.loads(rows[1][2])["parent_id"] == other.id
        assert json.loads(rows[1][3])["parent_id"] == root.id

    def test_noop_does_not_log(self, categories, connection):
        """验证 `noop_does_not_log` 所代表的行为。 参数包括：`categories`、`connection`。"""
        root = categories.create("根")
        categories.rename(root.id, "根", reason="不变")
        categories.move(root.id, None, reason="不变")
        categories.set_status(root.id, 0, reason="不变")
        assert connection.execute("SELECT count(*) FROM change_log").fetchone()[0] == 0

    @pytest.mark.parametrize("operation", ["rename", "move", "set_status", "delete"])
    def test_category_changes_require_reason(self, categories, operation):
        """验证 `category_changes_require_reason` 所代表的行为。 参数包括：`categories`、`operation`。"""
        root = categories.create("根")
        arguments = {"rename": ("新名",), "move": (None,), "set_status": (1,), "delete": ()}
        with pytest.raises(KnowledgeManagementError, match="原因"):
            getattr(categories, operation)(root.id, *arguments[operation], reason=" ")
        assert categories.get(root.id) == root


class TestCategoryFts:
    def test_multiple_direct_categories_survive_one_disabled(self, categories, aliases, connection):
        """验证 `multiple_direct_categories_survive_one_disabled` 所代表的行为。 参数包括：`categories`、`aliases`、`connection`。"""
        first, second = categories.create_tree((CategorySeed("FirstCategory"), CategorySeed("SecondCategory")))
        knowledge_id = knowledge(connection)
        for category in (first, second):
            categories.attach_knowledge(category.id, knowledge_id)
        alias = aliases.add(1, second.id, "SecondAlias", confirmed=True)
        categories.set_status(first.id, 1, reason="只停用一个")
        assert fts_row(connection, knowledge_id)[4] == "SecondCategory SecondAlias"
        aliases.delete(alias.id)
        assert fts_row(connection, knowledge_id)[4] == "SecondCategory"

    def test_enabled_direct_category_names_and_aliases_only(self, categories, aliases, connection):
        """验证 `enabled_direct_category_names_and_aliases_only` 所代表的行为。 参数包括：`categories`、`aliases`、`connection`。"""
        root, child = categories.create_tree((CategorySeed("AncestorOnly", (CategorySeed("DirectOnly"),)),))
        aliases.add(1, root.id, "AncestorAlias", confirmed=True)
        aliases.add(1, child.id, "DirectAlias", confirmed=True)
        disabled = categories.create("DisabledOnly")
        categories.set_status(disabled.id, 1, reason="停用")
        knowledge_id = knowledge(connection)
        categories.attach_knowledge(child.id, knowledge_id)
        categories.attach_knowledge(disabled.id, knowledge_id)
        assert fts_row(connection, knowledge_id)[4] == "DirectOnly DirectAlias"
        assert matches(connection, "DirectAlias") == [knowledge_id]
        assert matches(connection, "AncestorOnly") == []
        assert matches(connection, "DisabledOnly") == []

    def test_rename_updates_only_directly_linked_knowledge(self, categories, connection, monkeypatch):
        """验证 `rename_updates_only_directly_linked_knowledge` 所代表的行为。 参数包括：`categories`、`connection`、`monkeypatch`。"""
        parent, child = categories.create_tree((CategorySeed("OldParent", (CategorySeed("Child"),)),))
        direct = knowledge(connection)
        indirect = knowledge(connection)
        categories.attach_knowledge(parent.id, direct)
        categories.attach_knowledge(child.id, indirect)
        untouched = fts_row(connection, indirect)
        refreshed = []
        real_refresh = category_module.refresh_owner

        def spy(database, owner_type, owner_id):
            """执行 `spy` 对应的功能并返回处理结果。 参数包括：`database`、`owner_type`、`owner_id`。"""
            refreshed.append((owner_type, owner_id))
            return real_refresh(database, owner_type, owner_id)

        monkeypatch.setattr(category_module, "refresh_owner", spy)
        categories.rename(parent.id, "NewParent", reason="改名")
        assert matches(connection, "OldParent") == []
        assert matches(connection, "NewParent") == [direct]
        assert fts_row(connection, indirect) == untouched
        assert refreshed == [(1, parent.id)]

    def test_move_does_not_refresh_fts(self, categories, connection, monkeypatch):
        """验证 `move_does_not_refresh_fts` 所代表的行为。 参数包括：`categories`、`connection`、`monkeypatch`。"""
        root, other, child = categories.create_tree((CategorySeed("根"), CategorySeed("其他"), CategorySeed("子")))
        knowledge_id = knowledge(connection)
        categories.attach_knowledge(child.id, knowledge_id)
        before = fts_row(connection, knowledge_id)

        def unexpected_refresh(*args):
            """执行 `unexpected_refresh` 对应的功能并返回处理结果。 参数包括：`*args`。"""
            raise AssertionError("仅改变父节点不应重建直接分类文本")

        monkeypatch.setattr(category_module, "refresh_owner", unexpected_refresh)
        categories.move(child.id, root.id, reason="移动")
        categories.move(child.id, other.id, reason="再次移动")
        assert fts_row(connection, knowledge_id) == before

    def test_single_node_disable_keeps_descendants_and_knowledge(self, categories, aliases, connection):
        """验证 `single_node_disable_keeps_descendants_and_knowledge` 所代表的行为。 参数包括：`categories`、`aliases`、`connection`。"""
        parent, child = categories.create_tree((CategorySeed("ParentToken", (CategorySeed("ChildToken"),)),))
        aliases.add(1, parent.id, "ParentAlias", confirmed=True)
        direct = knowledge(connection)
        descendant = knowledge(connection)
        categories.attach_knowledge(parent.id, direct)
        categories.attach_knowledge(child.id, descendant)
        before_knowledge = connection.execute("SELECT * FROM knowledge ORDER BY id").fetchall()
        before_links = connection.execute("SELECT * FROM knowledge_category ORDER BY id").fetchall()
        categories.set_status(parent.id, 1, reason="仅停用父节点")
        assert categories.get(child.id).status == 0
        assert categories.list_all()[0].status == 1
        assert fts_row(connection, direct)[4] == ""
        assert matches(connection, "ParentAlias") == []
        assert matches(connection, "ChildToken") == [descendant]
        assert matches(connection, "原子性") == [direct, descendant]
        assert connection.execute("SELECT * FROM knowledge ORDER BY id").fetchall() == before_knowledge
        assert connection.execute("SELECT * FROM knowledge_category ORDER BY id").fetchall() == before_links
        aliases.add(1, parent.id, "WhileDisabled", confirmed=True)
        assert matches(connection, "WhileDisabled") == []
        categories.set_status(parent.id, 0, reason="重新启用")
        assert matches(connection, "ParentAlias") == [direct]
        assert matches(connection, "WhileDisabled") == [direct]

    def test_attach_detach_idempotent_and_validated(self, categories, connection):
        """验证 `attach_detach_idempotent_and_validated` 所代表的行为。 参数包括：`categories`、`connection`。"""
        category = categories.create("AttachToken")
        knowledge_id = knowledge(connection)
        assert categories.attach_knowledge(category.id, knowledge_id) is True
        assert categories.attach_knowledge(category.id, knowledge_id) is False
        assert matches(connection, "AttachToken") == [knowledge_id]
        assert categories.detach_knowledge(category.id, knowledge_id) is True
        assert categories.detach_knowledge(category.id, knowledge_id) is False
        assert matches(connection, "AttachToken") == []
        with pytest.raises(KnowledgeManagementError):
            categories.attach_knowledge(category.id, 999)
        with pytest.raises(KnowledgeManagementError):
            categories.attach_knowledge(999, knowledge_id)


class TestCategoryDeletion:
    def test_even_disabled_children_block_deletion(self, categories):
        """验证 `even_disabled_children_block_deletion` 所代表的行为。 参数包括：`categories`。"""
        parent, child = categories.create_tree((CategorySeed("父", (CategorySeed("子"),)),))
        categories.set_status(child.id, 1, reason="停用子")
        with pytest.raises(KnowledgeManagementError, match="子节点"):
            categories.delete(parent.id, reason="删除")
        assert categories.get(parent.id) == parent

    def test_even_inactive_knowledge_association_blocks_deletion(self, categories, connection):
        """验证 `even_inactive_knowledge_association_blocks_deletion` 所代表的行为。 参数包括：`categories`、`connection`。"""
        category = categories.create("分类")
        knowledge_id = knowledge(connection, status=1)
        categories.attach_knowledge(category.id, knowledge_id)
        with pytest.raises(KnowledgeManagementError, match="知识关联"):
            categories.delete(category.id, reason="删除")
        assert categories.get(category.id) == category

    def test_delete_leaf_cleans_own_aliases_and_keeps_log(self, categories, aliases, connection):
        """验证 `delete_leaf_cleans_own_aliases_and_keeps_log` 所代表的行为。 参数包括：`categories`、`aliases`、`connection`。"""
        category = categories.create("可删除")
        other = categories.create("保留分类")
        removed_alias = aliases.add(1, category.id, "删除别名", confirmed=True)
        retained_alias = aliases.add(1, other.id, "保留别名", confirmed=True)
        categories.delete(category.id, reason="误建", operator="system")
        with pytest.raises(KnowledgeManagementError):
            categories.get(category.id)
        with pytest.raises(KnowledgeManagementError):
            aliases.get(removed_alias.id)
        assert aliases.get(retained_alias.id) == retained_alias
        old, new, reason, operator = connection.execute(
            "SELECT old_value, new_value, change_reason, operator_id FROM change_log"
        ).fetchone()
        assert json.loads(old)["aliases"] == [{"id": removed_alias.id, "name": "删除别名", "origin": 1}]
        assert (new, reason, operator) == (None, "误建", "system")


class TestConfirmedAliases:
    def test_unconfirmed_add_and_rename_do_not_enter_formal_table(self, categories, aliases, connection):
        """验证 `unconfirmed_add_and_rename_do_not_enter_formal_table` 所代表的行为。 参数包括：`categories`、`aliases`、`connection`。"""
        owner = categories.create("分类")
        with pytest.raises(KnowledgeManagementError, match="suggestion_payload"):
            aliases.add(1, owner.id, "待审", confirmed=False, origin=0)
        existing = aliases.add(1, owner.id, "已确认", confirmed=True)
        with pytest.raises(KnowledgeManagementError):
            aliases.rename(existing.id, "待审改名", confirmed=False)
        assert aliases.list_for(1, owner.id) == [existing]

    def test_equivalent_add_reuses_record_across_origin(self, aliases, connection):
        """验证 `equivalent_add_reuses_record_across_origin` 所代表的行为。 参数包括：`aliases`、`connection`。"""
        knowledge_id = knowledge(connection)
        original = aliases.add(0, knowledge_id, "AI", confirmed=True, origin=0)
        for equivalent in ("ai", "A I", " AI", "AI "):
            assert aliases.add(0, knowledge_id, equivalent, confirmed=True, origin=1) == original
        assert aliases.get(original.id).origin == 0
        assert len(aliases.list_for(0, knowledge_id)) == 1
        assert fts_row(connection, knowledge_id)[3] == "AI"

    @pytest.mark.parametrize("name", ["A  I", " A I", "A I ", "A\tI", "A\nI", "A\u3000I", "ＡＩ"])
    def test_non_equivalent_names_remain_distinct(self, categories, aliases, name):
        """验证 `non_equivalent_names_remain_distinct` 所代表的行为。 参数包括：`categories`、`aliases`、`name`。"""
        owner = categories.create("分类")
        first = aliases.add(1, owner.id, "AI", confirmed=True)
        other = aliases.add(1, owner.id, name, confirmed=True)
        assert first.id != other.id
        assert other.name == name

    def test_same_name_on_other_objects_is_allowed(self, categories, aliases, connection):
        """验证 `same_name_on_other_objects_is_allowed` 所代表的行为。 参数包括：`categories`、`aliases`、`connection`。"""
        first, second = categories.create_tree((CategorySeed("一"), CategorySeed("二")))
        knowledge_id = knowledge(connection)
        records = [aliases.add(owner_type, owner_id, "AI", confirmed=True) for owner_type, owner_id in (
            (1, first.id), (1, second.id), (0, knowledge_id),
        )]
        assert len({row.id for row in records}) == 3

    def test_rename_conflict_keeps_both_aliases_and_fts(self, aliases, connection):
        """验证 `rename_conflict_keeps_both_aliases_and_fts` 所代表的行为。 参数包括：`aliases`、`connection`。"""
        knowledge_id = knowledge(connection)
        first = aliases.add(0, knowledge_id, "AI", confirmed=True)
        second = aliases.add(0, knowledge_id, "ML", confirmed=True)
        before = fts_row(connection, knowledge_id)
        with pytest.raises(KnowledgeManagementError, match="等价"):
            aliases.rename(second.id, "a i", confirmed=True)
        assert aliases.list_for(0, knowledge_id) == [first, second]
        assert fts_row(connection, knowledge_id) == before

    def test_category_alias_add_rename_delete_updates_direct_fts(self, categories, aliases, connection):
        """验证 `category_alias_add_rename_delete_updates_direct_fts` 所代表的行为。 参数包括：`categories`、`aliases`、`connection`。"""
        category = categories.create("分类")
        knowledge_id = knowledge(connection)
        categories.attach_knowledge(category.id, knowledge_id)
        alias = aliases.add(1, category.id, "OldAlias", confirmed=True)
        assert matches(connection, "OldAlias") == [knowledge_id]
        renamed = aliases.rename(alias.id, "NewAlias", confirmed=True)
        assert renamed.id == alias.id
        assert matches(connection, "OldAlias") == []
        assert matches(connection, "NewAlias") == [knowledge_id]
        aliases.delete(alias.id)
        assert matches(connection, "NewAlias") == []

    def test_knowledge_alias_changes_preserve_versions_sources_and_index_markers(self, aliases, connection):
        """验证 `knowledge_alias_changes_preserve_versions_sources_and_index_markers` 所代表的行为。 参数包括：`aliases`、`connection`。"""
        knowledge_id = knowledge(connection)
        with connection:
            doc = connection.execute("INSERT INTO source_document (title, storage_url, content_hash) VALUES ('文档', 'file', 'hash')").lastrowid
            chunk = connection.execute("INSERT INTO document_chunk (source_document_id, chunk_index, content, chunk_hash) VALUES (?, 0, '原文证据', 'h')", (doc,)).lastrowid
            connection.execute("INSERT INTO knowledge_source (knowledge_id, chunk_id) VALUES (?, ?)", (knowledge_id, chunk))
            qa = connection.execute("INSERT INTO qa_record (question, answer) VALUES ('问题', '回答')").lastrowid
            version_id = connection.execute("SELECT current_version_id FROM knowledge WHERE id = ?", (knowledge_id,)).fetchone()[0]
            connection.execute("INSERT INTO qa_citation (qa_record_id, knowledge_id, version_id, chunk_id) VALUES (?, ?, ?, ?)", (qa, knowledge_id, version_id, chunk))
        tables = ("knowledge", "knowledge_version", "knowledge_source", "document_chunk", "qa_record", "qa_citation", "task_process")
        before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        alias = aliases.add(0, knowledge_id, "OldAlias", confirmed=True)
        assert matches(connection, "OldAlias") == [knowledge_id]
        aliases.rename(alias.id, "NewAlias", confirmed=True)
        assert matches(connection, "OldAlias") == []
        assert matches(connection, "NewAlias") == [knowledge_id]
        aliases.delete(alias.id)
        assert matches(connection, "NewAlias") == []
        assert {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before

    @pytest.mark.parametrize("owner_type,owner_id,origin", [(2, 1, 0), (False, 1, 0), (0, 999, 0), (1, 999, 0), (0, -1, 0), (0, 1, 2)])
    def test_invalid_polymorphic_owner_or_origin_rejected(self, aliases, connection, owner_type, owner_id, origin):
        """验证 `invalid_polymorphic_owner_or_origin_rejected` 所代表的行为。 参数包括：`aliases`、`connection`、`owner_type`、`owner_id`、`origin`。"""
        with pytest.raises(KnowledgeManagementError):
            aliases.add(owner_type, owner_id, "无效", confirmed=True, origin=origin)
        assert connection.execute("SELECT count(*) FROM alias").fetchone()[0] == 0

    def test_alias_on_inactive_knowledge_does_not_resurrect_fts(self, aliases, connection):
        """验证 `alias_on_inactive_knowledge_does_not_resurrect_fts` 所代表的行为。 参数包括：`aliases`、`connection`。"""
        knowledge_id = knowledge(connection, status=1)
        aliases.add(0, knowledge_id, "InactiveAlias", confirmed=True)
        assert fts_row(connection, knowledge_id) is None

    def test_current_version_from_other_knowledge_rejected(self, aliases, connection):
        """验证 `current_version_from_other_knowledge_rejected` 所代表的行为。 参数包括：`aliases`、`connection`。"""
        first = knowledge(connection)
        second = knowledge(connection)
        with connection:
            other_version = connection.execute("SELECT current_version_id FROM knowledge WHERE id = ?", (second,)).fetchone()[0]
            connection.execute("UPDATE knowledge SET current_version_id = ? WHERE id = ?", (other_version, first))
        before = fts_row(connection, first)
        with pytest.raises(KnowledgeManagementError, match="不属于"):
            aliases.add(0, first, "错误版本别名", confirmed=True)
        assert aliases.list_for(0, first) == []
        assert fts_row(connection, first) == before


class TestAtomicManagement:
    def test_partial_multi_knowledge_refresh_failure_rolls_back_all(self, categories, connection, monkeypatch):
        """验证 `partial_multi_knowledge_refresh_failure_rolls_back_all` 所代表的行为。 参数包括：`categories`、`connection`、`monkeypatch`。"""
        category = categories.create("OriginalCategory")
        first, second = knowledge(connection), knowledge(connection)
        for knowledge_id in (first, second):
            categories.attach_knowledge(category.id, knowledge_id)
        before = {key: fts_row(connection, key) for key in (first, second)}
        real_refresh = fts_module.refresh_knowledge

        def fail_second(database, knowledge_id):
            """执行 `fail_second` 对应的功能并返回处理结果。 参数包括：`database`、`knowledge_id`。"""
            if knowledge_id == second:
                raise RuntimeError("第二条知识刷新失败")
            real_refresh(database, knowledge_id)

        monkeypatch.setattr(fts_module, "refresh_knowledge", fail_second)
        with pytest.raises(RuntimeError):
            categories.rename(category.id, "FailedCategory", reason="模拟级联中断")
        assert categories.get(category.id) == category
        assert {key: fts_row(connection, key) for key in (first, second)} == before
        assert connection.execute("SELECT count(*) FROM change_log").fetchone()[0] == 0

    def test_outer_rollback_includes_categories_aliases_fts_and_logs(self, categories, aliases, connection, database_path):
        """验证 `outer_rollback_includes_categories_aliases_fts_and_logs` 所代表的行为。 参数包括：`categories`、`aliases`、`connection`、`database_path`。"""
        knowledge_id = knowledge(connection)
        before_fts = fts_row(connection, knowledge_id)
        connection.execute("BEGIN IMMEDIATE")
        root = categories.create("ExternalCategory")
        categories.attach_knowledge(root.id, knowledge_id)
        aliases.add(0, knowledge_id, "ExternalAlias", confirmed=True)
        categories.rename(root.id, "RenamedCategory", reason="外层未提交")
        assert connection.in_transaction
        with closing(connect_database(database_path)) as observer:
            assert observer.execute("SELECT count(*) FROM category").fetchone()[0] == 0
            assert observer.execute("SELECT count(*) FROM alias").fetchone()[0] == 0
        connection.rollback()
        for table in ("category", "alias", "knowledge_category", "change_log"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert fts_row(connection, knowledge_id) == before_fts

    def test_nested_failure_rolls_back_savepoint_preserving_outer_work(self, categories, aliases, connection, monkeypatch):
        """验证 `nested_failure_rolls_back_savepoint_preserving_outer_work` 所代表的行为。 参数包括：`categories`、`aliases`、`connection`、`monkeypatch`。"""
        knowledge_id = knowledge(connection)
        connection.execute("BEGIN IMMEDIATE")
        root = categories.create("保留外层")
        real_refresh = alias_module.refresh_owner

        def fail_after_fts(*args):
            """执行 `fail_after_fts` 对应的功能并返回处理结果。 参数包括：`*args`。"""
            real_refresh(*args)
            raise RuntimeError("模拟 FTS 后失败")

        monkeypatch.setattr(alias_module, "refresh_owner", fail_after_fts)
        with pytest.raises(RuntimeError):
            aliases.add(0, knowledge_id, "RolledBackAlias", confirmed=True)
        assert connection.in_transaction
        assert categories.get(root.id) == root
        assert aliases.list_for(0, knowledge_id) == []
        assert matches(connection, "RolledBackAlias") == []
        connection.commit()

    def test_fts_failure_rolls_back_category_change(self, categories, connection, monkeypatch):
        """验证 `fts_failure_rolls_back_category_change` 所代表的行为。 参数包括：`categories`、`connection`、`monkeypatch`。"""
        category = categories.create("OriginalCategory")
        knowledge_id = knowledge(connection)
        categories.attach_knowledge(category.id, knowledge_id)
        before = fts_row(connection, knowledge_id)
        real_refresh = category_module.refresh_owner

        def fail_after_fts(*args):
            """执行 `fail_after_fts` 对应的功能并返回处理结果。 参数包括：`*args`。"""
            real_refresh(*args)
            raise RuntimeError("模拟刷新中途失败")

        monkeypatch.setattr(category_module, "refresh_owner", fail_after_fts)
        with pytest.raises(RuntimeError):
            categories.rename(category.id, "FailedCategory", reason="失败")
        assert categories.get(category.id) == category
        assert fts_row(connection, knowledge_id) == before
        assert connection.execute("SELECT count(*) FROM change_log").fetchone()[0] == 0

    def test_deletion_log_failure_rolls_back_category_and_alias(self, categories, aliases, connection, monkeypatch):
        """验证 `deletion_log_failure_rolls_back_category_and_alias` 所代表的行为。 参数包括：`categories`、`aliases`、`connection`、`monkeypatch`。"""
        category = categories.create("保留")
        alias = aliases.add(1, category.id, "保留别名", confirmed=True)

        def fail_log(*args):
            """执行 `fail_log` 对应的功能并返回处理结果。 参数包括：`*args`。"""
            raise RuntimeError("日志失败")

        monkeypatch.setattr(category_module, "log_category_change", fail_log)
        with pytest.raises(RuntimeError):
            categories.delete(category.id, reason="失败")
        assert categories.get(category.id) == category
        assert aliases.get(alias.id) == alias
