"""One cancellable HTTP pool behind the synchronous driver boundary.

Only network I/O runs on this loop. Journal transactions and lifecycle locks stay
in the calling thread. Cancelling I/O does not imply rollback on the remote node.
"""

import asyncio
import threading
from http.cookiejar import CookieJar, DefaultCookiePolicy
from contextlib import asynccontextmanager

import httpx

from orchestrator.application.execution import remaining
from orchestrator.application.telemetry import Reason, Stage, span


class NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        # Agents use per-request Bearer credentials; never persist upstream cookies.
        return False


class QueueFull(httpx.PoolTimeout):
    pass


class HTTPPool:
    def __init__(self, policy, transport=None):
        self.policy = policy
        self.transport = transport
        self._lock = threading.RLock()
        self._pending_nodes = {}
        self._pending = threading.BoundedSemaphore(policy.max_pending_requests)
        self._loop = None
        self._thread = None
        self._closed = False
        self._client = None
        self._global = None
        self._nodes = {}
        self._tasks = set()

    def _serve(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor(timeout=1))
            self._loop.close()

    def run(self, node_id, call):
        # Check before starting the loop or scheduling any remote work.
        remaining()
        with self._lock:
            if self._closed:
                raise httpx.TransportError("HTTP pool is closed")
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._serve, name="node-http", daemon=True)
                self._thread.start()
            if self._pending_nodes.get(node_id, 0) >= self.policy.max_pending_per_node:
                raise QueueFull("Agent per-node pending limit exceeded")
            if not self._pending.acquire(blocking=False):
                raise QueueFull("Agent pending limit exceeded")
            self._pending_nodes[node_id] = self._pending_nodes.get(node_id, 0) + 1
            try:
                future = asyncio.run_coroutine_threadsafe(self._execute(node_id, call), self._loop)
            except BaseException:
                self._release_pending(node_id)
                raise
        future.add_done_callback(lambda _: self._release_pending(node_id))
        # The coroutine owns cancellation and releases the response and permits
        # before this returns. No abandoned mutation thread or automatic retry.
        return future.result()

    def _release_pending(self, node_id):
        with self._lock:
            self._pending.release()
            self._pending_nodes[node_id] -= 1
            if not self._pending_nodes[node_id]:
                del self._pending_nodes[node_id]

    async def _execute(self, node_id, call):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    transport=self.transport,
                    cookies=CookieJar(policy=NoCookies()),
                    timeout=httpx.Timeout(
                        self.policy.response_timeout_seconds,
                        connect=self.policy.connect_timeout_seconds,
                    ),
                    limits=httpx.Limits(
                        max_connections=self.policy.max_concurrent_requests,
                        max_keepalive_connections=self.policy.max_concurrent_requests,
                    ),
                    follow_redirects=False,
                    trust_env=False,
                )
                self._global = asyncio.Semaphore(self.policy.max_concurrent_requests)
            timeout = remaining()
            try:
                async with asyncio.timeout(timeout):
                    async with self._admit(node_id):
                        # This is a total request limit, including a trickling body,
                        # in addition to HTTPX's per-phase inactivity timeouts.
                        async with asyncio.timeout(self.policy.response_timeout_seconds):
                            return await call(self._client)
            except TimeoutError:
                # Distinguish the enclosing operation deadline from request expiry.
                remaining()
                raise httpx.ReadTimeout("Agent request deadline exceeded") from None
        finally:
            self._tasks.discard(task)

    @asynccontextmanager
    async def _admit(self, node_id):
        entry = self._nodes.setdefault(
            node_id, [asyncio.Semaphore(self.policy.max_concurrent_per_node), 0]
        )
        entry[1] += 1
        local = global_slot = False
        try:
            with span(Stage.QUEUE, node_id=node_id) as observation:
                try:
                    async with asyncio.timeout(self.policy.queue_timeout_seconds):
                        # Waiting for a busy node must not occupy a global slot.
                        await entry[0].acquire()
                        local = True
                        await self._global.acquire()
                        global_slot = True
                except TimeoutError:
                    observation.reason = Reason.SATURATED
                    raise QueueFull("Agent queue deadline exceeded") from None
            yield
        finally:
            if global_slot:
                self._global.release()
            if local:
                entry[0].release()
            entry[1] -= 1
            if not entry[1]:
                del self._nodes[node_id]

    async def _shutdown(self):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._loop is None:
                return
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            future.result()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
