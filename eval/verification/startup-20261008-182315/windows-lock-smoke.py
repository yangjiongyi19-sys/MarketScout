import asyncio
import tempfile
from pathlib import Path
from app.domain.buyer.preference import BuyerPreference
from app.infrastructure.persistence.json_file_stores import JsonFilePreferenceStore

async def main():
    with tempfile.TemporaryDirectory(prefix="globex-lock-smoke-") as root:
        store = JsonFilePreferenceStore(Path(root))
        await store.append(BuyerPreference(buyer_id="lock-smoke", kind="like", statement="文件锁烟雾检查"))
        items = await store.list_by_buyer("lock-smoke")
        assert [item.statement for item in items] == ["文件锁烟雾检查"]
        assert await store.delete("lock-smoke", "文件锁烟雾检查")
        print("Windows file-lock and isolated preference CRUD: passed")

asyncio.run(main())
