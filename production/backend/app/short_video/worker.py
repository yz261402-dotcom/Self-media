"""Durable local Worker for ordered native-audio storyboard rendering."""
from __future__ import annotations

import argparse
import os
import time
import uuid
from urllib.parse import quote

from .. import jobs, storage as st
from ..contracts import now_iso
from . import media, service


def run_stitch(graph_id: str, *, worker_id: str | None = None) -> dict:
    worker_id = worker_id or f"wan-storyboard-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    graph = service.get_graph(graph_id)
    if graph.get("state") == "succeeded":
        return service.project(graph)
    service.assert_current(graph)
    if (graph.get("clip_approval") or {}).get("clip_digest") != service._clip_digest(graph):
        raise service.StoryboardError("STORYBOARD_CLIP_REVIEW_REQUIRED", "原片审核已变化，停止拼接")
    job = jobs.get_job(graph.get("stitch_job_id") or "")
    if not job:
        raise service.StoryboardError("STORYBOARD_STITCH_JOB_REQUIRED", "请先创建拼接作业")
    if job["state"] == "succeeded":
        return service.project(service._mutate(graph_id, lambda g: g.update(state="succeeded",
            output=job["result"]["output"], video_id=job["result"]["video_id"])))
    if job["state"] == "failed":
        raise service.StoryboardError("STORYBOARD_STITCH_FAILED", "原拼接作业失败，请检查镜头与 Worker 日志")
    # Render (1800s) and full decode (900s) run serially; allow probe/hash/write overhead.
    jobs.acquire_lease(job["id"], worker_id, ttl_seconds=3600)
    service._mutate(graph_id, lambda g: g.update(state="stitching"))
    try:
        output = media.stitch(graph)
        output["preview_url"] = "/api/v1/media?uri=" + quote(output["storage_uri"], safe="/")
        video = {"id": st.new_id("storyboard_video"), "work_id": graph["work_id"], "kind": "storyboard_final",
                 "route": "WAN_3_0", "provider": "wan_storyboard", "model": "wan3.0-video",
                 "graph_id": graph_id, "script_version_id": graph["script_version_id"],
                 "script_digest": graph["script_digest"], "generation_mode": "storyboard_native_audio",
                 "approval_state": "pending", "is_mock": False, "created_at": now_iso(), **output}
        # Check again after FFmpeg; changed content retains the output as historical evidence.
        service.assert_current(service.get_graph(graph_id))
        st.put("videos", video["id"], video, {"work_id": graph["work_id"]})
        jobs.complete(job["id"], worker_id, {"output": output, "video_id": video["id"]})
        return service.project(service._mutate(graph_id, lambda g: g.update(state="succeeded", output=output, video_id=video["id"])))
    except Exception as error:
        jobs.fail(job["id"], worker_id, {"code": getattr(error, "code", "STORYBOARD_STITCH_FAILED"),
                                            "message": "本地拼接失败，原片与临时结果已保留"})
        service._mutate(graph_id, lambda g: g.update(state="stitch_failed",
            error={"code": getattr(error, "code", "STORYBOARD_STITCH_FAILED"), "message": "本地拼接失败，原片已保留"}))
        raise


def run_queued(*, worker_id: str | None = None) -> list[dict]:
    results = []
    for graph in st.all_rows("media_jobs"):
        if graph.get("kind") != service.KIND or graph.get("state") not in ("stitch_queued", "stitching"):
            continue
        try:
            results.append(run_stitch(graph["id"], worker_id=worker_id))
        except service.StoryboardError as error:
            # A queued graph can become stale after the user changes the
            # selected script, route, or identity.  Leaving its job queued
            # makes the persistent worker look dead forever and causes the
            # UI's 60-second timeout to hide the actual reason.
            job = jobs.get_job(graph.get("stitch_job_id") or "")
            if job and job.get("state") == "queued":
                jobs.cancel_unsent_call(job["id"])
                service._mutate(graph["id"], lambda current: current.update(
                    state="stitch_failed",
                    error={"code": error.code, "message": error.message},
                ))
            continue
        except jobs.JobRejected:
            continue
        except Exception:
            current = service.get_graph(graph["id"])
            job = jobs.get_job(current.get("stitch_job_id") or "")
            if current.get("state") != "stitch_failed" or not job or job.get("state") != "failed":
                raise
            # A recorded per-job failure must not stop the persistent queue worker.
            continue
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run queued Wan storyboard stitching jobs")
    parser.add_argument("--graph-id", help="Run one queued graph; default scans queued graphs")
    parser.add_argument("--worker-id", default=f"wan-storyboard-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--loop", action="store_true", help="Keep processing queued jobs")
    parser.add_argument("--interval-seconds", type=float, default=3.0)
    args = parser.parse_args()
    if args.interval_seconds <= 0 or args.interval_seconds > 60:
        parser.error("--interval-seconds must be between 0 and 60")
    st.init_db()
    while True:
        results = ([run_stitch(args.graph_id, worker_id=args.worker_id)] if args.graph_id else
                   run_queued(worker_id=args.worker_id))
        for result in results:
            print(f"{result['id']}: {result['state']}", flush=True)
        if not args.loop or args.graph_id:
            break
        time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
