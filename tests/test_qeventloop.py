# © 2013 Mark Harviston <mark.harviston@gmail.com>
# © 2014 Arve Knudsen <arve.knudsen@gmail.com>
# BSD License
import asyncio
import locale
import logging
import sys
import ctypes
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import socket
import subprocess

import quamash

import pytest


class _SubprocessProtocol(asyncio.SubprocessProtocol):
	def __init__(self, *args, **kwds):
		super(_SubprocessProtocol, self).__init__(*args, **kwds)
		self.received_stdout = None

	def pipe_data_received(self, fd, data):
		text = data.decode(locale.getpreferredencoding(False))
		if fd == 1:
			self.received_stdout = text.strip()

	def process_exited(self):
		asyncio.get_event_loop().stop()


@pytest.fixture(scope='session')
def application():
	return quamash.QApplication.instance() or quamash.QApplication([])


@pytest.fixture
def loop(request, application):
	lp = quamash.QEventLoop(application)
	asyncio.set_event_loop(lp)

	additional_exceptions = []

	def fin():
		sys.excepthook = orig_excepthook

		try:
			lp.close()
		finally:
			asyncio.set_event_loop(None)

		if additional_exceptions:
			raise additional_exceptions[0]['exception']

	def except_handler(loop, ctx):
		additional_exceptions.append(ctx)

	def excepthook(type, *args):
		lp.stop()
		orig_excepthook(type, *args)

	orig_excepthook = sys.excepthook
	sys.excepthook = excepthook
	lp.set_exception_handler(except_handler)

	request.addfinalizer(fin)
	return lp


@pytest.fixture(
	params=[None, quamash.QThreadExecutor, ThreadPoolExecutor, ProcessPoolExecutor]
)
def executor(request):
	exc_cls = request.param
	if exc_cls is None:
		return None

	exc = exc_cls(1)  # FIXME? fixed number of workers?
	request.addfinalizer(exc.shutdown)
	return exc


TestException = type('TestException', (Exception,), {})  # to make flake8 not complain


class TestCanRunTasksInExecutor:

	"""
	Test Cases Concerning running jobs in Executors.

	This needs to be a class because pickle can't serialize closures,
	but can serialize bound methods.
	multiprocessing can only handle pickleable functions.
	"""

	def test_can_run_tasks_in_executor(self, loop, executor):
		"""Verify that tasks can be run in an executor."""
		logging.debug('Loop: {!r}'.format(loop))
		logging.debug('Executor: {!r}'.format(executor))

		manager = multiprocessing.Manager()
		was_invoked = manager.Value(ctypes.c_int, 0)
		logging.debug('running until complete')
		loop.run_until_complete(self.blocking_task(loop, executor, was_invoked))
		logging.debug('ran')

		assert was_invoked.value == 1

	def test_can_handle_exception_in_executor(self, loop, executor):
		with pytest.raises(TestException) as excinfo:
			loop.run_until_complete(asyncio.wait_for(
				loop.run_in_executor(executor, self.blocking_failure),
				timeout=3.0,
			))

		assert str(excinfo.value) == 'Testing'

	def blocking_failure(self):
		logging.debug('raising')
		try:
			raise TestException('Testing')
		finally:
			logging.debug('raised!')

	def blocking_func(self, was_invoked):
		logging.debug('start blocking_func()')
		was_invoked.value = 1
		logging.debug('end blocking_func()')

	@asyncio.coroutine
	def blocking_task(self, loop, executor, was_invoked):
		logging.debug('start blocking task()')
		fut = loop.run_in_executor(executor, self.blocking_func, was_invoked)
		yield from asyncio.wait_for(fut, timeout=5.0)
		logging.debug('start blocking task()')


def test_can_execute_subprocess_primitive(loop):
	"""Verify that a subprocess can be executed using low-level api."""
	transport, protocol = loop.run_until_complete(
		loop.subprocess_exec(
			_SubprocessProtocol, sys.executable or 'python', '-c', 'import sys; sys.exit(5)',
		),
	)
	loop.run_forever()
	assert transport.get_returncode() == 5


def test_can_execute_subprocess(loop):
	"""Verify that a subprocess can be executed."""
	@asyncio.coroutine
	def mycoro():
		process = yield from asyncio.create_subprocess_exec(
			sys.executable or 'python', '-c', 'import sys; sys.exit(5)')
		yield from process.wait()
		assert process.returncode == 5
	loop.run_until_complete(asyncio.wait_for(mycoro(), timeout=3))


def test_can_read_subprocess_primitive(loop):
	transport, protocol = loop.run_until_complete(
		loop.subprocess_exec(
			_SubprocessProtocol, sys.executable or 'python', '-c', 'print("Hello async world!")',
		),
	)
	loop.run_forever()
	assert transport.get_returncode() == 0
	assert protocol.received_stdout == "Hello async world!"


def test_can_read_subprocess(loop):
	"""Verify that a subprocess's data can be read from stdout."""
	@asyncio.coroutine
	def mycoro():
		process = yield from asyncio.create_subprocess_exec(
			sys.executable or 'python', '-c', 'print("Hello async world!")', stdout=subprocess.PIPE)
		received_stdout = yield from process.stdout.readexactly(len(b'Hello async world!\n'))
		yield from process.wait()
		assert process.returncode == 0
		assert received_stdout.strip() == b'Hello async world!'

	loop.run_until_complete(asyncio.wait_for(mycoro(), timeout=3))


def test_can_communicate_subprocess(loop):
	"""Verify that a subprocess's data can be passed in/out via stdin/stdout."""
	@asyncio.coroutine
	def mycoro():
		process = yield from asyncio.create_subprocess_exec(
			sys.executable or 'python', '-c', 'print(input())', stdout=subprocess.PIPE, stdin=subprocess.PIPE)
		received_stdout, received_stderr = yield from process.communicate(b'Hello async world!\n')
		yield from process.wait()
		assert process.returncode == 0
		assert received_stdout.strip() == b'Hello async world!'
	loop.run_until_complete(asyncio.wait_for(mycoro(), timeout=3))


def test_can_terminate_subprocess(loop):
	"""Verify that a subprocess can be terminated."""
	# Start a never-ending process
	transport = loop.run_until_complete(
		loop.subprocess_exec(
			_SubprocessProtocol, sys.executable or 'python', '-c', 'import time\nwhile True: time.sleep(1)',
		),
	)[0]
	# Terminate!
	transport.kill()
	# Wait for process to die
	loop.run_forever()

	assert transport.get_returncode() != 0


@pytest.mark.raises(TestException)
def test_loop_callback_exceptions_bubble_up(loop):
	"""Verify that test exceptions raised in event loop callbacks bubble up."""
	def raise_test_exception():
		raise TestException("Test Message")
	loop.call_soon(raise_test_exception)
	loop.run_until_complete(asyncio.sleep(.1))


def test_loop_running(loop):
	"""Verify that loop.is_running returns True when running."""
	@asyncio.coroutine
	def is_running():
		nonlocal loop
		assert loop.is_running()

	loop.run_until_complete(is_running())


def test_loop_not_running(loop):
	"""Verify that loop.is_running returns False when not running."""
	assert not loop.is_running()


def test_can_function_as_context_manager(application):
	"""Verify that a QEventLoop can function as its own context manager."""
	with quamash.QEventLoop(application) as loop:
		assert isinstance(loop, quamash.QEventLoop)
		loop.call_soon(loop.stop)
		loop.run_forever()


def test_future_not_done_on_loop_shutdown(loop):
	"""Verify RuntimError occurs when loop stopped before Future completed with run_until_complete."""
	loop.call_later(.1, loop.stop)
	fut = asyncio.Future()
	with pytest.raises(RuntimeError):
		loop.run_until_complete(fut)


def test_call_later_must_not_coroutine(loop):
	"""Verify TypeError occurs call_later is given a coroutine."""
	mycoro = asyncio.coroutine(lambda: None)

	with pytest.raises(TypeError):
		loop.call_soon(mycoro)


def test_call_later_must_be_callable(loop):
	"""Verify TypeError occurs call_later is not given a callable."""
	not_callable = object()
	with pytest.raises(TypeError):
		loop.call_soon(not_callable)


def test_call_at(loop):
	"""Verify that loop.call_at works as expected."""
	def mycallback():
		nonlocal was_invoked
		was_invoked = True
	was_invoked = False

	loop.call_at(loop.time() + .05, mycallback)
	loop.run_until_complete(asyncio.sleep(.1))

	assert was_invoked


def test_get_set_debug(loop):
	"""Verify get_debug and set_debug work as expected."""
	loop.set_debug(True)
	assert loop.get_debug()
	loop.set_debug(False)
	assert not loop.get_debug()


@pytest.fixture
def sock_pair(request):
	"""Create socket pair.

	If socket.socketpair isn't available, we emulate it.
	"""
	def fin():
		if client_sock is not None:
			client_sock.close()
		if srv_sock is not None:
			srv_sock.close()

	client_sock = srv_sock = None
	request.addfinalizer(fin)

	# See if socketpair() is available.
	have_socketpair = hasattr(socket, 'socketpair')
	if have_socketpair:
		client_sock, srv_sock = socket.socketpair()
		return client_sock, srv_sock

	# Create a non-blocking temporary server socket
	temp_srv_sock = socket.socket()
	temp_srv_sock.setblocking(False)
	temp_srv_sock.bind(('', 0))
	port = temp_srv_sock.getsockname()[1]
	temp_srv_sock.listen(1)

	# Create non-blocking client socket
	client_sock = socket.socket()
	client_sock.setblocking(False)
	try:
		client_sock.connect(('localhost', port))
	except socket.error as err:
		# Error 10035 (operation would block) is not an error, as we're doing this with a
		# non-blocking socket.
		if err.errno != 10035:
			raise

	# Use select to wait for connect() to succeed.
	import select
	timeout = 1
	readable = select.select([temp_srv_sock], [], [], timeout)[0]
	if temp_srv_sock not in readable:
		raise Exception('Client socket not connected in {} second(s)'.format(timeout))
	srv_sock, _ = temp_srv_sock.accept()

	return client_sock, srv_sock


def test_can_add_reader(loop, sock_pair):
	"""Verify that we can add a reader callback to an event loop."""
	def can_read():
		if fut.done():
			return

		data = srv_sock.recv(1)
		if len(data) != 1:
			return

		nonlocal got_msg
		got_msg = data
		# Indicate that we're done
		fut.set_result(None)
		srv_sock.close()

	def write():
		client_sock.send(ref_msg)
		client_sock.close()

	ref_msg = b'a'
	client_sock, srv_sock = sock_pair
	loop.call_soon(write)

	exp_num_notifiers = len(loop._read_notifiers) + 1
	got_msg = None
	fut = asyncio.Future()
	loop.add_reader(srv_sock.fileno(), can_read)
	assert len(loop._read_notifiers) == exp_num_notifiers, 'Notifier should be added'
	loop.run_until_complete(asyncio.wait_for(fut, timeout=1.0))

	assert got_msg == ref_msg


def test_can_remove_reader(loop, sock_pair):
	"""Verify that we can remove a reader callback from an event loop."""
	def can_read():
		data = srv_sock.recv(1)
		if len(data) != 1:
			return

		nonlocal got_msg
		got_msg = data

	client_sock, srv_sock = sock_pair

	got_msg = None
	loop.add_reader(srv_sock.fileno(), can_read)
	exp_num_notifiers = len(loop._read_notifiers) - 1
	loop.remove_reader(srv_sock.fileno())
	assert len(loop._read_notifiers) == exp_num_notifiers, 'Notifier should be removed'
	client_sock.send(b'a')
	client_sock.close()
	# Run for a short while to see if we get a read notification
	loop.call_later(0.1, loop.stop)
	loop.run_forever()

	assert got_msg is None, 'Should not have received a read notification'


def test_can_add_writer(loop, sock_pair):
	"""Verify that we can add a writer callback to an event loop."""
	def can_write():
		if not fut.done():
			# Indicate that we're done
			fut.set_result(None)
			client_sock.close()

	client_sock, _ = sock_pair
	fut = asyncio.Future()
	loop.add_writer(client_sock.fileno(), can_write)
	assert len(loop._write_notifiers) == 1, 'Notifier should be added'
	loop.run_until_complete(asyncio.wait_for(fut, timeout=1.0))


def test_can_remove_writer(loop, sock_pair):
	"""Verify that we can remove a writer callback from an event loop."""
	client_sock, _ = sock_pair
	loop.add_writer(client_sock.fileno(), lambda: None)
	loop.remove_writer(client_sock.fileno())
	assert not loop._write_notifiers, 'Notifier should be removed'


def test_add_reader_should_disable_qsocket_notifier_on_callback(loop, sock_pair):
	"""Verify that add_reader disables QSocketNotifier during callback."""
	def can_read():
		nonlocal num_calls
		num_calls += 1

		if num_calls == 2:
			# Since we get called again, the QSocketNotifier should've been re-enabled before
			# this call (although disabled during)
			assert not notifier.isEnabled()
			srv_sock.recv(1)
			fut.set_result(None)
			srv_sock.close()
			return

		assert not notifier.isEnabled()

	def write():
		client_sock.send(b'a')
		client_sock.close()

	num_calls = 0
	client_sock, srv_sock = sock_pair
	loop.call_soon(write)

	fut = asyncio.Future()
	loop.add_reader(srv_sock.fileno(), can_read)
	notifier = loop._read_notifiers[srv_sock.fileno()]
	loop.run_until_complete(asyncio.wait_for(fut, timeout=1.0))


def test_add_writer_should_disable_qsocket_notifier_on_callback(loop, sock_pair):
	"""Verify that add_writer disables QSocketNotifier during callback."""
	def can_write():
		nonlocal num_calls
		num_calls += 1

		if num_calls == 2:
			# Since we get called again, the QSocketNotifier should've been re-enabled before
			# this call (although disabled during)
			assert not notifier.isEnabled()
			fut.set_result(None)
			client_sock.close()
			return

		assert not notifier.isEnabled()

	num_calls = 0
	client_sock, _ = sock_pair
	fut = asyncio.Future()
	loop.add_writer(client_sock.fileno(), can_write)
	notifier = loop._write_notifiers[client_sock.fileno()]
	loop.run_until_complete(asyncio.wait_for(fut, timeout=1.0))


def test_reader_writer_echo(loop, sock_pair):
	"""Verify readers and writers can send data to each other."""
	c_sock, s_sock = sock_pair

	@asyncio.coroutine
	def mycoro():
		c_reader, c_writer = yield from asyncio.open_connection(sock=c_sock)
		s_reader, s_writer = yield from asyncio.open_connection(sock=s_sock)

		data = b'Echo... Echo... Echo...'
		s_writer.write(data)
		yield from s_writer.drain()
		read_data = yield from c_reader.readexactly(len(data))
		assert data == read_data
		s_writer.close()

	loop.run_until_complete(asyncio.wait_for(mycoro(), timeout=1.0))


def test_regression_bug13(loop, sock_pair):
	"""Verify that a simple handshake between client and server works as expected."""
	c_sock, s_sock = sock_pair
	client_done, server_done = asyncio.Future(), asyncio.Future()

	@asyncio.coroutine
	def server_coro():
		s_reader, s_writer = yield from asyncio.open_connection(sock=s_sock)

		s_writer.write(b'1')
		yield from s_writer.drain()
		assert (yield from s_reader.readexactly(1)) == b'2'
		s_writer.write(b'3')
		yield from s_writer.drain()
		server_done.set_result(True)

	result1 = None
	result3 = None

	@asyncio.coroutine
	def client_coro():
		def cb1():
			nonlocal result1
			assert result1 is None
			loop.remove_reader(c_sock.fileno())
			result1 = c_sock.recv(1)
			loop.add_writer(c_sock.fileno(), cb2)

		def cb2():
			nonlocal result3
			assert result3 is None
			c_sock.send(b'2')
			loop.remove_writer(c_sock.fileno())
			loop.add_reader(c_sock.fileno(), cb3)

		def cb3():
			nonlocal result3
			assert result3 is None
			result3 = c_sock.recv(1)
			client_done.set_result(True)

		loop.add_reader(c_sock.fileno(), cb1)

	asyncio.async(client_coro())
	asyncio.async(server_coro())

	both_done = asyncio.gather(client_done, server_done)
	loop.run_until_complete(asyncio.wait_for(both_done, timeout=1.0))
	assert result1 == b'1'
	assert result3 == b'3'


def test_add_reader_replace(loop, sock_pair):
	c_sock, s_sock = sock_pair
	callback_invoked = asyncio.Future()

	called1 = False
	called2 = False

	def any_callback():
		if not callback_invoked.done():
			callback_invoked.set_result(True)
		loop.remove_reader(c_sock.fileno())

	def callback1():
		# the "bad" callback: if this gets invoked, something went wrong
		nonlocal called1
		called1 = True
		any_callback()

	def callback2():
		# the "good" callback: this is the one which should get called
		nonlocal called2
		called2 = True
		any_callback()

	@asyncio.coroutine
	def server_coro():
		s_reader, s_writer = yield from asyncio.open_connection(
			sock=s_sock)
		s_writer.write(b"foo")
		yield from s_writer.drain()

	@asyncio.coroutine
	def client_coro():
		loop.add_reader(c_sock.fileno(), callback1)
		loop.add_reader(c_sock.fileno(), callback2)
		yield from callback_invoked
		loop.remove_reader(c_sock.fileno())
		assert (yield from loop.sock_recv(c_sock, 3)) == b"foo"

	client_done = asyncio.async(client_coro())
	server_done = asyncio.async(server_coro())

	both_done = asyncio.wait(
		[server_done, client_done],
		return_when=asyncio.FIRST_EXCEPTION)
	loop.run_until_complete(asyncio.wait_for(both_done, timeout=0.1))
	assert not called1
	assert called2


def test_add_writer_replace(loop, sock_pair):
	c_sock, s_sock = sock_pair
	callback_invoked = asyncio.Future()

	called1 = False
	called2 = False

	def any_callback():
		if not callback_invoked.done():
			callback_invoked.set_result(True)
		loop.remove_writer(c_sock.fileno())

	def callback1():
		# the "bad" callback: if this gets invoked, something went wrong
		nonlocal called1
		called1 = True
		any_callback()

	def callback2():
		# the "good" callback: this is the one which should get called
		nonlocal called2
		called2 = True
		any_callback()

	@asyncio.coroutine
	def client_coro():
		loop.add_writer(c_sock.fileno(), callback1)
		loop.add_writer(c_sock.fileno(), callback2)
		yield from callback_invoked
		loop.remove_writer(c_sock.fileno())

	loop.run_until_complete(asyncio.wait_for(client_coro(), timeout=0.1))
	assert not called1
	assert called2


def test_remove_reader_idempotence(loop, sock_pair):
	fd = sock_pair[0].fileno()

	def cb():
		pass

	removed0 = loop.remove_reader(fd)
	loop.add_reader(fd, cb)
	removed1 = loop.remove_reader(fd)
	removed2 = loop.remove_reader(fd)

	assert not removed0
	assert removed1
	assert not removed2


def test_remove_writer_idempotence(loop, sock_pair):
	fd = sock_pair[0].fileno()

	def cb():
		pass

	removed0 = loop.remove_writer(fd)
	loop.add_writer(fd, cb)
	removed1 = loop.remove_writer(fd)
	removed2 = loop.remove_writer(fd)

	assert not removed0
	assert removed1
	assert not removed2


def test_scheduling(loop, sock_pair):
	s1, s2 = sock_pair
	fd = s1.fileno()
	cb_called = asyncio.Future()

	def writer_cb(fut):
		if fut.done():
			cb_called.set_exception(
				ValueError("writer_cb called twice")
			)
		fut.set_result(None)

	def fut_cb(fut):
		loop.remove_writer(fd)
		cb_called.set_result(None)

	fut = asyncio.Future()
	fut.add_done_callback(fut_cb)
	loop.add_writer(fd, writer_cb, fut)
	loop.run_until_complete(cb_called)
