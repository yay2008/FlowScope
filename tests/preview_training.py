"""训练页面离线验收：临时数据目录，不启动或修改实时行情、模拟交易账户。"""
import argparse
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from training import TrainingService
from training_api import TrainingDownloads, make_training_router
from training_data import demo_dataset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--duration", type=int, default=900)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="flowscope-training-") as directory:
        service = TrainingService(lambda: directory)
        service.datasets.save(*demo_dataset())
        app = FastAPI()

        def offline(*_):
            raise ValueError("离线预览不连接天勤，请使用 CSV 或演示数据")

        app.include_router(make_training_router(service, TrainingDownloads(service.datasets, offline)))
        app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[1] / "static", html=True))
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
        timer = threading.Timer(args.duration, lambda: setattr(server, "should_exit", True))
        timer.daemon = True
        timer.start()
        print(f"Offline training preview: http://127.0.0.1:{args.port}/training.html", flush=True)
        try:
            server.run()
        finally:
            timer.cancel()


if __name__ == "__main__":
    main()
