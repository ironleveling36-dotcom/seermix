
import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("seedr-pcloud")

BOT_TOKEN = os.environ["BOT_TOKEN"]
SEEDR_EMAIL = os.environ["SEEDR_EMAIL"]
SEEDR_PASSWORD = os.environ["SEEDR_PASSWORD"]
PCLOUD_EMAIL = os.environ["PCLOUD_EMAIL"]
PCLOUD_PASSWORD = os.environ["PCLOUD_PASSWORD"]

PCLOUD_API_HOST = os.getenv("PCLOUD_API_HOST", "https://api.pcloud.com").rstrip("/")
PCLOUD_FOLDER_PATH = os.getenv("PCLOUD_FOLDER_PATH", "/Movies")
DELETE_ALL_SEEDR_FILES_AFTER_UPLOAD = os.getenv(
    "DELETE_ALL_SEEDR_FILES_AFTER_UPLOAD", "true"
).lower() in {"1", "true", "yes", "on"}

POLL_SECONDS = max(5, int(os.getenv("POLL_SECONDS", "15")))
TIMEOUT_MINUTES = max(1, int(os.getenv("TRANSFER_TIMEOUT_MINUTES", "180")))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/seedr-downloads"))
MAX_FILE_SIZE_GB = float(os.getenv("MAX_FILE_SIZE_GB", "50"))

SEEDR_BASE = "https://www.seedr.cc/rest"
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm"}
MAGNET_RE = re.compile(r"^magnet:\?[^\s]+$", re.I)


def esc(x):
    return html.escape(str(x))


def valid_magnet(text):
    if not text:
        return False
    x = text.strip().split()[0]
    return bool(MAGNET_RE.match(x) and "xt=" in x.lower())


def human_size(n):
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.2f} {unit}"
        x /= 1024


class Seedr:
    def __init__(self):
        self.http = httpx.AsyncClient(
            auth=(SEEDR_EMAIL, SEEDR_PASSWORD),
            follow_redirects=True,
            timeout=60,
        )

    async def close(self):
        await self.http.aclose()

    async def get(self, endpoint):
        r = await self.http.get(f"{SEEDR_BASE}/{endpoint}")
        r.raise_for_status()
        return r.json()

    async def post(self, endpoint, **kwargs):
        r = await self.http.post(f"{SEEDR_BASE}/{endpoint}", **kwargs)
        r.raise_for_status()
        return r.json()

    async def add_magnet(self, magnet):
        return await self.post("transfer/magnet", data={"magnet": magnet})

    async def transfer(self, tid):
        return await self.get(f"transfer/{tid}")

    async def root(self):
        return await self.get("folder")

    async def folder(self, fid):
        return await self.get(f"folder/{fid}")

    async def download(self, fid, destination):
        async with self.http.stream(
            "GET", f"{SEEDR_BASE}/file/{fid}", timeout=None
        ) as r:
            r.raise_for_status()
            with destination.open("wb") as out:
                async for chunk in r.aiter_bytes(1024 * 1024):
                    out.write(chunk)

    async def delete_file(self, fid):
        return await self.post(f"file/{fid}/delete")

    async def delete_folder(self, fid):
        return await self.post(f"folder/{fid}/delete")


class PCloud:
    def __init__(self):
        self.http = httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            headers={"User-Agent": "seedr-pcloud-telegram-bot/1.0"},
        )
        self.auth = None

    async def close(self):
        await self.http.aclose()

    async def login_once(self):
        r = await self.http.get(
            f"{PCLOUD_API_HOST}/userinfo",
            params={
                "username": PCLOUD_EMAIL,
                "password": PCLOUD_PASSWORD,
                "getauth": 1,
                "logout": 1,
                "authexpire": 31536000,
                "authinactiveexpire": 2678400,
            },
        )
        r.raise_for_status()
        data = r.json()
        if data.get("result") != 0 or not data.get("auth"):
            raise RuntimeError(f"pCloud login failed: {data}")
        self.auth = data["auth"]

    def params(self, **kwargs):
        return {"auth": self.auth, **kwargs}

    async def ensure_folder(self, path):
        path = "/" + "/".join(p for p in path.split("/") if p)
        if path == "/":
            return 0

        parent = 0
        for name in path.strip("/").split("/"):
            r = await self.http.get(
                f"{PCLOUD_API_HOST}/listfolder",
                params=self.params(folderid=parent),
            )
            r.raise_for_status()
            data = r.json()
            if data.get("result") != 0:
                raise RuntimeError(f"pCloud listfolder failed: {data}")

            found = next(
                (x for x in data.get("metadata", {}).get("contents", [])
                 if x.get("isfolder") and x.get("name") == name),
                None,
            )
            if found:
                parent = int(found["folderid"])
                continue

            r = await self.http.get(
                f"{PCLOUD_API_HOST}/createfolder",
                params=self.params(folderid=parent, name=name),
            )
            r.raise_for_status()
            data = r.json()
            if data.get("result") != 0:
                raise RuntimeError(f"pCloud createfolder failed: {data}")
            parent = int(data["metadata"]["folderid"])

        return parent

    async def upload(self, path, folder_id):
        with path.open("rb") as fh:
            r = await self.http.post(
                f"{PCLOUD_API_HOST}/uploadfile",
                params=self.params(
                    folderid=folder_id,
                    filename=path.name,
                    nopartial=1,
                ),
                files={"file": (path.name, fh, "application/octet-stream")},
            )
        r.raise_for_status()
        data = r.json()
        if data.get("result") != 0:
            raise RuntimeError(f"pCloud upload failed: {data}")
        return data


def first_dict(*items):
    return next((x for x in items if isinstance(x, dict)), {})


def transfer_id(data):
    for obj in (data, data.get("transfer"), data.get("data")):
        if isinstance(obj, dict):
            for key in ("id", "transfer_id", "transferId"):
                if obj.get(key) is not None:
                    return str(obj[key])


def progress(data):
    for obj in (data, data.get("transfer"), data.get("data")):
        if isinstance(obj, dict):
            for key in ("progress", "percentage", "percent"):
                if isinstance(obj.get(key), (int, float)):
                    return float(obj[key])
    return 0


def finished(data):
    p = progress(data)
    if p >= 100:
        return True
    for obj in (data, data.get("transfer"), data.get("data")):
        if isinstance(obj, dict):
            s = str(obj.get("status") or obj.get("state") or "").lower()
            if s in {"complete", "completed", "finished", "done", "seeding"}:
                return True
    return False


def items(data):
    if isinstance(data, dict):
        for key in ("files", "folders", "items"):
            if isinstance(data.get(key), list):
                yield from data[key]


async def find_video(seedr, folder_id=None):
    data = await (seedr.root() if folder_id is None else seedr.folder(folder_id))
    for item in items(data):
        if not isinstance(item, dict):
            continue
        iid = item.get("id") or item.get("file_id")
        name = str(item.get("name") or item.get("filename") or "")
        is_folder = bool(item.get("isfolder") or item.get("is_folder") or item.get("folder"))
        if not is_folder and iid and Path(name).suffix.lower() in VIDEO_EXTENSIONS:
            return {"id": str(iid), "name": name}
        if is_folder and iid:
            found = await find_video(seedr, str(iid))
            if found:
                return found


async def collect_all(seedr, folder_id=None):
    data = await (seedr.root() if folder_id is None else seedr.folder(folder_id))
    files, folders = [], []
    for item in items(data):
        if not isinstance(item, dict):
            continue
        iid = item.get("id") or item.get("file_id")
        if not iid:
            continue
        is_folder = bool(item.get("isfolder") or item.get("is_folder") or item.get("folder"))
        if is_folder:
            folders.append(str(iid))
            f, d = await collect_all(seedr, str(iid))
            files.extend(f)
            folders.extend(d)
        else:
            files.append(str(iid))
    return files, folders


async def delete_everything(seedr):
    files, folders = await collect_all(seedr)
    deleted_files = 0
    deleted_folders = 0
    for fid in files:
        try:
            await seedr.delete_file(fid)
            deleted_files += 1
        except Exception as e:
            log.warning("Seedr file delete failed %s: %s", fid, e)
    for fid in reversed(folders):
        try:
            await seedr.delete_folder(fid)
            deleted_folders += 1
        except Exception as e:
            log.warning("Seedr folder delete failed %s: %s", fid, e)
    return deleted_files, deleted_folders


async def set_progress(msg, text):
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def handle_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    magnet = (message.text or "").strip().split()[0]

    if not valid_magnet(magnet):
        await message.reply_text("❌ Invalid magnet URI.")
        return

    progress_msg = await message.reply_text(
        "🔄 <b>Starting</b>\n\n⏳ Validating magnet…",
        parse_mode=ParseMode.HTML,
    )

    seedr = Seedr()
    pcloud = PCloud()
    workdir = Path(tempfile.mkdtemp(prefix="seedr-job-", dir=str(DOWNLOAD_DIR)))

    try:
        await set_progress(
            progress_msg,
            "🔄 <b>Starting</b>\n\n"
            "✅ Magnet validated\n"
            "⏳ Connecting to Seedr…",
        )

        added = await seedr.add_magnet(magnet)
        tid = transfer_id(added)
        if not tid:
            raise RuntimeError("Seedr did not return a transfer ID.")

        await set_progress(
            progress_msg,
            "🔄 <b>Seedr</b>\n\n"
            "✅ Magnet validated\n"
            "✅ Seedr connected\n"
            "✅ Magnet submitted\n"
            "⏳ Download: <b>0%</b>",
        )

        deadline = asyncio.get_running_loop().time() + TIMEOUT_MINUTES * 60
        last = -1
        while asyncio.get_running_loop().time() < deadline:
            status = await seedr.transfer(tid)
            pct = int(max(0, min(100, progress(status))))
            if pct != last:
                await set_progress(
                    progress_msg,
                    "🔄 <b>Seedr download</b>\n\n"
                    "✅ Magnet submitted\n"
                    f"⏳ Progress: <b>{pct}%</b>",
                )
                last = pct
            if finished(status):
                break
            await asyncio.sleep(POLL_SECONDS)
        else:
            raise TimeoutError("Seedr download timeout.")

        video = await find_video(seedr)
        if not video:
            raise RuntimeError("Seedr completed but no video file was found.")

        local = workdir / Path(video["name"]).name
        await set_progress(
            progress_msg,
            "🔄 <b>Seedr download</b>\n\n"
            "✅ Seedr completed\n"
            f"🎬 File: <code>{esc(video['name'])}</code>\n"
            "⬇️ Downloading to temporary Render storage…",
        )

        await seedr.download(video["id"], local)
        size = local.stat().st_size
        if size > MAX_FILE_SIZE_GB * 1024**3:
            raise RuntimeError("File exceeds MAX_FILE_SIZE_GB.")

        await set_progress(
            progress_msg,
            "☁️ <b>pCloud</b>\n\n"
            "✅ Seedr completed\n"
            f"🎬 File: <code>{esc(video['name'])}</code>\n"
            f"📦 Size: {human_size(size)}\n"
            "⏳ Logging in to pCloud…",
        )

        await pcloud.login_once()
        folder_id = await pcloud.ensure_folder(PCLOUD_FOLDER_PATH)

        await set_progress(
            progress_msg,
            "☁️ <b>pCloud</b>\n\n"
            "✅ pCloud login successful\n"
            f"📁 Destination: <code>{esc(PCLOUD_FOLDER_PATH)}</code>\n"
            "⬆️ Uploading…",
        )

        uploaded = await pcloud.upload(local, folder_id)
        meta = (uploaded.get("metadata") or [{}])[0]
        pcloud_path = meta.get("path") or f"{PCLOUD_FOLDER_PATH.rstrip('/')}/{video['name']}"

        # IMPORTANT: Seedr cleanup only starts after pCloud reports success.
        cleanup = "Disabled"
        if DELETE_ALL_SEEDR_FILES_AFTER_UPLOAD:
            await set_progress(
                progress_msg,
                "🧹 <b>Cleanup</b>\n\n"
                "✅ pCloud upload successful\n"
                "⏳ Deleting all Seedr files/folders…",
            )
            df, dd = await delete_everything(seedr)
            cleanup = f"{df} files + {dd} folders deleted"

        await set_progress(
            progress_msg,
            "🎉 <b>ALL DONE</b>\n\n"
            f"🎬 <b>File:</b> {esc(video['name'])}\n"
            f"📦 <b>Size:</b> {human_size(size)}\n"
            "🌱 <b>Seedr:</b> Completed\n"
            "☁️ <b>pCloud:</b> Uploaded\n"
            f"📁 <b>pCloud:</b> <code>{esc(pcloud_path)}</code>\n"
            f"🧹 <b>Seedr cleanup:</b> {esc(cleanup)}\n"
            "🗑️ <b>Render temp file:</b> Deleted",
        )

    except Exception as e:
        log.exception("Job failed")
        await set_progress(
            progress_msg,
            "❌ <b>FAILED</b>\n\n"
            f"<code>{esc(type(e).__name__)}: {esc(e)}</code>\n\n"
            "🗑️ Render temporary files were cleaned.",
        )
    finally:
        await seedr.close()
        await pcloud.close()
        shutil.rmtree(workdir, ignore_errors=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 <b>Seedr → pCloud Bot</b>\n\n"
        "Send a magnet link.\n\n"
        "Credentials are configured once in Render, so you will not be asked "
        "for Seedr or pCloud login details for every file.",
        parse_mode=ParseMode.HTML,
    )


def main():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_magnet))
    log.info("Starting bot; full Seedr cleanup=%s", DELETE_ALL_SEEDR_FILES_AFTER_UPLOAD)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
