"""Bounded extraction pool (T025): ThreadPoolExecutor with MAX_CONCURRENCY
workers. Its internal work queue is FIFO, so admission order == execution
order; the visible queue positions are recomputed from JobStore's queued list
as earlier jobs leave it.
"""

from concurrent.futures import Future, ThreadPoolExecutor


class WorkerPool:
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="extract"
        )

    def submit(self, fn, /, *args, **kwargs) -> Future:
        return self._executor.submit(fn, *args, **kwargs)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
