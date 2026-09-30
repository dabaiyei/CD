"""Populate WhisperX's local cache from ModelScope's same-name model mirrors.

Run with the project Python. Files are checksum verified before cache activation;
no model code is executed. Interrupted downloads resume from .part files.
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "runtime-data/hypit-speech/models"
MODELS = {
    "Systran/faster-whisper-small": {
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
        "vocabulary.json",
    },
    "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn": {
        "config.json",
        "preprocessor_config.json",
        "pytorch_model.bin",
        "special_tokens_map.json",
        "vocab.json",
        "tokenizer_config.json",
    },
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, path, expected, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    if (
        path.exists()
        and path.stat().st_size == size
        and (not expected or digest(path) == expected)
    ):
        print(f"Cached {path.name}", flush=True)
        return
    partial = path.with_suffix(path.suffix + ".part")
    for attempt in range(3):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            if offset == size and (not expected or digest(partial) == expected):
                partial.replace(path)
                return
            with requests.get(
                url,
                headers={"Range": f"bytes={offset}-"} if offset else {},
                stream=True,
                timeout=(20, 60),
            ) as response:
                response.raise_for_status()
                append = offset and response.status_code == 206
                if append and not response.headers.get("Content-Range", "").startswith(
                    f"bytes {offset}-"
                ):
                    raise RuntimeError("Invalid resume range")
                with partial.open("ab" if append else "wb") as output:
                    for block in response.iter_content(1024 * 1024):
                        output.write(block)
            if partial.stat().st_size != size or (
                expected and digest(partial) != expected
            ):
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"Checksum mismatch: {path.name}")
            partial.replace(path)
            print(f"Verified {path.name} ({size / 1048576:.1f} MB)", flush=True)
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def prepare(repo, names):
    base = f"https://modelscope.cn/api/v1/models/{repo}/repo"
    response = requests.get(
        base + "/files", params={"Revision": "master", "Recursive": "true"}, timeout=30
    )
    response.raise_for_status()
    files = [f for f in response.json()["Data"]["Files"] if f["Path"] in names]
    required = names - {"vocabulary.json", "tokenizer_config.json"}
    if not required.issubset({f["Path"] for f in files}) or any(
        not f.get("Sha256") for f in files
    ):
        raise RuntimeError(f"Missing model checksums: {repo}")
    # A content-addressed local snapshot, separate from upstream commit snapshots.
    revision = hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()
    home = CACHE / "huggingface" / ("models--" + repo.replace("/", "--"))
    snapshot = home / "snapshots" / revision
    print(f"Preparing {repo}", flush=True)
    for file in files:
        url = (
            requests.Request(
                "GET", base, params={"Revision": "master", "FilePath": file["Path"]}
            )
            .prepare()
            .url
        )
        download(url, snapshot / file["Path"], file["Sha256"], file["Size"])
    (home / "refs").mkdir(parents=True, exist_ok=True)
    (home / "refs/main").write_text(revision, encoding="utf-8")


def prepare_english():
    name = "wav2vec2_fairseq_base_ls960_asr_ls960.pth"
    target = CACHE / "torch" / name
    total = 377664473
    if target.exists() and target.stat().st_size == total:
        return
    url = "https://download.pytorch.org/torchaudio/models/" + name
    chunks = CACHE / "torch" / (name + ".chunks")
    chunks.mkdir(parents=True, exist_ok=True)
    width = 4 * 1024 * 1024

    def fetch(start):
        end = min(start + width, total) - 1
        path = chunks / str(start)
        if path.exists() and path.stat().st_size == end - start + 1:
            return path
        for attempt in range(3):
            try:
                response = requests.get(
                    url,
                    params={"range": str(start)},
                    headers={"Range": f"bytes={start}-{end}"},
                    timeout=(15, 30),
                )
                response.raise_for_status()
                if (
                    response.status_code != 206
                    or response.headers.get("Content-Range")
                    != f"bytes {start}-{end}/{total}"
                    or len(response.content) != end - start + 1
                ):
                    raise RuntimeError("Invalid English model range")
                path.write_bytes(response.content)
                return path
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)

    with ThreadPoolExecutor(max_workers=4) as pool:
        parts = list(pool.map(fetch, range(0, total, width)))
    temporary = target.with_suffix(".assembling")
    with temporary.open("wb") as output:
        for part in parts:
            output.write(part.read_bytes())
    temporary.replace(target)
    print("English alignment downloaded from PyTorch", flush=True)


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda item: prepare(*item), MODELS.items()))
    # Pinned torchaudio English alignment resource; upstream provides no SHA256.
    # HTTPS, exact byte count and WhisperX's subsequent model loading validate it.
    prepare_english()
    print("ModelScope speech caches ready", flush=True)
