"""只读诊断：列出所有带"未核对"镜头的 Wan 任务图。

对应 service._assert_no_other_unresolved_graphs 的拦截条件。
不写库、不调用任何外部接口。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # 诊断脚本可裸跑：DSN 已从环境变量注入
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app import storage as st

KIND = "wan_storyboard_graph"
IMAGE_STUCK = {"sending", "unknown"}
VIDEO_STUCK = {"sending", "unknown", "waiting_provider"}


def main() -> int:
    rows = st.conn().execute(
        "SELECT id, work_id, data FROM media_jobs", []
    ).fetchall()

    by_work: dict[str, list] = {}
    for row in rows:
        graph = row.get("data")
        if isinstance(graph, str):
            graph = json.loads(graph)
        graph = graph or {}
        if graph.get("kind") != KIND:
            continue
        by_work.setdefault(graph.get("work_id") or row.get("work_id") or "?", []).append(graph)

    offenders = 0
    for work_id in sorted(by_work):
        graphs = by_work[work_id]
        if len(graphs) < 2:
            continue  # 只有一个任务图时不会被 _assert_no_other_unresolved_graphs 拦
        for graph in graphs:
            stuck_image = [s for s in (graph.get("shots") or [])
                           if s.get("image_state") in IMAGE_STUCK]
            stuck_video = [s for s in (graph.get("shots") or [])
                           if s.get("video_state") in VIDEO_STUCK]
            if not (stuck_image or stuck_video):
                continue
            offenders += 1
            print(f"\n作品 {work_id}")
            print(f"  任务图 {graph.get('id')}  state={graph.get('state')}  "
                  f"created={str(graph.get('created_at'))[:19]}")
            for s in stuck_image + stuck_video:
                tags = []
                if s.get("image_state") in IMAGE_STUCK:
                    tags.append(f"图片={s['image_state']}")
                if s.get("video_state") in VIDEO_STUCK:
                    tags.append(f"视频={s['video_state']}")
                print(f"    镜头 {s.get('shot_index'):>3}  {' '.join(tags)}  "
                      f"图片作业={s.get('image_job_id') or '-'}  "
                      f"视频作业={s.get('video_job_id') or '-'}")
                if s.get("error"):
                    print(f"          错误：{s['error'].get('code')} "
                          f"{s['error'].get('message')}")
        if offenders:
            print(f"  ↑ 该作品有 {len(graphs)} 张任务图，上面列出的每一张都会拦住新的图片生成")

    if not offenders:
        print("没有发现卡住的镜头：当前不存在被 _assert_no_other_unresolved_graphs 拦下的任务图。")
    else:
        print(f"\n合计 {offenders} 张任务图带有未核对作业。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
