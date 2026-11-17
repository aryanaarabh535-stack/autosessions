import os
import sys
import tarfile
import urllib.request
import json
import ssl
import time

HEROKU_API_KEY = os.getenv("HEROKU_API_KEY", "HRKU-AAnEy0YR5bi05uME67ZUZNK_mxThI1TtKz7ueYMMq4Pw_____wOwBFcLXO7_")
APP_NAME = os.getenv("HEROKU_APP_NAME", "tgzip-autoseller")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_PATH = os.path.join(PROJECT_DIR, "deploy_package.tar.gz")

CONFIG_VARS = {
    "BOT_TOKEN": "9035733464:AAFpeNFawtZmj3k-u5igPwatjt-tgyTC2ug",
    "ADMIN_IDS": "233444460,250730849,36888805,5258077200",
    "ROOT_OWNER_IDS": "233444460,250730849,36888805,5258077200",
    "ADMINLOG_ID": "-1004467341730",
    "DATABASE_URL": "mongodb+srv://haiu38716_db_user:zY8V62mUalSfAUma@tgbitz.n1enmes.mongodb.net/?appName=tgbitz",
    "MONGO_URI": "mongodb+srv://haiu38716_db_user:zY8V62mUalSfAUma@tgbitz.n1enmes.mongodb.net/?appName=tgbitz",
    "MONGO_DB_NAME": "tgbitz_zip_autosell",
    "API_ID": "21377358",
    "API_HASH": "e05bc1f4f03839db7864a99dbf72d1cd",
    "ENCRYPTION_KEY": "c4b5b044572298d5487f993d1b600f39574cb24e44afdf528924b1325c6e8626",
    "PYTHONUNBUFFERED": "1"
}


def make_tarfile(output_filename, source_dir):
    print("📦 Creating deployment tarball...")
    exclude_dirs = {".git", ".venv", ".vscode", "__pycache__", "tests", "temp_sessions", "extracted_archives", ".pytest_cache"}
    exclude_files = {".env", "deploy_package.tar.gz", "*.zip"}

    def filter_func(tarinfo):
        base = os.path.basename(tarinfo.name)
        if tarinfo.isdir() and base in exclude_dirs:
            return None
        if tarinfo.isreg() and (base in exclude_files or base.endswith(".zip") or base.endswith(".pyc")):
            return None
        return tarinfo

    with tarfile.open(output_filename, "w:gz") as tar:
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for file in files:
                if file not in exclude_files and not file.endswith(".zip") and not file.endswith(".pyc"):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, source_dir)
                    tar.add(full_path, arcname=rel_path, filter=filter_func)
    print(f"✅ Tarball created: {output_filename} ({os.path.getsize(output_filename)} bytes)")


def heroku_request(method, endpoint, data=None):
    url = f"https://api.heroku.com{endpoint}"
    headers = {
        "Authorization": f"Bearer {HEROKU_API_KEY}",
        "Accept": "application/vnd.heroku+json; version=3",
        "Content-Type": "application/json"
    }
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            if response.status == 204:
                return None
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        print(f"❌ Heroku API Error [{e.code}] on {endpoint}: {err_msg}")
        return None
    except Exception as e:
        print(f"❌ Connection error on {endpoint}: {e}")
        return None


def deploy():
    global APP_NAME
    print(f"🚀 Deploying TelegramZipAutoSeller to Heroku App: {APP_NAME}...")

    # 1. Check or Create App
    app_info = heroku_request("GET", f"/apps/{APP_NAME}")
    if not app_info:
        print(f"Creating new app '{APP_NAME}'...")
        app_info = heroku_request("POST", "/apps", {"name": APP_NAME, "region": "us"})
        if not app_info:
            print(f"Failed to create app '{APP_NAME}'. Attempting with alternative name...")
            alt_name = f"{APP_NAME}-{int(time.time())}"
            app_info = heroku_request("POST", "/apps", {"name": alt_name, "region": "us"})
            if not app_info:
                print("Could not create app. Exiting.")
                return False
            APP_NAME = alt_name
    print(f"✅ Target app active: {APP_NAME}")

    # 2. Set Config Vars
    print("⚙️ Updating config variables...")
    heroku_request("PATCH", f"/apps/{APP_NAME}/config-vars", CONFIG_VARS)
    print("✅ Config variables saved.")

    # 3. Create Source Blob
    print("📤 Requesting source upload URL...")
    sources = heroku_request("POST", f"/apps/{APP_NAME}/sources")
    if not sources or "source_blob" not in sources:
        print("❌ Failed to obtain source blob URLs.")
        return False

    put_url = sources["source_blob"]["put_url"]
    get_url = sources["source_blob"]["get_url"]

    # 4. Create and Upload Tarball
    make_tarfile(ARCHIVE_PATH, PROJECT_DIR)
    print("Uploading archive to Heroku storage...")
    try:
        ctx = ssl.create_default_context()
        with open(ARCHIVE_PATH, "rb") as f:
            tar_data = f.read()
        req = urllib.request.Request(put_url, data=tar_data, method="PUT")
        with urllib.request.urlopen(req, context=ctx) as resp:
            print(f"✅ Archive uploaded (Status: {resp.status})")
    except Exception as e:
        print(f"❌ Upload failed: {e}")
        return False
    finally:
        if os.path.exists(ARCHIVE_PATH):
            os.remove(ARCHIVE_PATH)

    # 5. Trigger Build
    print("🔨 Triggering build...")
    build_payload = {
        "source_blob": {
            "url": get_url,
            "version": f"v1.0.0-{int(time.time())}"
        }
    }
    build_resp = heroku_request("POST", f"/apps/{APP_NAME}/builds", build_payload)
    if not build_resp or "id" not in build_resp:
        print("❌ Failed to trigger build.")
        return False

    build_id = build_resp["id"]
    print(f"Build triggered! ID: {build_id}")

    # 6. Stream/Poll Build Status
    print("⏳ Monitoring build progress...")
    for _ in range(60):
        time.sleep(5)
        status_info = heroku_request("GET", f"/apps/{APP_NAME}/builds/{build_id}")
        if not status_info:
            continue
        status = status_info.get("status")
        print(f"Build status: {status}")
        if status == "succeeded":
            print("🎉 Build succeeded!")
            break
        elif status == "failed":
            print("❌ Build failed!")
            return False

    # 7. Scale Dyno
    print("⚙️ Ensuring worker dyno is active...")
    formation = heroku_request("PATCH", f"/apps/{APP_NAME}/formation", {
        "updates": [{"type": "worker", "quantity": 1, "size": "eco"}]
    })
    print(f"✅ Formation updated: {formation}")
    print(f"🚀 TelegramZipAutoSeller is now LIVE on Heroku ({APP_NAME})!")
    return True


if __name__ == "__main__":
    deploy()
