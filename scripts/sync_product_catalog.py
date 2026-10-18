# -*- coding: utf-8 -*-
"""Amazon 商品目录同步命令。初次手动运行 --once，常驻调度使用 --schedule。"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from app.application.usecases.rainforest_catalog_sync import RainforestProductCatalogSync, load_sync_scope
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.rainforest_api import RainforestApiProvider
from app.infrastructure.settings import PROJECT_ROOT, load_settings
from app.infrastructure.vector.qdrant_amazon_catalog import QdrantAmazonCatalogIndex

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
logger = logging.getLogger("product_sync")


@contextmanager
def _single_instance_lock(data_dir):
    """以跨平台文件锁保证同步命令和常驻调度最多只有一个实例。"""
    lock_path = data_dir / "product-sync.lock"
    with lock_path.open("a+b") as stream:
        if os.name == "nt":
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("商品同步进程已在运行") from error
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("商品同步进程已在运行") from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _next_run(now: datetime | None = None) -> datetime:
    local_now = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI)
    target = datetime.combine(local_now.date(), time(hour=3), tzinfo=SHANGHAI)
    if target <= local_now:
        target += timedelta(days=1)
    return target


async def _build_sync_service(settings):
    provider = RainforestApiProvider(
        api_key=settings.rainforest_api_key,
        amazon_domain=settings.rainforest_amazon_domain,
        associate_id=settings.rainforest_associate_id,
    )
    index = QdrantAmazonCatalogIndex(settings)
    embedder = OpenAIEmbeddingClient(settings)
    scope = load_sync_scope(PROJECT_ROOT / "configs" / "product_sync.yaml")
    return settings, provider, index, RainforestProductCatalogSync(provider, index, embedder, scope)


async def _run_once(settings) -> dict:
    _, provider, index, syncer = await _build_sync_service(settings)
    try:
        return await syncer.sync_once()
    finally:
        await provider.aclose()
        await index.close()


async def _schedule_daily(settings) -> None:
    _, provider, index, syncer = await _build_sync_service(settings)
    logger.info("Amazon 商品同步调度已启动，每天 03:00（Asia/Shanghai）刷新索引 %s", index.alias)
    try:
        while True:
            target = _next_run()
            delay = max(1, (target - datetime.now(SHANGHAI)).total_seconds())
            logger.info("下一次 Amazon 商品同步时间：%s", target.isoformat())
            await asyncio.sleep(delay)
            try:
                report = await syncer.sync_once()
                logger.info("Amazon 商品每日同步报告：%s", json.dumps(report, ensure_ascii=False))
            except Exception as error:  # noqa: BLE001
                # 保留活动索引，下一天继续；失败不泄露请求 URL 或凭据。
                logger.error("Amazon 商品每日同步失败：%s: %s", type(error).__name__, error)
    finally:
        await provider.aclose()
        await index.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="从 Rainforest 同步 Amazon 搜索结果并更新 Qdrant RAG 索引")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="立即同步一次（初次建库和手工刷新）")
    group.add_argument("--schedule", action="store_true", help="每天 03:00 Asia/Shanghai 执行一次")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings(require_llm_key=False)
        with _single_instance_lock(settings.data_dir):
            if args.schedule:
                asyncio.run(_schedule_daily(settings))
                return 0
            report = asyncio.run(_run_once(settings))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
    except Exception as error:  # noqa: BLE001
        logger.error("Amazon 商品同步未完成：%s: %s", type(error).__name__, error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
