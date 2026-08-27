# -*- coding: utf-8 -*-
"""
Uber Eats 原始 JSON 資料湖上傳腳本 (Stage 3: Push to Hugging Face Hub)
【檢核與重試機制】：
1. 嚴格檢核 HF_TOKEN 與來源目錄檔案總數 (> 0)
2. 3 次指數退避重試上傳
3. 上傳後透過 Hugging Face API 進行遠端檔案清單回查驗證
4. 輸出檢核報告至 GitHub Actions $GITHUB_STEP_SUMMARY
"""

import os
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))

# 確保標準輸出與標準錯誤支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass


def append_github_step_summary(markdown_text: str):
    """將 Markdown 內容寫入 GitHub Actions $GITHUB_STEP_SUMMARY"""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(markdown_text + "\n")
        except Exception as e:
            print(f"⚠️ 寫入 GITHUB_STEP_SUMMARY 失敗: {e}")
    else:
        print(f"\n[Local Step Summary]\n{markdown_text}\n")


def fatal_error(step_name: str, reason: str, expected: str = "", actual: str = "", retries: int = 3):
    """輸出醒目錯誤橫幅、寫入 GITHUB_STEP_SUMMARY 並強制以 exit code 1 終止"""
    msg = f"""
================================================================================
❌ 【階段 3: Hugging Face 上傳檢核失敗 (FATAL ERROR)】
步驟名稱: {step_name}
重試次數: 已重試 {retries} 次均未達標
錯誤原因: {reason}
預期成果: {expected}
實際結果: {actual}
================================================================================
"""
    print(msg, file=sys.stderr, flush=True)
    
    summary_md = f"""
### ❌ 【Hugging Face 上傳失敗】
> [!CAUTION]
> **在「{step_name}」經 {retries} 次重試仍未成功，流程已強制終止 (Exit Code 1)！**
> - **錯誤原因**: `{reason}`
> - **預期成果**: `{expected}`
> - **實際結果**: `{actual}`
"""
    append_github_step_summary(summary_md)
    sys.exit(1)


def load_env_file():
    """嘗試自本機 .env 讀取環境變數"""
    env_paths = [
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".env")),
        os.path.abspath(os.path.join(os.getcwd(), ".env")),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
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


import random


def upload_to_huggingface(src_dir: str, repo_id: str, path_in_repo: str = "Json", commit_message: str = ""):
    start_time = time.time()
    load_env_file()
    token = os.environ.get("HF_TOKEN")

    print("=" * 80)
    print("🚀 【Hugging Face 資料湖備份】啟動 (強化版指數退避防衝突模式)")
    print(f"📦 來源目錄: {src_dir}")
    print(f"🎯 目標 Repo: {repo_id} (內部路徑: {path_in_repo})")
    print("=" * 80)

    # 1. 檢核 HF_TOKEN
    if not token:
        fatal_error(
            step_name="步驟 Hugging Face Token 檢核",
            reason="未檢測到 HF_TOKEN 環境變數或 Secrets",
            expected="有效的 HF_TOKEN 字串",
            actual="None (未設定)",
            retries=0
        )

    # 2. 檢核來源目錄與檔案數量
    if not os.path.exists(src_dir):
        fatal_error(
            step_name="步驟 來源目錄檢核",
            reason=f"來源目錄不存在: {src_dir}",
            expected="目錄存在且包含檔案",
            actual="目錄不存在",
            retries=0
        )

    all_files = [f for f in os.listdir(src_dir) if os.path.isfile(os.path.join(src_dir, f))] if os.path.isdir(src_dir) else []
    print(f"📦 掃描到 {len(all_files)} 個檔案準備上傳至 {path_in_repo}/。")
    
    if len(all_files) == 0:
        fatal_error(
            step_name="步驟 來源檔案總量檢核",
            reason=f"{src_dir} 內無任何檔案可供上傳",
            expected="檔案數 > 0",
            actual="0 個檔案",
            retries=0
        )

    try:
        from huggingface_hub import HfApi
    except ImportError:
        fatal_error(
            step_name="步驟 依賴套件檢核",
            reason="未安裝 huggingface_hub 套件",
            expected="huggingface_hub 已安裝",
            actual="ImportError",
            retries=0
        )

    api = HfApi(token=token)
    today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")

    if not commit_message:
        commit_message = f"Upload snapshot {today_str} ({len(all_files)} files in {path_in_repo})"

    # 3. 確保 Dataset 存在 (5 次重試)
    repo_ok = False
    for attempt in range(1, 6):
        try:
            api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True)
            print(f"✅ [確認 Repo 成功] Hugging Face Dataset: {repo_id}")
            repo_ok = True
            break
        except Exception as e:
            print(f"⚠️ [確認 Repo 異常] (嘗試 {attempt}/5): {e}")
            if attempt < 5:
                backoff = (2.0 * attempt) + random.uniform(1.0, 3.0)
                time.sleep(backoff)

    if not repo_ok:
        fatal_error(
            step_name="步驟 Dataset 建立與確認",
            reason="無法連接或建立 Hugging Face Dataset",
            expected=f"Repo {repo_id} 可訪問",
            actual="連線/權限失敗",
            retries=5
        )

    # 4. 上傳資料夾 (5 次指數退避 + 隨機擾動重試，徹底解決 Git Commit 衝突)
    upload_ok = False
    last_upload_err = ""
    for attempt in range(1, 6):
        print(f"\n🚀 正在推送 {len(all_files)} 個檔案至 Hugging Face (嘗試 {attempt}/5)...")
        try:
            api.upload_folder(
                folder_path=src_dir,
                path_in_repo=path_in_repo,
                repo_id=repo_id,
                repo_type="dataset",
                commit_message=commit_message
            )
            upload_ok = True
            print(f"✅ [上傳成功] 成功推送至 Hugging Face (路徑: {path_in_repo}/)！")
            break
        except Exception as e:
            last_upload_err = str(e)
            print(f"⚠️ [上傳異常/可能遇並行衝突] (嘗試 {attempt}/5): {e}")
            if attempt < 5:
                # 指數退避 + 隨機擾動 (5s, 10s, 18s, 30s)
                backoff = (3.0 * (1.8 ** (attempt - 1))) + random.uniform(2.0, 6.0)
                print(f"   ⏳ 觸發防衝突退避冷卻，隨機等待 {backoff:.1f} 秒後重新嘗試 Commit...")
                time.sleep(backoff)

    if not upload_ok:
        fatal_error(
            step_name="步驟 Hugging Face 資料庫推送",
            reason=f"上傳重試 5 次皆失敗: {last_upload_err}",
            expected="上傳成功 (HTTP 200 / Commit Created)",
            actual="上傳失敗",
            retries=5
        )

    # 5. 遠端檔案二次回查驗證
    print(f"\n🔍 正在向 Hugging Face API 回查確認檔案清單...")
    verify_ok = False
    for attempt in range(1, 5):
        try:
            remote_files = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
            normalized_prefix = path_in_repo.strip("/") + "/"
            remote_set = set(remote_files)
            expected_remote = {
                normalized_prefix + os.path.basename(local_name)
                for local_name in all_files
            }
            missing = expected_remote - remote_set
            files_in_repo = [f for f in remote_files if f.startswith(normalized_prefix)]
            if not missing:
                print(f"✅ [回查驗證通過] 本批 {len(expected_remote)} 個檔案皆存在（遠端目錄共 {len(files_in_repo)} 個）！")
                verify_ok = True
                break
            print(f"⚠️ [回查未完整] 尚缺 {len(missing)} 個本批檔案，將重試確認。")
        except Exception as e:
            print(f"⚠️ [回查異常] (嘗試 {attempt}/4): {e}")
            if attempt < 4:
                time.sleep(2.0 + random.uniform(1.0, 2.0))

    if not verify_ok:
        fatal_error(
            step_name="步驟 Hugging Face 遠端資料回查",
            reason="上傳後向 API 回查遠端目錄未發現檔案",
            expected="遠端檔案數 > 0",
            actual="回查失敗或無檔案",
            retries=4
        )

    elapsed = time.time() - start_time
    dataset_url = f"https://huggingface.co/datasets/{repo_id}"

    # 6. 輸出 Step Summary
    summary_md = f"""
### 📦 Hugging Face 資料湖備份檢核報告
- **目標 Repo**: [{repo_id}]({dataset_url}) | **存放目錄**: `{path_in_repo}/`
- **上傳檔案總數**: `{len(all_files)}` 個檔案 | **耗時**: `{elapsed:.2f} 秒`
- **Commit 說明**: `{commit_message}`
- **遠端回查校驗**: ✅ 已確認遠端檔案存在
"""
    append_github_step_summary(summary_md)

    print("\n" + "=" * 80)
    print(f"🎉 【Hugging Face 備份完成】網址: {dataset_url}")
    print("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="上傳資料至 Hugging Face Datasets (防衝突強化版)")
    parser.add_argument("--src-dir", default="JSON", help="原始資料夾 (預設 JSON)")
    parser.add_argument("--repo-id", default=os.environ.get("HF_REPO_ID", "hub-google/UberEat"), help="Hugging Face Dataset Repo ID")
    parser.add_argument("--path-in-repo", default="Json", help="Dataset 內部存放路徑 (預設 Json)")
    parser.add_argument("--commit-message", default="", help="自訂 Git Commit 訊息")
    args = parser.parse_args()

    upload_to_huggingface(args.src_dir, args.repo_id, args.path_in_repo, args.commit_message)

