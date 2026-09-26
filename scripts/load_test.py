"""Repeatable load test for 10/25/50/100 queued jobs.

Measures avg/p95/p99 processing time, throughput, queue depth, failure/retry rates.
Uses the in-process pipeline with mocked Odoo and AI for deterministic results.
"""
from __future__ import annotations

import asyncio
import time
import statistics
from unittest.mock import MagicMock

from order_parser.core.job import JobRecord, JobStatus
from order_parser.core.job_queue import JobQueue
from order_parser.core.job_store import JobStore
from order_parser.models import ParsedOrder, OrderModel, CustomerModel, ItemModel, MetadataModel
from order_parser.services.pipeline import OrderPipeline

# Minimal deterministic pipeline without external calls
def _mock_odoo():
    mock = MagicMock()
    mock.enabled = False
    mock.create_partner.return_value = 1
    mock.create_sale_order.return_value = {"id": 1, "name": "SO0001"}
    return mock

def make_parsed(text: str, confidence: float = 96.0) -> ParsedOrder:
    order = OrderModel(
        customer=CustomerModel(name="Test Customer"),
        items=[ItemModel(product_name="Laptop", quantity=2, unit_price=1000, uom="Units")],
        metadata=MetadataModel(source="load_test", input_type="text", confidence=confidence),
    )
    return ParsedOrder(order=order, ai_response={"confidence": confidence}, extracted_text=text)


async def run_load(num_jobs: int, concurrency: int = 5) -> dict:
    job_store = JobStore(directory="/tmp/load_test_jobs")
    # clear
    import shutil, pathlib
    p = pathlib.Path("/tmp/load_test_jobs")
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)
    job_store = JobStore(directory=str(p))
    queue = JobQueue(max_workers=concurrency, max_queue_size=1000, job_store=job_store)
    await queue.start()
    pipeline = OrderPipeline(odoo=_mock_odoo())
    # temporarily make pipeline resolve no-op
    pipeline.resolver = None

    times: list[float] = []
    start_all = time.monotonic()

    async def _process(job: JobRecord, parsed: ParsedOrder):
        s = time.monotonic()
        result = await asyncio.to_thread(pipeline.process, "load_test", "text", parsed, {"text": parsed.extracted_text})
        elapsed = time.monotonic() - s
        times.append(elapsed)
        # update job store
        job.status = JobStatus.COMPLETED if result.get("status") == "success" else JobStatus.NEEDS_REVIEW
        job_store.save(job)

    # enqueue
    for i in range(num_jobs):
        job = JobRecord(source="load_test", input_type="text", status=JobStatus.QUEUED)
        job_store.save(job)
        parsed = make_parsed(f"Order {i}: 2 laptops for Test Customer", confidence=96)
        await queue.enqueue(job.job_id, _process, job, parsed)

    # wait for completion
    while queue.depth > 0:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)
    await queue.stop()

    total = time.monotonic() - start_all
    if times:
        avg = sum(times) / len(times)
        p95 = statistics.quantiles(times, n=20)[18] if len(times) >= 20 else max(times)
        p99 = max(times) if len(times) < 100 else sorted(times)[int(len(times)*0.99)]
        throughput = len(times) / total if total > 0 else 0
    else:
        avg = p95 = p99 = throughput = 0

    return {
        "jobs": num_jobs,
        "concurrency": concurrency,
        "avg_ms": round(avg*1000, 1),
        "p95_ms": round(p95*1000, 1),
        "p99_ms": round(p99*1000, 1),
        "throughput_per_sec": round(throughput, 2),
        "total_sec": round(total, 2),
        "queue_depth": queue.depth,
        "failed": sum(1 for j in job_store.list() if j.status == JobStatus.FAILED),
    }


async def main():
    for n in [10, 25, 50, 100]:
        result = await run_load(n, concurrency=5)
        print(f"{result['jobs']} jobs: avg={result['avg_ms']}ms p95={result['p95_ms']}ms p99={result['p99_ms']}ms throughput={result['throughput_per_sec']}/s total={result['total_sec']}s failed={result['failed']}")

if __name__ == "__main__":
    asyncio.run(main())
