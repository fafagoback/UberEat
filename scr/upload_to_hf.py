# -*- coding: utf-8 -*-
"""
Uber Eats 原始 JSON 資料湖上傳腳本 (Stage 3: Push to Hugging Face Hub)
使用 huggingface_hub Python SDK 將各節點的 JSON 檔案版本化同步至私有 Dataset。
"""

import os
import sys
import argparse
from datetime import datetime

# 確保標準輸出支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def load_env_file():
    """嘗試自本機 .env 讀取環境變數"""
    env_paths = [
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".env")),
        os.path.abspath(os.path.join(os.getcwd(), ".env")),
        "C:\\Users\\ET\\我的雲端硬碟\\作品\\有聲小說\\.env",
        "C:\\Users\\ET\\我的雲端硬碟\\作品\\UberEat\\.env"
    ]
    for p in env_paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            os.environ[k.strip()] = v.strip()
            except Exception:
                pass


def upload_to_huggingface(src_dir: str, repo_id: str, path_in_repo: str = "Json"):
    load_env_file()
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("⚠️ 未檢測到 HF_TOKEN 環境變數，跳過 Hugging Face 上傳。")
        return

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("❌ 未安裝 huggingface_hub，請執行 pip install huggingface_hub")
        return

    if not os.path.exists(src_dir):
        print(f"❌ 來源目錄不存在: {src_dir}")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"🚀 正在上傳 {src_dir} 內的所有原始 JSON 至 Hugging Face Dataset: {repo_id} (目標目錄: {path_in_repo})...")
    
    api = HfApi(token=token)
    
    # 確保 Dataset 存在，不存在則自動建立
    try:
        api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True)
        print(f"✅ 確認 Hugging Face Dataset: {repo_id}")
    except Exception as e:
        print(f"ℹ️ 建立/確認 Repo 狀態: {e}")

    api.upload_folder(
        folder_path=src_dir,
        path_in_repo=path_in_repo,
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=f"Upload UberEats crawl JSON snapshot {today_str}"
    )
    print(f"✅ Hugging Face 資料湖上傳成功！網址: https://huggingface.co/datasets/{repo_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="上傳資料至 Hugging Face Datasets")
    parser.add_argument("--src-dir", default="JSON", help="原始 JSON 資料夾 (預設 JSON)")
    parser.add_argument("--repo-id", default=os.environ.get("HF_REPO_ID", "fafagoback/UberEat"), help="Hugging Face Dataset Repo ID (例如 fafagoback/UberEat)")
    parser.add_argument("--path-in-repo", default="Json", help="Dataset 內部存放路徑 (預設 Json)")
    args = parser.parse_args()

    upload_to_huggingface(args.src_dir, args.repo_id, args.path_in_repo)
