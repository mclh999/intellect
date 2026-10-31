"""T3.1：所有权隔离、状态/错误原子提交及真实解析/抽取服务集成。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
import json
import sqlite3

import pytest
from langchain_core.runnables import RunnableLambda

from intellect.core.config import ChunkConfig
from intellect.core.errors import TaskErrorRecorder, TaskExecutionError, build_error_info
from intellect.core.tasks import DatabaseTaskErrorRecorder, TaskLease, TaskOwnershipError, TaskStateService
from intellect.core.worker import ImportTaskHandler, TaskContext, TaskWorker
from intellect.db import connect_database, initialize_database
from intellect.doc.batches import ChunkInput, DocumentBatchService
from intellect.doc.importing import DocumentImportService
from intellect.doc.processing import DocumentProcessingService
from intellect.extract.model import LangChainExtractor
from intellect.extract.service import ExtractionService


@pytest.fixture
def database_path(tmp_path):
    """执行 `database_path` 对应的功能并返回处理结果。 参数包括：`tmp_path`。"""
    path = tmp_path / 'worker.db'
    initialize_database(path)
    return path


@pytest.fixture
def connection(database_path):
    """执行 `connection` 对应的功能并返回处理结果。 参数包括：`database_path`。"""
    with closing(connect_database(database_path)) as connection:
        yield connection


def register(connection, tmp_path, data=b'first full source\nsecond full source', *, chunk_size=20):
    """执行 `register` 对应的功能并返回处理结果。 参数包括：`connection`、`tmp_path`、`data`、`chunk_size`。"""
    path = tmp_path / 'source.txt'
    path.write_bytes(data)
    return DocumentImportService(connection).register(path, title='资料', kind='TXT',
        chunking=ChunkConfig(chunk_size=chunk_size, chunk_overlap=0))


class Model:
    def __init__(self, items=(), callback=None):
        """实现 `__init__` 生命周期钩子，初始化或维护对象状态。 参数包括：`items`、`callback`。"""
        self.items, self.callback, self.calls = items, callback, 0

    def invoke(self, prompt):
        """执行 `invoke` 对应的功能并返回处理结果。 参数包括：`prompt`。"""
        self.calls += 1
        if self.callback:
            self.callback()
        return json.dumps({'items':list(self.items)}, ensure_ascii=False)

    def adapter(self):
        """执行 `adapter` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
        return LangChainExtractor(RunnableLambda(self.invoke), provider='test', model_name='fixture', model_version='1')


ITEM = {'title':'条件与例外','content':'10 mg；不适用于例外。'}


def fail(connection, lease):
    """执行 `fail` 对应的功能并返回处理结果。 参数包括：`connection`、`lease`。"""
    recorder = DatabaseTaskErrorRecorder(connection, lease)
    assert isinstance(recorder, TaskErrorRecorder)
    recorder.record_error(lease.task_id, build_error_info(RuntimeError('模拟故障'), task_id=lease.task_id))


def replace_owner(connection, lease):
    """执行 `replace_owner` 对应的功能并返回处理结果。 参数包括：`connection`、`lease`。"""
    fail(connection, lease)
    states = TaskStateService(connection)
    states.retry(lease.task_id)
    return states.claim(lease.task_type, task_id=lease.task_id)


class TestTaskState:
    def test_atomic_claim_has_only_one_winner(self, connection, database_path, tmp_path):
        """验证 `atomic_claim_has_only_one_winner` 所代表的行为。 参数包括：`connection`、`database_path`、`tmp_path`。"""
        registered = register(connection, tmp_path)
        barrier = Barrier(2)
        def claim():
            """执行 `claim` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
            with closing(connect_database(database_path)) as db:
                barrier.wait(timeout=10)
                return TaskStateService(db).claim(0, task_id=registered.batch.task_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            leases = list(pool.map(lambda _: claim(), range(2)))
        assert sum(lease is not None for lease in leases) == 1
        assert TaskStateService(connection).get(registered.batch.task_id).status == 1
        assert connection.execute('SELECT start_time,finish_time FROM task_process').fetchone()[0]

    def test_failure_is_atomic_monotonic_and_explicit_retry_retains_error_and_snapshots(self, connection, tmp_path):
        """验证 `failure_is_atomic_monotonic_and_explicit_retry_retains_error_and_snapshots` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        task = register(connection, tmp_path).batch
        states = TaskStateService(connection)
        lease = states.claim(0)
        before = states.get(task.task_id)
        fail(connection, lease)
        failed = states.get(task.task_id)
        assert failed.status == 4 and failed.retry_count == 1
        assert json.loads(failed.error_info)['task_id'] == task.task_id
        assert connection.execute('SELECT finish_time FROM task_process').fetchone()[0]
        assert states.claim(0) is None
        retried = states.retry(task.task_id)
        assert retried.status == 0 and retried.retry_count == 1 and retried.error_info == failed.error_info
        assert (retried.chunk_batch,retried.parse_config)==(before.chunk_batch,before.parse_config)
        newer = states.claim(0)
        assert newer.retry_count == 1
        fail(connection,newer)
        assert states.get(task.task_id).retry_count == 2

    @pytest.mark.parametrize('operation',['heartbeat','error','finish'])
    def test_old_ownership_cannot_touch_reclaimed_execution(self, connection, tmp_path, operation):
        """验证 `old_ownership_cannot_touch_reclaimed_execution` 所代表的行为。 参数包括：`connection`、`tmp_path`、`operation`。"""
        task = register(connection,tmp_path).batch
        states=TaskStateService(connection)
        old=states.claim(0)
        new=replace_owner(connection,old)
        before=connection.execute('SELECT status,retry_count,error_info,update_time FROM task_process').fetchone()
        with pytest.raises(TaskOwnershipError):
            if operation=='heartbeat': states.heartbeat(old)
            elif operation=='error': fail(connection,old)
            else: states.finish(old)
        assert connection.execute('SELECT status,retry_count,error_info,update_time FROM task_process').fetchone()==before
        states.heartbeat(new)

    @pytest.mark.parametrize('status',[0,1,2,3])
    def test_only_failed_tasks_can_be_retried(self,connection,tmp_path,status):
        """验证 `only_failed_tasks_can_be_retried` 所代表的行为。 参数包括：`connection`、`tmp_path`、`status`。"""
        task=register(connection,tmp_path).batch
        with connection: connection.execute('UPDATE task_process SET status=? WHERE id=?',(status,task.task_id))
        with pytest.raises(TaskExecutionError,match='已失败'): TaskStateService(connection).retry(task.task_id)

    @pytest.mark.parametrize('error_info',['invalid','{}','[]'])
    def test_malformed_error_does_not_change_task(self,connection,tmp_path,error_info):
        """验证 `malformed_error_does_not_change_task` 所代表的行为。 参数包括：`connection`、`tmp_path`、`error_info`。"""
        register(connection,tmp_path)
        lease=TaskStateService(connection).claim(0)
        with pytest.raises(TaskExecutionError): DatabaseTaskErrorRecorder(connection,lease).record_error(lease.task_id,error_info)
        assert connection.execute('SELECT status,retry_count,error_info FROM task_process').fetchone()==(1,0,None)

    def test_error_sql_failure_rolls_back_status_counter_and_error(self,connection,tmp_path):
        """验证 `error_sql_failure_rolls_back_status_counter_and_error` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        register(connection,tmp_path)
        lease=TaskStateService(connection).claim(0)
        connection.execute("CREATE TRIGGER fault BEFORE UPDATE ON task_process WHEN NEW.status=4 BEGIN SELECT RAISE(ABORT,'fault'); END")
        with pytest.raises(sqlite3.IntegrityError): fail(connection,lease)
        assert connection.execute('SELECT status,retry_count,error_info FROM task_process').fetchone()==(1,0,None)

    def test_state_and_error_updates_respect_outer_rollback(self,connection,tmp_path):
        """验证 `state_and_error_updates_respect_outer_rollback` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        register(connection,tmp_path)
        states=TaskStateService(connection)
        connection.execute('BEGIN IMMEDIATE')
        lease=states.claim(0)
        states.heartbeat(lease)
        fail(connection,lease)
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute('SELECT status,retry_count,error_info FROM task_process').fetchone()==(0,0,None)

    def test_heartbeat_and_business_transaction_rollback(self,connection,tmp_path):
        """验证 `heartbeat_and_business_transaction_rollback` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        register(connection,tmp_path)
        lease=TaskStateService(connection).claim(0)
        context=TaskContext(connection,lease)
        with connection: connection.execute("UPDATE task_process SET update_time='2000-01-01 00:00:00'")
        context.heartbeat()
        assert connection.execute('SELECT update_time FROM task_process').fetchone()[0]!='2000-01-01 00:00:00'
        with pytest.raises(RuntimeError), context.transaction():
            connection.execute("INSERT INTO category (name) VALUES ('回滚')")
            raise RuntimeError('fault')
        assert connection.execute('SELECT count(*) FROM category').fetchone()==(0,)


class TestGuardedDocumentAndExtraction:
    @pytest.mark.parametrize('operation',['chunks','publish','prepare','result'])
    def test_stale_writer_is_rejected_inside_actual_commit(self,connection,tmp_path,operation):
        """验证 `stale_writer_is_rejected_inside_actual_commit` 所代表的行为。 参数包括：`connection`、`tmp_path`、`operation`。"""
        task=register(connection,tmp_path).batch
        states=TaskStateService(connection)
        old=states.claim(0)
        batches=DocumentBatchService(connection,lease=old)
        extractor=Model([ITEM]).adapter()
        source=None
        if operation in ('prepare','result'):
            DocumentProcessingService(connection,lease=old).run(task.task_id)
            if operation=='result': source=ExtractionService(connection,extractor,lease=old).prepare(task.task_id)[0]
        newer=replace_owner(connection,old)
        before=tuple(connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                     for table in ('document_chunk','candidate','extraction_result'))
        with pytest.raises(TaskOwnershipError):
            if operation=='chunks': batches.save_chunks(task.task_id,[ChunkInput(0,'late')])
            elif operation=='publish': batches.publish(task.task_id,expected_count=0)
            elif operation=='prepare': ExtractionService(connection,extractor,lease=old).prepare(task.task_id)
            else: ExtractionService(connection,extractor,lease=old).save_result(source,{'items':[ITEM]})
        assert tuple(connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                     for table in ('document_chunk','candidate','extraction_result'))==before
        states.heartbeat(newer)

    def test_slow_model_return_does_not_overwrite_new_execution(self,connection,tmp_path):
        """验证 `slow_model_return_does_not_overwrite_new_execution` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        task=register(connection,tmp_path).batch
        new=[]
        def supersede():
            """执行 `supersede` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
            assert not connection.in_transaction
            new.append(replace_owner(connection,TaskLease(task.task_id,0,0)))
        worker=TaskWorker(connection,{0:ImportTaskHandler(Model([ITEM],supersede).adapter())})
        with pytest.raises(TaskOwnershipError): worker.run_once()
        assert connection.execute('SELECT status,retry_count FROM task_process').fetchone()==(1,1)
        assert connection.execute('SELECT count(*) FROM candidate').fetchone()==(0,)
        assert connection.execute('SELECT count(*) FROM extraction_result').fetchone()==(0,)
        TaskStateService(connection).heartbeat(new[0])

    def test_bound_worker_cannot_create_new_batch(self,connection,tmp_path):
        """验证 `bound_worker_cannot_create_new_batch` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        task=register(connection,tmp_path).batch
        lease=TaskStateService(connection).claim(0)
        with pytest.raises(TaskExecutionError):
            # 任务外批次不可由 worker 替代失败任务；批次服务保留自己的稳定错误类型。
            TaskContext(connection,TaskLease(task.task_id+1,0,0)).heartbeat()
        from intellect.doc.batches import DocumentBatchError
        with pytest.raises(DocumentBatchError):
            DocumentBatchService(connection,lease=lease).create(task.source_document_id,parser='TXT',parser_version='1')


class TestImportWorker:
    @pytest.mark.parametrize('raw',[b'',b'plain source'])
    def test_successful_empty_extraction_completes_without_review(self,connection,tmp_path,raw):
        """验证 `successful_empty_extraction_completes_without_review` 所代表的行为。 参数包括：`connection`、`tmp_path`、`raw`。"""
        task=register(connection,tmp_path,raw).batch
        model=Model()
        result=TaskWorker(connection,{0:ImportTaskHandler(model.adapter())}).run_once()
        assert result.id==task.task_id and result.status==3 and result.retry_count==0
        assert connection.execute('SELECT count(*) FROM candidate').fetchone()==(0,)
        assert connection.execute('SELECT count(*) FROM review_issue').fetchone()==(0,)
        assert connection.execute('SELECT count(*) FROM knowledge').fetchone()==(0,)
        assert bool(model.calls)==bool(raw)

    def test_unmatched_candidates_fail_then_retry_reuses_ids_without_model_call(self,connection,tmp_path):
        """验证 `unmatched_candidates_fail_then_retry_reuses_ids_without_model_call` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        task=register(connection,tmp_path,b'plain source').batch
        model=Model([ITEM])
        first=TaskWorker(connection,{0:ImportTaskHandler(model.adapter())}).run_once()
        assert first.status==4 and first.retry_count==1
        ids=connection.execute('SELECT id FROM candidate').fetchall()
        def review(context):
            """执行 `review` 对应的功能并返回处理结果。 参数包括：`context`。"""
            with context.transaction():
                for row in context.connection.execute('SELECT id FROM candidate WHERE task_process_id=?',(context.lease.task_id,)).fetchall():
                    context.connection.execute('INSERT INTO review_issue (candidate_id,issue_type,suggestion_action_type) VALUES (?,3,5)',row)
        TaskStateService(connection).retry(task.task_id)
        second=TaskWorker(connection,{0:ImportTaskHandler(model.adapter(),match_and_review=review)}).run_once()
        assert second.status==2 and second.retry_count==1 and second.error_info==first.error_info
        assert connection.execute('SELECT id FROM candidate').fetchall()==ids and model.calls==1
        assert connection.execute('SELECT count(*) FROM knowledge').fetchone()==(0,)

    def test_partial_model_failure_preserves_published_chunks_and_successful_results(self,connection,tmp_path):
        """验证 `partial_model_failure_preserves_published_chunks_and_successful_results` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        task=register(connection,tmp_path).batch
        model=Model()
        def stop_second():
            """执行 `stop_second` 对应的功能并返回处理结果。 参数包括：无显式参数。"""
            if model.calls==2: raise RuntimeError('second chunk fault')
        model.callback=stop_second
        worker=TaskWorker(connection,{0:ImportTaskHandler(model.adapter())})
        assert worker.run_once().status==4
        ids=connection.execute('SELECT id FROM document_chunk ORDER BY id').fetchall()
        assert connection.execute('SELECT count(*) FROM extraction_result').fetchone()==(1,)
        model.callback=None
        TaskStateService(connection).retry(task.task_id)
        assert worker.run_once().status==3
        assert connection.execute('SELECT id FROM document_chunk ORDER BY id').fetchall()==ids
        assert model.calls==len(ids)+1

    @pytest.mark.parametrize('damage',['checkpoint','unmatched_second','processed_with_pending','model','sequence'])
    def test_finish_checks_actual_completeness_and_review_state(self,connection,tmp_path,damage):
        """验证 `finish_checks_actual_completeness_and_review_state` 所代表的行为。 参数包括：`connection`、`tmp_path`、`damage`。"""
        task=register(connection,tmp_path,b'plain source').batch
        states=TaskStateService(connection)
        lease=states.claim(0)
        DocumentProcessingService(connection,lease=lease).run(task.task_id)
        ExtractionService(connection,Model([ITEM,ITEM]).adapter(),lease=lease).run(task.task_id)
        ids=[row[0] for row in connection.execute('SELECT id FROM candidate ORDER BY id')]
        with connection:
            if damage=='checkpoint': connection.execute('DELETE FROM extraction_result')
            elif damage=='model': connection.execute("UPDATE candidate SET extract_model='changed'")
            elif damage=='sequence': connection.execute('UPDATE candidate SET extraction_index=NULL')
            else:
                connection.execute('INSERT INTO review_issue (candidate_id,issue_type) VALUES (?,3)',(ids[0],))
                if damage=='processed_with_pending': connection.execute('UPDATE candidate SET status=0')
        with pytest.raises(TaskExecutionError): states.finish(lease)
        assert states.get(task.task_id).status==1

    def test_import_cannot_complete_without_parsing_and_extraction(self,connection,tmp_path):
        """验证 `import_cannot_complete_without_parsing_and_extraction` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        register(connection,tmp_path)
        states=TaskStateService(connection)
        lease=states.claim(0)
        with pytest.raises(TaskExecutionError): states.finish(lease)
        assert states.get(lease.task_id).status==1

    def test_finish_rejects_configuration_missing_prompt_and_schema_fingerprints(self,connection,tmp_path):
        """验证 `finish_rejects_configuration_missing_prompt_and_schema_fingerprints` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        task=register(connection,tmp_path,b'plain source').batch
        states=TaskStateService(connection)
        lease=states.claim(0)
        DocumentProcessingService(connection,lease=lease).run(task.task_id)
        ExtractionService(connection,Model().adapter(),lease=lease).run(task.task_id)
        config=json.loads(states.get(task.task_id).extract_config)
        with connection:
            connection.execute('UPDATE task_process SET extract_config=?',(json.dumps({'model':config['model']}),))
        with pytest.raises(TaskExecutionError,match='抽取配置'):
            states.finish(lease)

    def test_processed_or_rejected_candidates_complete_after_all_issues_resolved(self,connection,tmp_path):
        """验证 `processed_or_rejected_candidates_complete_after_all_issues_resolved` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        register(connection,tmp_path,b'plain source')
        def handled(context):
            """执行 `handled` 对应的功能并返回处理结果。 参数包括：`context`。"""
            with context.transaction():
                context.connection.execute('UPDATE candidate SET status=2 WHERE task_process_id=?',(context.lease.task_id,))
        result=TaskWorker(connection,{0:ImportTaskHandler(Model([ITEM]).adapter(),match_and_review=handled)}).run_once()
        assert result.status==3


class TestWorkerPolling:
    def test_late_exception_cannot_fail_a_newer_execution(self,connection,tmp_path):
        """验证 `late_exception_cannot_fail_a_newer_execution` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        task=register(connection,tmp_path).batch
        def stale(context):
            """执行 `stale` 对应的功能并返回处理结果。 参数包括：`context`。"""
            replace_owner(connection,context.lease)
            raise RuntimeError('late error')
        with pytest.raises(TaskOwnershipError): TaskWorker(connection,{0:stale}).run_once()
        assert connection.execute('SELECT status,retry_count FROM task_process').fetchone()==(1,1)
        assert 'late error' not in TaskStateService(connection).get(task.task_id).error_info

    def test_unregistered_type_is_not_claimed_and_failures_are_not_retried_automatically(self,connection,tmp_path):
        """验证 `unregistered_type_is_not_claimed_and_failures_are_not_retried_automatically` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        task=register(connection,tmp_path).batch
        assert TaskWorker(connection,{}).run_once() is None
        assert TaskStateService(connection).get(task.task_id).status==0
        # 模拟任务处理器抛出异常，供失败状态与重试行为用例使用。
        def broken(context): raise RuntimeError('stop')
        worker=TaskWorker(connection,{0:broken})
        assert [record.status for record in worker.run_pending(limit=10)]==[4]
        assert worker.run_once() is None
        assert TaskStateService(connection).get(task.task_id).retry_count==1

    @pytest.mark.parametrize('limit',[0,-1,True,'1'])
    def test_poll_limit_must_be_explicit_positive_integer(self,connection,limit):
        """验证 `poll_limit_must_be_explicit_positive_integer` 所代表的行为。 参数包括：`connection`、`limit`。"""
        with pytest.raises(TaskExecutionError): TaskWorker(connection,{}).run_pending(limit=limit)

    def test_worker_rejects_outer_transaction_and_rolls_back_leaked_handler_transaction(self,connection,tmp_path):
        """验证 `worker_rejects_outer_transaction_and_rolls_back_leaked_handler_transaction` 所代表的行为。 参数包括：`connection`、`tmp_path`。"""
        task=register(connection,tmp_path).batch
        worker=TaskWorker(connection,{0:lambda context:None})
        connection.execute('BEGIN IMMEDIATE')
        with pytest.raises(TaskExecutionError,match='外层事务'): worker.run_once()
        assert connection.in_transaction
        connection.rollback()
        def leaked(context):
            """执行 `leaked` 对应的功能并返回处理结果。 参数包括：`context`。"""
            context.connection.execute('BEGIN IMMEDIATE')
            context.connection.execute("INSERT INTO category (name) VALUES ('未提交')")
        result=TaskWorker(connection,{0:leaked}).run_once()
        assert result.status==4 and connection.execute('SELECT count(*) FROM category').fetchone()==(0,)

    def test_sync_handler_is_injected_and_never_enters_waiting_review(self,connection):
        """验证 `sync_handler_is_injected_and_never_enters_waiting_review` 所代表的行为。 参数包括：`connection`。"""
        from test_tasks import sync_task
        task=sync_task(connection)
        assert TaskWorker(connection,{}).run_once(1) is None
        result=TaskWorker(connection,{1:lambda context:context.heartbeat()}).run_once(1)
        assert result.id==task and result.status==3
        assert connection.execute('SELECT source_document_id,chunk_batch FROM task_process').fetchone()==(None,None)
