import copy
import re
from contextlib import contextmanager
from unittest.mock import ANY, MagicMock, Mock, call, patch, sentinel

import pytest
from kombu.serialization import prepare_accept_content
from kombu.utils.encoding import bytes_to_str, ensure_bytes

import celery
from celery import chord, group, signature, states, uuid
from celery.app.task import Context, Task
from celery.backends.base import (BaseBackend, DisabledBackend, KeyValueStoreBackend, _create_chord_error_with_cause,
                                  _create_fake_task_request, _nulldict)
from celery.exceptions import BackendGetMetaError, BackendStoreError, ChordError, SecurityError, TimeoutError
from celery.result import GroupResult, result_from_tuple
from celery.utils import serialization
from celery.utils.functional import pass1
from celery.utils.serialization import UnpickleableExceptionWrapper
from celery.utils.serialization import find_pickleable_exception as fnpe
from celery.utils.serialization import get_pickleable_exception as gpe
from celery.utils.serialization import subclass_exception


class wrapobject:

    def __init__(self, *args, **kwargs):
        self.args = args


class paramexception(Exception):

    def __init__(self, param):
        self.param = param


class objectexception:
    class Nested(Exception):
        pass


Oldstyle = None

Unpickleable = subclass_exception(
    'Unpickleable', KeyError, 'foo.module',
)
Impossible = subclass_exception(
    'Impossible', object, 'foo.module',
)
Lookalike = subclass_exception(
    'Lookalike', wrapobject, 'foo.module',
)


class test_nulldict:

    def test_nulldict(self):
        x = _nulldict()
        x['foo'] = 1
        x.update(foo=1, bar=2)
        x.setdefault('foo', 3)


class test_serialization:

    def test_create_exception_cls(self):
        assert serialization.create_exception_cls('FooError', 'm')
        assert serialization.create_exception_cls('FooError', 'm', KeyError)


class test_Backend_interface:

    def setup_method(self):
        self.app.conf.accept_content = ['json']

    def test_accept_precedence(self):

        # default is app.conf.accept_content
        accept_content = self.app.conf.accept_content
        b1 = BaseBackend(self.app)
        assert prepare_accept_content(accept_content) == b1.accept

        # accept parameter
        b2 = BaseBackend(self.app, accept=['yaml'])
        assert len(b2.accept) == 1
        assert list(b2.accept)[0] == 'application/x-yaml'
        assert prepare_accept_content(['yaml']) == b2.accept

        # accept parameter over result_accept_content
        self.app.conf.result_accept_content = ['json']
        b3 = BaseBackend(self.app, accept=['yaml'])
        assert len(b3.accept) == 1
        assert list(b3.accept)[0] == 'application/x-yaml'
        assert prepare_accept_content(['yaml']) == b3.accept

        # conf.result_accept_content if specified
        self.app.conf.result_accept_content = ['yaml']
        b4 = BaseBackend(self.app)
        assert len(b4.accept) == 1
        assert list(b4.accept)[0] == 'application/x-yaml'
        assert prepare_accept_content(['yaml']) == b4.accept

    def test_get_result_meta(self):
        b1 = BaseBackend(self.app)
        meta = b1._get_result_meta(result={'fizz': 'buzz'},
                                   state=states.SUCCESS, traceback=None,
                                   request=None)
        assert meta['status'] == states.SUCCESS
        assert meta['result'] == {'fizz': 'buzz'}
        assert meta['traceback'] is None

        self.app.conf.result_extended = True
        args = ['a', 'b']
        kwargs = {'foo': 'bar'}
        task_name = 'mytask'

        b2 = BaseBackend(self.app)
        request = Context(args=args, kwargs=kwargs,
                          task=task_name,
                          delivery_info={'routing_key': 'celery'})
        meta = b2._get_result_meta(result={'fizz': 'buzz'},
                                   state=states.SUCCESS, traceback=None,
                                   request=request, encode=False)
        assert meta['name'] == task_name
        assert meta['args'] == args
        assert meta['kwargs'] == kwargs
        assert meta['queue'] == 'celery'

    def test_get_result_meta_stamps_attribute_error(self):
        class Request:
            pass
        self.app.conf.result_extended = True
        b1 = BaseBackend(self.app)
        meta = b1._get_result_meta(result={'fizz': 'buzz'},
                                   state=states.SUCCESS, traceback=None,
                                   request=Request())
        assert meta['status'] == states.SUCCESS
        assert meta['result'] == {'fizz': 'buzz'}
        assert meta['traceback'] is None

    def test_get_result_meta_encoded(self):
        self.app.conf.result_extended = True
        b1 = BaseBackend(self.app)
        args = ['a', 'b']
        kwargs = {'foo': 'bar'}

        request = Context(args=args, kwargs=kwargs)
        meta = b1._get_result_meta(result={'fizz': 'buzz'},
                                   state=states.SUCCESS, traceback=None,
                                   request=request, encode=True)
        assert meta['args'] == ensure_bytes(b1.encode(args))
        assert meta['kwargs'] == ensure_bytes(b1.encode(kwargs))

    def test_get_result_meta_with_none(self):
        b1 = BaseBackend(self.app)
        meta = b1._get_result_meta(result=None,
                                   state=states.SUCCESS, traceback=None,
                                   request=None)
        assert meta['status'] == states.SUCCESS
        assert meta['result'] is None
        assert meta['traceback'] is None

        self.app.conf.result_extended = True
        args = ['a', 'b']
        kwargs = {'foo': 'bar'}
        task_name = 'mytask'

        b2 = BaseBackend(self.app)
        request = Context(args=args, kwargs=kwargs,
                          task=task_name,
                          delivery_info={'routing_key': 'celery'})
        meta = b2._get_result_meta(result=None,
                                   state=states.SUCCESS, traceback=None,
                                   request=request, encode=False)
        assert meta['name'] == task_name
        assert meta['args'] == args
        assert meta['kwargs'] == kwargs
        assert meta['queue'] == 'celery'

    def test_get_result_meta_format_date(self):
        import datetime
        self.app.conf.result_extended = True
        b1 = BaseBackend(self.app)
        args = ['a', 'b']
        kwargs = {'foo': 'bar'}

        request = Context(args=args, kwargs=kwargs)
        meta = b1._get_result_meta(result={'fizz': 'buzz'},
                                   state=states.SUCCESS, traceback=None,
                                   request=request, format_date=True)
        assert isinstance(meta['date_done'], str)

        self.app.conf.result_extended = True
        b2 = BaseBackend(self.app)
        args = ['a', 'b']
        kwargs = {'foo': 'bar'}

        request = Context(args=args, kwargs=kwargs)
        meta = b2._get_result_meta(result={'fizz': 'buzz'},
                                   state=states.SUCCESS, traceback=None,
                                   request=request, format_date=False)
        assert isinstance(meta['date_done'], datetime.datetime)


class test_BaseBackend_interface:

    def setup_method(self):
        self.b = BaseBackend(self.app)

        @self.app.task(shared=False)
        def callback(result):
            pass

        self.callback = callback

    def test__forget(self):
        with pytest.raises(NotImplementedError):
            self.b._forget('SOMExx-N0Nex1stant-IDxx-')

    def test_forget(self):
        with pytest.raises(NotImplementedError):
            self.b.forget('SOMExx-N0nex1stant-IDxx-')

    def test_on_chord_part_return(self):
        self.b.on_chord_part_return(None, None, None)

    def test_apply_chord(self, unlock='celery.chord_unlock'):
        self.app.tasks[unlock] = Mock()
        header_result_args = (
            uuid(),
            [self.app.AsyncResult(x) for x in range(3)],
        )
        self.b.apply_chord(header_result_args, self.callback.s())
        assert self.app.tasks[unlock].apply_async.call_count

    def test_chord_unlock_queue(self, unlock='celery.chord_unlock'):
        self.app.tasks[unlock] = Mock()
        header_result_args = (
            uuid(),
            [self.app.AsyncResult(x) for x in range(3)],
        )
        body = self.callback.s()

        self.b.apply_chord(header_result_args, body)
        called_kwargs = self.app.tasks[unlock].apply_async.call_args[1]
        assert called_kwargs['queue'] == 'testcelery'

        routing_queue = Mock()
        routing_queue.name = "routing_queue"
        self.app.amqp.router.route = Mock(return_value={
            "queue": routing_queue
        })
        self.b.apply_chord(header_result_args, body)
        assert self.app.amqp.router.route.call_args[0][1] == body.name
        called_kwargs = self.app.tasks[unlock].apply_async.call_args[1]
        assert called_kwargs["queue"] == "routing_queue"

        self.b.apply_chord(header_result_args, body.set(queue='test_queue'))
        called_kwargs = self.app.tasks[unlock].apply_async.call_args[1]
        assert called_kwargs['queue'] == 'test_queue'

        @self.app.task(shared=False, queue='test_queue_two')
        def callback_queue(result):
            pass

        self.b.apply_chord(header_result_args, callback_queue.s())
        called_kwargs = self.app.tasks[unlock].apply_async.call_args[1]
        assert called_kwargs['queue'] == 'test_queue_two'

        with self.Celery() as app2:
            @app2.task(name='callback_different_app', shared=False)
            def callback_different_app(result):
                pass

            callback_different_app_signature = self.app.signature('callback_different_app')
            self.b.apply_chord(header_result_args, callback_different_app_signature)
            called_kwargs = self.app.tasks[unlock].apply_async.call_args[1]
            assert called_kwargs['queue'] == 'routing_queue'

            callback_different_app_signature.set(queue='test_queue_three')
            self.b.apply_chord(header_result_args, callback_different_app_signature)
            called_kwargs = self.app.tasks[unlock].apply_async.call_args[1]
            assert called_kwargs['queue'] == 'test_queue_three'


class test_exception_pickle:
    def test_BaseException(self):
        assert fnpe(Exception()) is None

    def test_get_pickleable_exception(self):
        exc = Exception('foo')
        assert gpe(exc) == exc

    def test_unpickleable(self):
        assert isinstance(fnpe(Unpickleable()), KeyError)
        assert fnpe(Impossible()) is None


class test_prepare_exception:

    def setup_method(self):
        self.b = BaseBackend(self.app)

    def test_unpickleable(self):
        self.b.serializer = 'pickle'
        x = self.b.prepare_exception(Unpickleable(1, 2, 'foo'))
        assert isinstance(x, KeyError)
        y = self.b.exception_to_python(x)
        assert isinstance(y, KeyError)

    def test_json_exception_arguments(self):
        self.b.serializer = 'json'
        x = self.b.prepare_exception(Exception(object))
        assert x == {
            'exc_message': serialization.ensure_serializable(
                (object,), self.b.encode),
            'exc_type': Exception.__name__,
            'exc_module': Exception.__module__}
        y = self.b.exception_to_python(x)
        assert isinstance(y, Exception)

    def test_json_exception_nested(self):
        self.b.serializer = 'json'
        x = self.b.prepare_exception(objectexception.Nested('msg'))
        assert x == {
            'exc_message': ('msg',),
            'exc_type': 'objectexception.Nested',
            'exc_module': objectexception.Nested.__module__}
        y = self.b.exception_to_python(x)
        assert isinstance(y, objectexception.Nested)

    def test_impossible(self):
        self.b.serializer = 'pickle'
        x = self.b.prepare_exception(Impossible())
        assert isinstance(x, UnpickleableExceptionWrapper)
        assert str(x)
        y = self.b.exception_to_python(x)
        assert y.__class__.__name__ == 'Impossible'
        assert y.__class__.__module__ == 'foo.module'

    def test_regular(self):
        self.b.serializer = 'pickle'
        x = self.b.prepare_exception(KeyError('baz'))
        assert isinstance(x, KeyError)
        y = self.b.exception_to_python(x)
        assert isinstance(y, KeyError)

    def test_unicode_message(self):
        message = '\u03ac'
        x = self.b.prepare_exception(Exception(message))
        assert x == {'exc_message': (message,),
                     'exc_type': Exception.__name__,
                     'exc_module': Exception.__module__}


class KVBackend(KeyValueStoreBackend):
    mget_returns_dict = False

    def __init__(self, app, *args, **kwargs):
        self.db = {}
        super().__init__(app, *args, **kwargs)

    def get(self, key):
        return self.db.get(key)

    def _set_with_state(self, key, value, state):
        self.db[key] = value

    def mget(self, keys):
        if self.mget_returns_dict:
            return {key: self.get(key) for key in keys}
        else:
            return [self.get(k) for k in keys]

    def delete(self, key):
        self.db.pop(key, None)


class DictBackend(BaseBackend):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._data = {'can-delete': {'result': 'foo'}}

    def _restore_group(self, group_id):
        if group_id == 'exists':
            return {'result': 'group'}

    def _get_task_meta_for(self, task_id):
        if task_id == 'task-exists':
            return {'result': 'task'}

    def _delete_group(self, group_id):
        self._data.pop(group_id, None)


class test_BaseBackend_dict:

    def setup_method(self):
        self.b = DictBackend(app=self.app)

        @self.app.task(shared=False, bind=True)
        def bound_errback(self, result):
            pass

        @self.app.task(shared=False)
        def errback(arg1, arg2):
            errback.last_result = arg1 + arg2

        self.bound_errback = bound_errback
        self.errback = errback

    def test_delete_group(self):
        self.b.delete_group('can-delete')
        assert 'can-delete' not in self.b._data

    def test_prepare_exception_json(self):
        x = DictBackend(self.app, serializer='json')
        e = x.prepare_exception(KeyError('foo'))
        assert 'exc_type' in e
        e = x.exception_to_python(e)
        assert e.__class__.__name__ == 'KeyError'
        assert str(e).strip('u') == "'foo'"

    def test_save_group(self):
        b = BaseBackend(self.app)
        b._save_group = Mock()
        b.save_group('foofoo', 'xxx')
        b._save_group.assert_called_with('foofoo', 'xxx')

    def test_add_to_chord_interface(self):
        b = BaseBackend(self.app)
        with pytest.raises(NotImplementedError):
            b.add_to_chord('group_id', 'sig')

    def test_forget_interface(self):
        b = BaseBackend(self.app)
        with pytest.raises(NotImplementedError):
            b.forget('foo')

    def test_restore_group(self):
        assert self.b.restore_group('missing') is None
        assert self.b.restore_group('missing') is None
        assert self.b.restore_group('exists') == 'group'
        assert self.b.restore_group('exists') == 'group'
        assert self.b.restore_group('exists', cache=False) == 'group'

    def test_reload_group_result(self):
        self.b._cache = {}
        self.b.reload_group_result('exists')
        self.b._cache['exists'] = {'result': 'group'}

    def test_reload_task_result(self):
        self.b._cache = {}
        self.b.reload_task_result('task-exists')
        self.b._cache['task-exists'] = {'result': 'task'}

    def test_fail_from_current_stack(self):
        import inspect
        self.b.mark_as_failure = Mock()
        frame_list = []

        def raise_dummy():
            frame_str_temp = str(inspect.currentframe().__repr__)
            frame_list.append(frame_str_temp)
            raise KeyError('foo')
        try:
            raise_dummy()
        except KeyError as exc:
            self.b.fail_from_current_stack('task_id')
            self.b.mark_as_failure.assert_called()
            args = self.b.mark_as_failure.call_args[0]
            assert args[0] == 'task_id'
            assert args[1] is exc
            assert args[2]

            tb_ = exc.__traceback__
            while tb_ is not None:
                if str(tb_.tb_frame.__repr__) == frame_list[0]:
                    assert len(tb_.tb_frame.f_locals) == 0
                tb_ = tb_.tb_next

    def test_prepare_value_serializes_group_result(self):
        self.b.serializer = 'json'
        g = self.app.GroupResult('group_id', [self.app.AsyncResult('foo')])
        v = self.b.prepare_value(g)
        assert isinstance(v, (list, tuple))
        assert result_from_tuple(v, app=self.app) == g

        v2 = self.b.prepare_value(g[0])
        assert isinstance(v2, (list, tuple))
        assert result_from_tuple(v2, app=self.app) == g[0]

        self.b.serializer = 'pickle'
        assert isinstance(self.b.prepare_value(g), self.app.GroupResult)

    def test_is_cached(self):
        b = BaseBackend(app=self.app, max_cached_results=1)
        b._cache['foo'] = 1
        assert b.is_cached('foo')
        assert not b.is_cached('false')

    def test_mark_as_done__chord(self):
        b = BaseBackend(app=self.app)
        b._store_result = Mock()
        request = Mock(name='request')
        b.on_chord_part_return = Mock()
        b.mark_as_done('id', 10, request=request)
        b.on_chord_part_return.assert_called_with(request, states.SUCCESS, 10)

    def test_mark_as_failure__bound_errback_eager(self):
        b = BaseBackend(app=self.app)
        b._store_result = Mock()
        request = Mock(name='request')
        request.delivery_info = {
            'is_eager': True
        }
        request.errbacks = [
            self.bound_errback.subtask(args=[1], immutable=True)]
        exc = KeyError()
        group = self.patching('celery.backends.base.group')
        b.mark_as_failure('id', exc, request=request)
        group.assert_called_with(request.errbacks, app=self.app)
        group.return_value.apply.assert_called_with(
            (request.id, ), parent_id=request.id, root_id=request.root_id)

    def test_mark_as_failure__bound_errback(self):
        b = BaseBackend(app=self.app)
        b._store_result = Mock()
        request = Mock(name='request')
        request.delivery_info = {}
        request.errbacks = [
            self.bound_errback.subtask(args=[1], immutable=True)]
        exc = KeyError()
        group = self.patching('celery.backends.base.group')
        b.mark_as_failure('id', exc, request=request)
        group.assert_called_with(request.errbacks, app=self.app)
        group.return_value.apply_async.assert_called_with(
            (request.id, ), parent_id=request.id, root_id=request.root_id)

    def test_mark_as_failure__errback(self):
        b = BaseBackend(app=self.app)
        b._store_result = Mock()
        request = Mock(name='request')
        request.errbacks = [self.errback.subtask(args=[2, 3], immutable=True)]
        exc = KeyError()
        b.mark_as_failure('id', exc, request=request)
        assert self.errback.last_result == 5

    @patch('celery.backends.base.group')
    def test_class_based_task_can_be_used_as_error_callback(self, mock_group):
        b = BaseBackend(app=self.app)
        b._store_result = Mock()

        class TaskBasedClass(Task):
            def run(self):
                pass

        TaskBasedClass = self.app.register_task(TaskBasedClass())

        request = Mock(name='request')
        request.errbacks = [TaskBasedClass.subtask(args=[], immutable=True)]
        exc = KeyError()
        b.mark_as_failure('id', exc, request=request)
        mock_group.assert_called_once_with(request.errbacks, app=self.app)

    @patch('celery.backends.base.group')
    def test_unregistered_task_can_be_used_as_error_callback(self, mock_group):
        b = BaseBackend(app=self.app)
        b._store_result = Mock()

        request = Mock(name='request')
        request.errbacks = [signature('doesnotexist',
                                      immutable=True)]
        exc = KeyError()
        b.mark_as_failure('id', exc, request=request)
        mock_group.assert_called_once_with(request.errbacks, app=self.app)

    def test_mark_as_failure__chord(self):
        b = BaseBackend(app=self.app)
        b._store_result = Mock()
        request = Mock(name='request')
        request.errbacks = []
        b.on_chord_part_return = Mock()
        exc = KeyError()
        b.mark_as_failure('id', exc, request=request)
        b.on_chord_part_return.assert_called_with(request, states.FAILURE, exc)

    def test_mark_as_revoked__chord(self):
        b = BaseBackend(app=self.app)
        b._store_result = Mock()
        request = Mock(name='request')
        request.errbacks = []
        b.on_chord_part_return = Mock()
        b.mark_as_revoked('id', 'revoked', request=request)
        b.on_chord_part_return.assert_called_with(request, states.REVOKED, ANY)

    def test_chord_error_from_stack_raises(self):
        class ExpectedException(Exception):
            pass

        b = BaseBackend(app=self.app)
        callback = MagicMock(name='callback')
        callback.options = {'link_error': []}
        callback.keys.return_value = []
        task = self.app.tasks[callback.task] = Mock()
        b.fail_from_current_stack = Mock()
        self.patching('celery.group')
        with patch.object(
            b, "_call_task_errbacks", side_effect=ExpectedException()
        ) as mock_call_errbacks:
            b.chord_error_from_stack(callback, exc=ValueError())
        task.backend.fail_from_current_stack.assert_called_with(
            callback.id, exc=mock_call_errbacks.side_effect,
        )

    def test_exception_to_python_when_None(self):
        b = BaseBackend(app=self.app)
        assert b.exception_to_python(None) is None

    def test_not_an_actual_exc_info(self):
        pass

    def test_not_an_exception_but_a_callable(self):
        x = {
            'exc_message': ('echo 1',),
            'exc_type': 'system',
            'exc_module': 'os'
        }

        with pytest.raises(SecurityError,
                           match=re.escape(r"Expected an exception class, got os.system with payload ('echo 1',)")):
            self.b.exception_to_python(x)

    def test_not_an_exception_but_another_object(self):
        x = {
            'exc_message': (),
            'exc_type': 'object',
            'exc_module': 'builtins'
        }

        with pytest.raises(SecurityError,
                           match=re.escape(r"Expected an exception class, got builtins.object with payload ()")):
            self.b.exception_to_python(x)

    def test_exception_to_python_when_attribute_exception(self):
        b = BaseBackend(app=self.app)
        test_exception = {'exc_type': 'AttributeDoesNotExist',
                          'exc_module': 'celery',
                          'exc_message': ['Raise Custom Message']}

        result_exc = b.exception_to_python(test_exception)
        assert str(result_exc) == 'Raise Custom Message'

    def test_exception_to_python_when_type_error(self):
        b = BaseBackend(app=self.app)
        celery.TestParamException = paramexception
        test_exception = {'exc_type': 'TestParamException',
                          'exc_module': 'celery',
                          'exc_message': []}

        result_exc = b.exception_to_python(test_exception)
        del celery.TestParamException
        assert str(result_exc) == "<class 't.unit.backends.test_base.paramexception'>([])"

    def test_wait_for__on_interval(self):
        self.patching('time.sleep')
        b = BaseBackend(app=self.app)
        b._get_task_meta_for = Mock()
        b._get_task_meta_for.return_value = {'status': states.PENDING}
        callback = Mock(name='callback')
        with pytest.raises(TimeoutError):
            b.wait_for(task_id='1', on_interval=callback, timeout=1)
        callback.assert_called_with()

        b._get_task_meta_for.return_value = {'status': states.SUCCESS}
        b.wait_for(task_id='1', timeout=None)

    def test_get_children(self):
        b = BaseBackend(app=self.app)
        b._get_task_meta_for = Mock()
        b._get_task_meta_for.return_value = {}
        assert b.get_children('id') is None
        b._get_task_meta_for.return_value = {'children': 3}
        assert b.get_children('id') == 3

    @pytest.mark.parametrize(
        "message,original_exc,expected_cause_behavior",
        [
            # With exception - should preserve original exception
            (
                "Dependency failed",
                ValueError("original error"),
                "has_cause",
            ),
            # Without exception (None) - should not have __cause__
            (
                "Dependency failed",
                None,
                "no_cause",
            ),
            # With non-exception - should not have __cause__
            (
                "Dependency failed",
                "not an exception",
                "no_cause",
            ),
        ],
        ids=(
            "with_exception",
            "without_exception",
            "with_non_exception",
        )
    )
    def test_create_chord_error_with_cause(
        self, message, original_exc, expected_cause_behavior
    ):
        """Test _create_chord_error_with_cause with various parameter combinations."""
        chord_error = _create_chord_error_with_cause(message, original_exc)

        # Verify basic ChordError properties
        assert isinstance(chord_error, ChordError)
        assert str(chord_error) == message

        # Verify __cause__ behavior based on test case
        if expected_cause_behavior == "has_cause":
            assert chord_error.__cause__ is original_exc
        elif expected_cause_behavior == "no_cause":
            assert not hasattr(chord_error, '__cause__') or chord_error.__cause__ is None

    @pytest.mark.parametrize(
        "task_id,errbacks,task_name,extra_kwargs,expected_attrs",
        [
            # Basic parameters test
            (
                "test-task-id",
                ["errback1", "errback2"],
                "test.task",
                {},
                {
                    "id": "test-task-id",
                    "errbacks": ["errback1", "errback2"],
                    "task": "test.task",
                    "delivery_info": {},
                },
            ),
            # Default parameters test
            (
                "test-task-id",
                None,
                None,
                {},
                {
                    "id": "test-task-id",
                    "errbacks": [],
                    "task": "unknown",
                    "delivery_info": {},
                },
            ),
            # Extra parameters test
            (
                "test-task-id",
                None,
                None,
                {"extra_param": "extra_value"},
                {
                    "id": "test-task-id",
                    "errbacks": [],
                    "task": "unknown",
                    "delivery_info": {},
                    "extra_param": "extra_value",
                },
            ),
        ],
        ids=(
            "basic_parameters",
            "default_parameters",
            "extra_parameters",
        )
    )
    def test_create_fake_task_request(
        self, task_id, errbacks, task_name, extra_kwargs, expected_attrs
    ):
        """Test _create_fake_task_request with various parameter combinations."""
        # Build call arguments
        args = [task_id]
        if errbacks is not None:
            args.append(errbacks)
        if task_name is not None:
            args.append(task_name)

        fake_request = _create_fake_task_request(*args, **extra_kwargs)

        # Verify all expected attributes
        for attr_name, expected_value in expected_attrs.items():
            assert getattr(fake_request, attr_name) == expected_value

    def _create_mock_callback(self, task_name="test.task", spec=None, **options):
        """Helper to create mock callbacks with common setup."""
        from collections.abc import Mapping

        # Create a mock that properly implements the
        # mapping protocol for PyPy env compatibility
        class MockCallback(Mock, Mapping):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._mapping_data = {}

            def __getitem__(self, key):
                return self._mapping_data[key]

            def __iter__(self):
                return iter(self._mapping_data)

            def __len__(self):
                return len(self._mapping_data)

            def keys(self):
                return self._mapping_data.keys()

            def items(self):
                return self._mapping_data.items()

        callback = MockCallback(spec=spec)
        callback.task = task_name
        callback.options = {"link_error": [], **options}

        return callback

    def _setup_task_backend(self, task_name, backend=None):
        """Helper to set up task with backend in app registry."""
        if backend is None:
            backend = Mock()
            backend.fail_from_current_stack = Mock(return_value="backend_result")

        self.app.tasks[task_name] = Mock()
        self.app.tasks[task_name].backend = backend
        return backend

    @pytest.mark.parametrize(
        "callback_type,task_name,expected_group_handler_called",
        [
            ("group", "test.group.task", True),
            ("regular", "test.task", False),
        ],
        ids=["group_callback", "regular_callback"]
    )
    def test_chord_error_from_stack_callback_dispatch(self, callback_type, task_name, expected_group_handler_called):
        """Test chord_error_from_stack dispatches to correct handler based on callback type."""
        backend = self.b

        # Create callback based on type
        spec = group if callback_type == "group" else None
        callback = self._create_mock_callback(task_name, spec=spec)

        # Setup backend resolution
        mock_backend = self._setup_task_backend(task_name)

        # Mock handlers
        backend._handle_group_chord_error = Mock(return_value="group_result")
        backend._call_task_errbacks = Mock()

        exc = ValueError("test exception")
        result = backend.chord_error_from_stack(callback, exc)

        if expected_group_handler_called:
            backend._handle_group_chord_error.assert_called_once_with(
                group_callback=callback, backend=mock_backend, exc=exc
            )
            assert result == "group_result"
        else:
            mock_backend.fail_from_current_stack.assert_called_once()

    def test_chord_error_from_stack_backend_fallback(self):
        """Test chord_error_from_stack falls back to self when task not found."""
        backend = self.b

        callback = self._create_mock_callback("nonexistent.task")

        # Ensure task doesn't exist
        if "nonexistent.task" in self.app.tasks:
            del self.app.tasks["nonexistent.task"]

        backend._call_task_errbacks = Mock()
        backend.fail_from_current_stack = Mock(return_value="self_result")

        _ = backend.chord_error_from_stack(callback, ValueError("test"))

        # Verify self was used as fallback backend
        backend.fail_from_current_stack.assert_called_once()

    def _create_mock_frozen_group(self, group_id="group-id", task_ids=None, task_names=None):
        """Helper to create mock frozen group with results."""
        if task_ids is None:
            task_ids = ["task-id-1"]
        if task_names is None:
            task_names = ["test.task"] * len(task_ids)

        results = []
        for task_id, task_name in zip(task_ids, task_names):
            result = Mock()
            result.id = task_id
            result.task = task_name
            results.append(result)

        frozen_group = Mock(spec=GroupResult)
        frozen_group.results = results
        frozen_group.id = group_id
        frozen_group.revoke = Mock()
        return frozen_group

    def _setup_group_chord_error_test(self, exc=None, errbacks=None, task_ids=None):
        """Common setup for group chord error tests."""
        if exc is None:
            exc = ValueError("test error")
        if errbacks is None:
            errbacks = []
        if task_ids is None:
            task_ids = ["task-id-1"]

        backend = Mock()
        backend._call_task_errbacks = Mock()
        backend.fail_from_current_stack = Mock()
        backend.mark_as_failure = Mock()

        group_callback = Mock(spec=group)
        group_callback.options = {"link_error": errbacks}

        frozen_group = self._create_mock_frozen_group(task_ids=task_ids)
        group_callback.freeze.return_value = frozen_group

        return self.b, backend, group_callback, frozen_group, exc

    @pytest.mark.parametrize(
        "exception_setup,expected_exc_used",
        [
            ("with_cause", "original"),
            ("without_cause", "direct"),
        ],
        ids=["extracts_cause", "without_cause"]
    )
    def test_handle_group_chord_error_exception_handling(self, exception_setup, expected_exc_used):
        """Test _handle_group_chord_error handles exceptions with and without __cause__."""
        # Setup exceptions based on test case
        if exception_setup == "with_cause":
            original_exc = ValueError("original error")
            exc = ChordError("wrapped error")
            exc.__cause__ = original_exc
            expected_exc = original_exc
        else:
            exc = ValueError("direct error")
            expected_exc = exc

        b, backend, group_callback, frozen_group, _ = self._setup_group_chord_error_test(exc=exc)

        # Call the method
        _ = b._handle_group_chord_error(group_callback, backend, exc)

        # Verify correct exception was used
        backend.fail_from_current_stack.assert_called_with("task-id-1", exc=expected_exc)
        backend.mark_as_failure.assert_called_with("group-id", expected_exc)
        frozen_group.revoke.assert_called_once()

    def test_handle_group_chord_error_multiple_tasks(self):
        """Test _handle_group_chord_error handles multiple tasks in group."""
        task_ids = ["task-id-1", "task-id-2"]
        b, backend, group_callback, frozen_group, exc = self._setup_group_chord_error_test(task_ids=task_ids)

        # Call the method
        b._handle_group_chord_error(group_callback, backend, exc)

        # Verify group revocation and all tasks handled
        frozen_group.revoke.assert_called_once()
        assert backend.fail_from_current_stack.call_count == 2
        backend.fail_from_current_stack.assert_any_call("task-id-1", exc=exc)
        backend.fail_from_current_stack.assert_any_call("task-id-2", exc=exc)

    def test_handle_group_chord_error_with_errbacks(self):
        """Test _handle_group_chord_error calls error callbacks for each task."""
        errbacks = ["errback1", "errback2"]
        b, backend, group_callback, frozen_group, exc = self._setup_group_chord_error_test(errbacks=errbacks)

        # Call the method
        b._handle_group_chord_error(group_callback, backend, exc)

        # Verify error callbacks were called
        backend._call_task_errbacks.assert_called_once()
        call_args = backend._call_task_errbacks.call_args
        fake_request = call_args[0][0]

        # Verify fake request was created correctly
        assert fake_request.id == "task-id-1"
        assert fake_request.errbacks == errbacks
        assert fake_request.task == "test.task"

    def test_handle_group_chord_error_cleanup_exception_handling(self):
        """Test _handle_group_chord_error handles cleanup exceptions gracefully."""
        b = self.b
        backend = Mock()

        exc = ValueError("test error")

        # Mock group callback that raises exception during freeze
        group_callback = Mock(spec=group)
        group_callback.freeze.side_effect = RuntimeError("freeze failed")

        # Mock fallback behavior
        backend.fail_from_current_stack = Mock(return_value="fallback_result")

        # Should not raise exception, but return fallback result
        result = b._handle_group_chord_error(group_callback, backend, exc)

        # Verify fallback was called - the method returns an ExceptionInfo when cleanup fails
        # and falls back to single task handling
        assert result is not None  # Method returns ExceptionInfo from fail_from_current_stack

    def test_handle_group_chord__exceptions_paths(self, caplog):
        """Test _handle_group_chord handles exceptions in various paths."""
        backend = Mock()

        # Mock group callback
        group_callback = Mock(spec=group)
        group_callback.options = {"link_error": []}

        # Mock frozen group with multiple results
        mock_result1 = Mock()
        mock_result1.id = "task-id-1"
        mock_result2 = Mock()
        mock_result2.id = "task-id-2"

        frozen_group = Mock(spec=GroupResult)
        frozen_group.results = [mock_result1, mock_result2]
        frozen_group.revoke = Mock()

        group_callback.freeze.return_value = frozen_group

        # Test exception during fail_from_current_stack
        backend._call_task_errbacks.side_effect = RuntimeError("fail on _call_task_errbacks")

        backend.fail_from_current_stack.side_effect = RuntimeError("fail on fail_from_current_stack")

        _ = self.b._handle_group_chord_error(group_callback, backend, ValueError("test error"))

        assert "Failed to handle chord error for task" in caplog.text


class test_KeyValueStoreBackend:

    def setup_method(self):
        self.b = KVBackend(app=self.app)

    def test_on_chord_part_return(self):
        assert not self.b.implements_incr
        self.b.on_chord_part_return(None, None, None)

    def test_get_store_delete_result(self):
        tid = uuid()
        self.b.mark_as_done(tid, 'Hello world')
        assert self.b.get_result(tid) == 'Hello world'
        assert self.b.get_state(tid) == states.SUCCESS
        self.b.forget(tid)
        assert self.b.get_state(tid) == states.PENDING

    @pytest.mark.parametrize('serializer',
                             ['json', 'pickle', 'yaml', 'msgpack'])
    def test_store_result_parent_id(self, serializer):
        self.app.conf.accept_content = ('json', serializer)
        self.b = KVBackend(app=self.app, serializer=serializer)
        tid = uuid()
        pid = uuid()
        state = 'SUCCESS'
        result = 10
        request = Context(parent_id=pid)
        self.b.store_result(
            tid, state=state, result=result, request=request,
        )
        stored_meta = self.b.decode(self.b.get(self.b.get_key_for_task(tid)))
        assert stored_meta['parent_id'] == request.parent_id

    def test_store_result_group_id(self):
        tid = uuid()
        state = 'SUCCESS'
        result = 10
        request = Context(group='gid', children=[])
        self.b.store_result(
            tid, state=state, result=result, request=request,
        )
        stored_meta = self.b.decode(self.b.get(self.b.get_key_for_task(tid)))
        assert stored_meta['group_id'] == request.group

    def test_store_result_race_second_write_should_ignore_if_previous_success(self):
        tid = uuid()
        state = 'SUCCESS'
        result = 10
        request = Context(group='gid', children=[])
        self.b.store_result(
            tid, state=state, result=result, request=request,
        )
        self.b.store_result(
            tid, state=states.FAILURE, result=result, request=request,
        )
        stored_meta = self.b.decode(self.b.get(self.b.get_key_for_task(tid)))
        assert stored_meta['status'] == states.SUCCESS

    def test_get_key_for_task_none_task_id(self):
        with pytest.raises(ValueError):
            self.b.get_key_for_task(None)

    def test_get_key_for_group_none_group_id(self):
        with pytest.raises(ValueError):
            self.b.get_key_for_task(None)

    def test_get_key_for_chord_none_group_id(self):
        with pytest.raises(ValueError):
            self.b.get_key_for_group(None)

    def test_strip_prefix(self):
        x = self.b.get_key_for_task('x1b34')
        assert self.b._strip_prefix(x) == 'x1b34'
        assert self.b._strip_prefix('x1b34') == 'x1b34'

    def test_global_keyprefix(self):
        global_keyprefix = "test_global_keyprefix"
        app = copy.deepcopy(self.app)
        app.conf.get('result_backend_transport_options', {}).update({"global_keyprefix": global_keyprefix})
        b = KVBackend(app=app)
        tid = uuid()
        assert bytes_to_str(b.get_key_for_task(tid)) == f"{global_keyprefix}_celery-task-meta-{tid}"
        assert bytes_to_str(b.get_key_for_group(tid)) == f"{global_keyprefix}_celery-taskset-meta-{tid}"
        assert bytes_to_str(b.get_key_for_chord(tid)) == f"{global_keyprefix}_chord-unlock-{tid}"

        global_keyprefix = "test_global_keyprefix_"
        app = copy.deepcopy(self.app)
        app.conf.get('result_backend_transport_options', {}).update({"global_keyprefix": global_keyprefix})
        b = KVBackend(app=app)
        tid = uuid()
        assert bytes_to_str(b.get_key_for_task(tid)) == f"{global_keyprefix}celery-task-meta-{tid}"
        assert bytes_to_str(b.get_key_for_group(tid)) == f"{global_keyprefix}celery-taskset-meta-{tid}"
        assert bytes_to_str(b.get_key_for_chord(tid)) == f"{global_keyprefix}chord-unlock-{tid}"

        global_keyprefix = "test_global_keyprefix:"
        app = copy.deepcopy(self.app)
        app.conf.get('result_backend_transport_options', {}).update({"global_keyprefix": global_keyprefix})
        b = KVBackend(app=app)
        tid = uuid()
        assert bytes_to_str(b.get_key_for_task(tid)) == f"{global_keyprefix}celery-task-meta-{tid}"
        assert bytes_to_str(b.get_key_for_group(tid)) == f"{global_keyprefix}celery-taskset-meta-{tid}"
        assert bytes_to_str(b.get_key_for_chord(tid)) == f"{global_keyprefix}chord-unlock-{tid}"

    def test_global_keyprefix_missing(self):
        tid = uuid()
        assert bytes_to_str(self.b.get_key_for_task(tid)) == f"celery-task-meta-{tid}"
        assert bytes_to_str(self.b.get_key_for_group(tid)) == f"celery-taskset-meta-{tid}"
        assert bytes_to_str(self.b.get_key_for_chord(tid)) == f"chord-unlock-{tid}"

    def test_get_many(self):
        for is_dict in True, False:
            self.b.mget_returns_dict = is_dict
            ids = {uuid(): i for i in range(10)}
            for id, i in ids.items():
                self.b.mark_as_done(id, i)
            it = self.b.get_many(list(ids), interval=0.01)
            for i, (got_id, got_state) in enumerate(it):
                assert got_state['result'] == ids[got_id]
            assert i == 9
            assert list(self.b.get_many(list(ids), interval=0.01))

            self.b._cache.clear()
            callback = Mock(name='callback')
            it = self.b.get_many(
                list(ids),
                on_message=callback,
                interval=0.05
            )
            for i, (got_id, got_state) in enumerate(it):
                assert got_state['result'] == ids[got_id]
            assert i == 9
            assert list(
                self.b.get_many(list(ids), interval=0.01)
            )
            callback.assert_has_calls([
                call(ANY) for id in ids
            ])

    def test_get_many_times_out(self):
        tasks = [uuid() for _ in range(4)]
        self.b._cache[tasks[1]] = {'status': 'PENDING'}
        with pytest.raises(self.b.TimeoutError):
            list(self.b.get_many(tasks, timeout=0.01, interval=0.01))

    def test_get_many_passes_ready_states(self):
        tasks_length = 10
        ready_states = frozenset({states.SUCCESS})

        self.b._cache.clear()
        ids = {uuid(): i for i in range(tasks_length)}
        for id, i in ids.items():
            if i % 2 == 0:
                self.b.mark_as_done(id, i)
            else:
                self.b.mark_as_failure(id, Exception())

        it = self.b.get_many(list(ids), interval=0.01, max_iterations=1, READY_STATES=ready_states)
        it_list = list(it)

        assert all([got_state['status'] in ready_states for (got_id, got_state) in it_list])
        assert len(it_list) == tasks_length / 2

    def test_chord_part_return_no_gid(self):
        self.b.implements_incr = True
        task = Mock()
        state = 'SUCCESS'
        result = 10
        task.request.group = None
        self.b.get_key_for_chord = Mock()
        self.b.get_key_for_chord.side_effect = AssertionError(
            'should not get here',
        )
        assert self.b.on_chord_part_return(
            task.request, state, result) is None

    @patch('celery.backends.base.GroupResult')
    @patch('celery.backends.base.maybe_signature')
    def test_chord_part_return_restore_raises(self, maybe_signature,
                                              GroupResult):
        self.b.implements_incr = True
        GroupResult.restore.side_effect = KeyError()
        self.b.chord_error_from_stack = Mock()
        callback = Mock(name='callback')
        request = Mock(name='request')
        request.group = 'gid'
        maybe_signature.return_value = callback
        self.b.on_chord_part_return(request, states.SUCCESS, 10)
        self.b.chord_error_from_stack.assert_called_with(
            callback, ANY,
        )

    @patch('celery.backends.base.GroupResult')
    @patch('celery.backends.base.maybe_signature')
    def test_chord_part_return_restore_empty(self, maybe_signature,
                                             GroupResult):
        self.b.implements_incr = True
        GroupResult.restore.return_value = None
        self.b.chord_error_from_stack = Mock()
        callback = Mock(name='callback')
        request = Mock(name='request')
        request.group = 'gid'
        maybe_signature.return_value = callback
        self.b.on_chord_part_return(request, states.SUCCESS, 10)
        self.b.chord_error_from_stack.assert_called_with(
            callback, ANY,
        )

    def test_filter_ready(self):
        self.b.decode_result = Mock()
        self.b.decode_result.side_effect = pass1
        assert len(list(self.b._filter_ready([
            (1, {'status': states.RETRY}),
            (2, {'status': states.FAILURE}),
            (3, {'status': states.SUCCESS}),
        ]))) == 2

    @contextmanager
    def _chord_part_context(self, b):

        @self.app.task(shared=False)
        def callback(result):
            pass

        b.implements_incr = True
        b.client = Mock()
        with patch('celery.backends.base.GroupResult') as GR:
            deps = GR.restore.return_value = Mock(name='DEPS')
            deps.__len__ = Mock()
            deps.__len__.return_value = 10
            b.incr = Mock()
            b.incr.return_value = 10
            b.expire = Mock()
            task = Mock()
            task.request.group = 'grid'
            cb = task.request.chord = callback.s()
            task.request.chord.freeze()
            callback.backend = b
            callback.backend.fail_from_current_stack = Mock()
            yield task, deps, cb

    def test_chord_part_return_timeout(self):
        with self._chord_part_context(self.b) as (task, deps, _):
            try:
                self.app.conf.result_chord_join_timeout += 1.0
                self.b.on_chord_part_return(task.request, 'SUCCESS', 10)
            finally:
                self.app.conf.result_chord_join_timeout -= 1.0

            self.b.expire.assert_not_called()
            deps.delete.assert_called_with()
            deps.join_native.assert_called_with(propagate=True, timeout=4.0)

    def test_chord_part_return_propagate_set(self):
        with self._chord_part_context(self.b) as (task, deps, _):
            self.b.on_chord_part_return(task.request, 'SUCCESS', 10)
            self.b.expire.assert_not_called()
            deps.delete.assert_called_with()
            deps.join_native.assert_called_with(propagate=True, timeout=3.0)

    def test_chord_part_return_propagate_default(self):
        with self._chord_part_context(self.b) as (task, deps, _):
            self.b.on_chord_part_return(task.request, 'SUCCESS', 10)
            self.b.expire.assert_not_called()
            deps.delete.assert_called_with()
            deps.join_native.assert_called_with(propagate=True, timeout=3.0)

    def test_chord_part_return_join_raises_internal(self):
        with self._chord_part_context(self.b) as (task, deps, callback):
            deps._failed_join_report = lambda: iter([])
            deps.join_native.side_effect = KeyError('foo')
            self.b.on_chord_part_return(task.request, 'SUCCESS', 10)
            self.b.fail_from_current_stack.assert_called()
            args = self.b.fail_from_current_stack.call_args
            exc = args[1]['exc']
            assert isinstance(exc, ChordError)
            assert 'foo' in str(exc)

    def test_chord_part_return_join_raises_task(self):
        b = KVBackend(serializer='pickle', app=self.app)
        with self._chord_part_context(b) as (task, deps, callback):
            deps._failed_join_report = lambda: iter([
                self.app.AsyncResult('culprit'),
            ])
            deps.join_native.side_effect = KeyError('foo')
            b.on_chord_part_return(task.request, 'SUCCESS', 10)
            b.fail_from_current_stack.assert_called()
            args = b.fail_from_current_stack.call_args
            exc = args[1]['exc']
            assert isinstance(exc, ChordError)
            assert 'Dependency culprit raised' in str(exc)

    def test_restore_group_from_json(self):
        b = KVBackend(serializer='json', app=self.app)
        g = self.app.GroupResult(
            'group_id',
            [self.app.AsyncResult('a'), self.app.AsyncResult('b')],
        )
        b._save_group(g.id, g)
        g2 = b._restore_group(g.id)['result']
        assert g2 == g

    def test_restore_group_from_pickle(self):
        b = KVBackend(serializer='pickle', app=self.app)
        g = self.app.GroupResult(
            'group_id',
            [self.app.AsyncResult('a'), self.app.AsyncResult('b')],
        )
        b._save_group(g.id, g)
        g2 = b._restore_group(g.id)['result']
        assert g2 == g

    def test_chord_apply_fallback(self):
        self.b.implements_incr = False
        self.b.fallback_chord_unlock = Mock()
        header_result_args = (
            'group_id',
            [self.app.AsyncResult(x) for x in range(3)],
        )
        self.b.apply_chord(
            header_result_args, 'body', foo=1,
        )
        self.b.fallback_chord_unlock.assert_called_with(
            self.app.GroupResult(*header_result_args), 'body', foo=1,
        )

    def test_get_missing_meta(self):
        assert self.b.get_result('xxx-missing') is None
        assert self.b.get_state('xxx-missing') == states.PENDING

    def test_save_restore_delete_group(self):
        tid = uuid()
        tsr = self.app.GroupResult(
            tid, [self.app.AsyncResult(uuid()) for _ in range(10)],
        )
        self.b.save_group(tid, tsr)
        self.b.restore_group(tid)
        assert self.b.restore_group(tid) == tsr
        self.b.delete_group(tid)
        assert self.b.restore_group(tid) is None

    def test_restore_missing_group(self):
        assert self.b.restore_group('xxx-nonexistant') is None


class test_KeyValueStoreBackend_interface:

    def test_get(self):
        with pytest.raises(NotImplementedError):
            KeyValueStoreBackend(self.app).get('a')

    def test_set(self):
        with pytest.raises(NotImplementedError):
            KeyValueStoreBackend(self.app)._set_with_state('a', 1, states.SUCCESS)

    def test_incr(self):
        with pytest.raises(NotImplementedError):
            KeyValueStoreBackend(self.app).incr('a')

    def test_cleanup(self):
        assert not KeyValueStoreBackend(self.app).cleanup()

    def test_delete(self):
        with pytest.raises(NotImplementedError):
            KeyValueStoreBackend(self.app).delete('a')

    def test_mget(self):
        with pytest.raises(NotImplementedError):
            KeyValueStoreBackend(self.app).mget(['a'])

    def test_forget(self):
        with pytest.raises(NotImplementedError):
            KeyValueStoreBackend(self.app).forget('a')


class test_DisabledBackend:

    def test_store_result(self):
        DisabledBackend(self.app).store_result()

    def test_is_disabled(self):
        with pytest.raises(NotImplementedError):
            DisabledBackend(self.app).get_state('foo')

    def test_as_uri(self):
        assert DisabledBackend(self.app).as_uri() == 'disabled://'

    @pytest.mark.celery(result_backend='disabled')
    def test_chord_raises_error(self):
        with pytest.raises(NotImplementedError):
            chord(self.add.s(i, i) for i in range(10))(self.add.s([2]))

    @pytest.mark.celery(result_backend='disabled')
    def test_chain_with_chord_raises_error(self):
        with pytest.raises(NotImplementedError):
            (self.add.s(2, 2) |
             group(self.add.s(2, 2),
                   self.add.s(5, 6)) | self.add.s()).delay()


class test_as_uri:

    def setup_method(self):
        self.b = BaseBackend(
            app=self.app,
            url='sch://uuuu:pwpw@hostname.dom'
        )

    def test_as_uri_include_password(self):
        assert self.b.as_uri(True) == self.b.url

    def test_as_uri_exclude_password(self):
        assert self.b.as_uri() == 'sch://uuuu:**@hostname.dom/'


class test_backend_retries:

    def test_should_retry_exception(self):
        assert not BaseBackend(app=self.app).exception_safe_to_retry(Exception("test"))

    def test_get_failed_never_retries(self):
        self.app.conf.result_backend_always_retry, prev = False, self.app.conf.result_backend_always_retry

        expected_exc = Exception("failed")
        try:
            b = BaseBackend(app=self.app)
            b.exception_safe_to_retry = lambda exc: True
            b._sleep = Mock()
            b._get_task_meta_for = Mock()
            b._get_task_meta_for.side_effect = [
                expected_exc,
                {'status': states.SUCCESS, 'result': 42}
            ]
            try:
                b.get_task_meta(sentinel.task_id)
                assert False
            except Exception as exc:
                assert b._sleep.call_count == 0
                assert exc == expected_exc
        finally:
            self.app.conf.result_backend_always_retry = prev

    def test_get_with_retries(self):
        self.app.conf.result_backend_always_retry, prev = True, self.app.conf.result_backend_always_retry

        try:
            b = BaseBackend(app=self.app)
            b.exception_safe_to_retry = lambda exc: True
            b._sleep = Mock()
            b._get_task_meta_for = Mock()
            b._get_task_meta_for.side_effect = [
                Exception("failed"),
                {'status': states.SUCCESS, 'result': 42}
            ]
            res = b.get_task_meta(sentinel.task_id)
            assert res == {'status': states.SUCCESS, 'result': 42}
            assert b._sleep.call_count == 1
        finally:
            self.app.conf.result_backend_always_retry = prev

    def test_get_reaching_max_retries(self):
        self.app.conf.result_backend_always_retry, prev = True, self.app.conf.result_backend_always_retry
        self.app.conf.result_backend_max_retries, prev_max_retries = 0, self.app.conf.result_backend_max_retries

        try:
            b = BaseBackend(app=self.app)
            b.exception_safe_to_retry = lambda exc: True
            b._sleep = Mock()
            b._get_task_meta_for = Mock()
            b._get_task_meta_for.side_effect = [
                Exception("failed"),
                {'status': states.SUCCESS, 'result': 42}
            ]
            try:
                b.get_task_meta(sentinel.task_id)
                assert False
            except BackendGetMetaError:
                assert b._sleep.call_count == 0
        finally:
            self.app.conf.result_backend_always_retry = prev
            self.app.conf.result_backend_max_retries = prev_max_retries

    def test_get_unsafe_exception(self):
        self.app.conf.result_backend_always_retry, prev = True, self.app.conf.result_backend_always_retry

        expected_exc = Exception("failed")
        try:
            b = BaseBackend(app=self.app)
            b._sleep = Mock()
            b._get_task_meta_for = Mock()
            b._get_task_meta_for.side_effect = [
                expected_exc,
                {'status': states.SUCCESS, 'result': 42}
            ]
            try:
                b.get_task_meta(sentinel.task_id)
                assert False
            except Exception as exc:
                assert b._sleep.call_count == 0
                assert exc == expected_exc
        finally:
            self.app.conf.result_backend_always_retry = prev

    def test_store_result_never_retries(self):
        self.app.conf.result_backend_always_retry, prev = False, self.app.conf.result_backend_always_retry

        expected_exc = Exception("failed")
        try:
            b = BaseBackend(app=self.app)
            b.exception_safe_to_retry = lambda exc: True
            b._sleep = Mock()
            b._get_task_meta_for = Mock()
            b._get_task_meta_for.return_value = {
                'status': states.RETRY,
                'result': {
                    "exc_type": "Exception",
                    "exc_message": ["failed"],
                    "exc_module": "builtins",
                },
            }
            b._store_result = Mock()
            b._store_result.side_effect = [
                expected_exc,
                42
            ]
            try:
                b.store_result(sentinel.task_id, 42, states.SUCCESS)
            except Exception as exc:
                assert b._sleep.call_count == 0
                assert exc == expected_exc
        finally:
            self.app.conf.result_backend_always_retry = prev

    def test_store_result_with_retries(self):
        self.app.conf.result_backend_always_retry, prev = True, self.app.conf.result_backend_always_retry

        try:
            b = BaseBackend(app=self.app)
            b.exception_safe_to_retry = lambda exc: True
            b._sleep = Mock()
            b._get_task_meta_for = Mock()
            b._get_task_meta_for.return_value = {
                'status': states.RETRY,
                'result': {
                    "exc_type": "Exception",
                    "exc_message": ["failed"],
                    "exc_module": "builtins",
                },
            }
            b._store_result = Mock()
            b._store_result.side_effect = [
                Exception("failed"),
                42
            ]
            res = b.store_result(sentinel.task_id, 42, states.SUCCESS)
            assert res == 42
            assert b._sleep.call_count == 1
        finally:
            self.app.conf.result_backend_always_retry = prev

    def test_store_result_reaching_max_retries(self):
        self.app.conf.result_backend_always_retry, prev = True, self.app.conf.result_backend_always_retry
        self.app.conf.result_backend_max_retries, prev_max_retries = 0, self.app.conf.result_backend_max_retries

        try:
            b = BaseBackend(app=self.app)
            b.exception_safe_to_retry = lambda exc: True
            b._sleep = Mock()
            b._get_task_meta_for = Mock()
            b._get_task_meta_for.return_value = {
                'status': states.RETRY,
                'result': {
                    "exc_type": "Exception",
                    "exc_message": ["failed"],
                    "exc_module": "builtins",
                },
            }
            b._store_result = Mock()
            b._store_result.side_effect = [
                Exception("failed"),
                42
            ]
            try:
                b.store_result(sentinel.task_id, 42, states.SUCCESS)
                assert False
            except BackendStoreError:
                assert b._sleep.call_count == 0
        finally:
            self.app.conf.result_backend_always_retry = prev
            self.app.conf.result_backend_max_retries = prev_max_retries

    def test_result_backend_thread_safe(self):
        # Should identify the backend as thread safe
        self.app.conf.result_backend_thread_safe = True
        b = BaseBackend(app=self.app)
        assert b.thread_safe is True

    def test_result_backend_not_thread_safe(self):
        # Should identify the backend as not being thread safe
        self.app.conf.result_backend_thread_safe = False
        b = BaseBackend(app=self.app)
        assert b.thread_safe is False
