# Copyright 2018 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

import functools
import multiprocessing

from portage.util._async.AsyncTaskFuture import AsyncTaskFuture
from portage.util._async.TaskScheduler import TaskScheduler
from portage.util._eventloop.global_event_loop import global_event_loop
from portage.util.futures import asyncio


def iter_completed(futures, max_jobs=None, max_load=None, loop=None):
	"""
	This is similar to asyncio.as_completed, but takes an iterator of
	futures as input, and includes support for max_jobs and max_load
	parameters.

	@param futures: iterator of asyncio.Future (or compatible)
	@type futures: iterator
	@param max_jobs: max number of futures to process concurrently (default
		is multiprocessing.cpu_count())
	@type max_jobs: int
	@param max_load: max load allowed when scheduling a new future,
		otherwise schedule no more than 1 future at a time (default
		is multiprocessing.cpu_count())
	@type max_load: int or float
	@param loop: event loop
	@type loop: EventLoop
	@return: iterator of futures that are done
	@rtype: iterator
	"""
	loop = loop or global_event_loop()
	loop = getattr(loop, '_asyncio_wrapper', loop)

	for future_done_set in async_iter_completed(futures,
		max_jobs=max_jobs, max_load=max_load, loop=loop):
		for future in loop.run_until_complete(future_done_set):
			yield future


def async_iter_completed(futures, max_jobs=None, max_load=None, loop=None):
	"""
	An asynchronous version of iter_completed. This yields futures, which
	when done, result in a set of input futures that are done. This serves
	as a wrapper around portage's internal TaskScheduler class, using
	standard asyncio interfaces.

	@param futures: iterator of asyncio.Future (or compatible)
	@type futures: iterator
	@param max_jobs: max number of futures to process concurrently (default
		is multiprocessing.cpu_count())
	@type max_jobs: int
	@param max_load: max load allowed when scheduling a new future,
		otherwise schedule no more than 1 future at a time (default
		is multiprocessing.cpu_count())
	@type max_load: int or float
	@param loop: event loop
	@type loop: EventLoop
	@return: iterator of futures, which when done, result in a set of
		input futures that are done
	@rtype: iterator
	"""
	loop = loop or global_event_loop()
	loop = getattr(loop, '_asyncio_wrapper', loop)

	max_jobs = max_jobs or multiprocessing.cpu_count()
	max_load = max_load or multiprocessing.cpu_count()

	future_map = {}
	def task_generator():
		for future in futures:
			future_map[id(future)] = future
			yield AsyncTaskFuture(future=future)

	scheduler = TaskScheduler(
		task_generator(),
		max_jobs=max_jobs,
		max_load=max_load,
		event_loop=loop._loop)

	def done_callback(future_done_set, wait_result):
		"""Propagate results from wait_result to future_done_set."""
		if future_done_set.cancelled():
			return
		done, pending = wait_result.result()
		for future in done:
			del future_map[id(future)]
		future_done_set.set_result(done)

	def cancel_callback(wait_result, future_done_set):
		"""Cancel wait_result if future_done_set has been cancelled."""
		if future_done_set.cancelled() and not wait_result.done():
			wait_result.cancel()

	try:
		scheduler.start()

		# scheduler should ensure that future_map is non-empty until
		# task_generator is exhausted
		while future_map:
			wait_result = asyncio.ensure_future(
				asyncio.wait(list(future_map.values()),
				return_when=asyncio.FIRST_COMPLETED, loop=loop), loop=loop)
			future_done_set = loop.create_future()
			future_done_set.add_done_callback(
				functools.partial(cancel_callback, wait_result))
			wait_result.add_done_callback(
				functools.partial(done_callback, future_done_set))
			yield future_done_set
	finally:
		# cleanup in case of interruption by SIGINT, etc
		scheduler.cancel()
		scheduler.wait()