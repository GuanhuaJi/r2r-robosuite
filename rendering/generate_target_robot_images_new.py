#!/usr/bin/env python3
# file: generate_target_robot_images_mp.py
import argparse, json, logging, os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import os, argparse, json, logging, multiprocessing as mp

import numpy as np

logger = logging.getLogger(__name__)

from core import pick_best_gpu, locked_json
from config.dataset_poses_dict import ROBOT_CAMERA_POSES_DICT
from config.robot_pose_dict import ROBOT_POSE_DICT


# ───────────────────────────── helpers ──────────────────────────────
def select_gripper(robot: str) -> str:
    """Return the proper gripper name for a given robot type."""
    if robot == "Sawyer":
        return "RethinkGripper"
    if robot == "Jaco":
        return "JacoThreeFingerGripper"
    if robot in {"IIWA", "UR5e", "Kinova3"}:
        return "Robotiq85Gripper"
    if robot == "Panda":
        return "PandaGripper"
    raise ValueError(f"Unknown robot {robot!r}")


def generate_one_episode(
    robot_dataset: str,
    robot: str,
    episode: int,
    camera_hw: tuple[int, int],
    out_root: str | os.PathLike,
    unlimited: bool = False,
    load_displacement: bool = False,
) -> tuple[str, int, bool]:
    pick_best_gpu()
    os.environ["MUJOCO_GL"] = "egl"
    from envs import TargetEnvWrapper
    H, W = camera_hw
    gripper = select_gripper(robot)
    try:
        wrapper = TargetEnvWrapper(
            robot,
            gripper,
            robot_dataset,
            camera_height=H,
            camera_width=W,
        )

        if load_displacement:
            off_file = Path(out_root) / "source_robot_states" / robot / "offsets" / f"{episode}.npy"
            if off_file.is_file():
                displacement = np.load(off_file)
            else:
                displacement = np.zeros(3, dtype=np.float32)
                print(f"WARNING: displacement file not found → {off_file}; using default [0, 0, 0].")
        else:
            displacement = ROBOT_POSE_DICT[robot_dataset][robot]

        logger.info(
            "Episode %d robot %s initial displacement %s",
            episode,
            robot,
            displacement,
        )

        step = 0.05
        max_iter = 5
        for _ in range(max_iter):
            candidates = [
                displacement,
                displacement + np.array([step, 0, 0]),
                displacement - np.array([step, 0, 0]),
                displacement + np.array([0, step, 0]),
                displacement - np.array([0, step, 0]),
                displacement + np.array([0, 0, step]),
                displacement - np.array([0, 0, step]),
            ]

            best_steps = -1
            best_disp = displacement
            for cand in candidates:
                ok, _sug, steps = wrapper.generate_image(
                    save_paired_images_folder_path=out_root,
                    source_robot_states_path=out_root,
                    robot_dataset=robot_dataset,
                    robot_disp=cand,
                    episode=episode,
                    unlimited=unlimited,
                    dry_run=True,
                )
                logger.debug(
                    "Episode %d robot %s test disp %s -> ok=%s steps=%s",
                    episode,
                    robot,
                    cand,
                    ok,
                    steps,
                )
                if ok:
                    displacement = cand
                    best_disp = cand
                    best_steps = steps
                    break
                if steps > best_steps:
                    best_steps = steps
                    best_disp = cand
            else:
                displacement = best_disp
                step *= 0.5
                continue
            break

        success, _sug, _ = wrapper.generate_image(
            save_paired_images_folder_path=out_root,
            source_robot_states_path=out_root,
            robot_dataset=robot_dataset,
            robot_disp=displacement,
            episode=episode,
            unlimited=unlimited,
            dry_run=False,
        )

        return robot, episode, bool(success)

    except Exception as exc:
        logging.exception(
            "Episode %s (robot=%s) failed: %s", episode, robot, exc, exc_info=True
        )
        return robot, episode, False


# ───────────────────────────── dispatcher ────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--robot_dataset", required=True)
    p.add_argument("--target_robot", nargs="+", required=True)
    p.add_argument("--num_workers", type=int, default=8)
    p.add_argument("--unlimited", action="store_true")
    p.add_argument("--load_displacement", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(message)s",
    )

    meta = ROBOT_CAMERA_POSES_DICT[args.robot_dataset]
    with open(Path(meta["replay_path"]) / "dataset_metadata.json", encoding="utf-8") as f:
        dmeta = json.load(f)
    H, W = dmeta["image_height"], dmeta["image_width"]
    out_root = Path(meta["replay_path"])
    num_eps = dmeta["num_episodes"]
    episodes = range(num_eps)

    mp_ctx = mp.get_context("spawn")

    tasks = []
    for robot in args.target_robot:
        wl_path = out_root / robot / "whitelist.json"
        wl_path.parent.mkdir(parents=True, exist_ok=True)
        with locked_json(wl_path) as wl:
            done_eps = set(wl.get(robot, []))

        for ep in episodes:
            if ep not in done_eps:
                tasks.append(
                    (
                        args.robot_dataset,
                        robot,
                        ep,
                        (H, W),
                        str(out_root),
                        args.unlimited,
                        args.load_displacement,
                    )
                )

    if not tasks:
        print("Nothing to do – all episodes already processed.")
        return

    with ProcessPoolExecutor(max_workers=args.num_workers, mp_context=mp_ctx) as pool:
        futures = [pool.submit(generate_one_episode, *t) for t in tasks]
        for fut in as_completed(futures):
            robot, ep, ok = fut.result()
            if not ok:
                continue
            wl_path = out_root / robot / "whitelist.json"
            with locked_json(wl_path) as wl:
                wl.setdefault(robot, []).append(ep)

    print("✓ all dispatched episodes finished")

'''
python /home/guanhuaji/mirage/robot2robot/rendering/generate_target_robot_images_new.py --robot_dataset austin_buds --target_robot Sawyer --num_workers 10 --load_displacement
'''

if __name__ == "__main__":
    main()
